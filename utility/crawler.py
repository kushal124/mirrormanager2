#!/usr/bin/env python

import argparse
import datetime
import ftplib
from ftplib import FTP
import hashlib
import httplib
import logging
import multiprocessing.pool
import os
import smtplib
import socket
import sys
import threading
import time
import urlgrabber
import urlparse

sys.path.append('..')
import mirrormanager2.lib
from mirrormanager2.lib.model import HostCategoryDir
from mirrormanager2.lib.sync import run_rsync


logger = logging.getLogger("crawler")

# This is a "thread local" object that allows us to store the start time of
# each worker thread (so they can measure and check if they should time out or
# not...)
threadlocal = threading.local()


def thread_id():
    """ Silly util that returns a git-style short-hash id of the thread. """
    return hashlib.md5(str(threading.current_thread().ident)).hexdigest()[:7]


def doit(options, config):

    session = mirrormanager2.lib.create_session(config['DB_URL'])

    # Get *all* of the mirrors
    hosts = mirrormanager2.lib.get_mirrors(session, private=False)

    # Limit our host list down to only the ones we really want to crawl
    hosts = (
        host for host in hosts if (
            not host.id < options.startid and
            not host.id >= options.stopid and
            not (
                host.admin_active and
                host.user_active and
                host.site.user_active and
                host.site.admin_active) and
            not host.site.private)
    )

    # And then, for debugging, only do one host
    #hosts = [hosts.next()]

    # Before we do work, chdir to /var/tmp/.  mirrormanager1 did this and I'm
    # not totally sure why...
    os.chdir('/var/tmp')

    # Then create a threadpool to handle as many at a time as we like
    threadpool = multiprocessing.pool.ThreadPool(processes=options.threads)
    fn = lambda host: worker(options, config, host)
    results = threadpool.map(fn, hosts)
    return results


def setup_logging(debug):
    logging.basicConfig()
    if debug:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)
    return logger


def main():
    parser = argparse.ArgumentParser(usage=sys.argv[0] + " [options]")
    parser.add_argument(
        "-c", "--config",
        dest="config", default='/etc/mirrormanager/prod.cfg',
        help="Configuration file to use")

    parser.add_argument(
        "--include-private",
        action="store_true", dest="include_private", default=False,
        help="Include hosts marked 'private' in the crawl")

    parser.add_argument(
        "-t", "--threads", type=int, dest="threads", default=10,
        help="max threads to start in parallel")

    parser.add_argument(
        "--timeout-minutes", type=int, dest="timeout_minutes", default=120,
        help="per-host timeout, in minutes")

    parser.add_argument(
        "--startid", type=int, metavar="ID", dest="startid", default=0,
        help="Start crawling at host ID (default=0)")

    parser.add_argument(
        "--stopid", type=int, metavar="ID",
        dest="stopid", default=sys.maxint,
        help="Stop crawling before host ID (default=maxint)")

    parser.add_argument(
        "--category", dest="categories", action="append", default=[],
        help="Category to scan (default=all), can be repeated")

    parser.add_argument(
        "--canary", dest="canary", action="store_true", default=False,
        help="fast crawl by only scanning for canary files")

    parser.add_argument(
        "--debug", "-d", dest="debug", action="store_true", default=False,
        help="enable printing of debug-level messages")

    options = parser.parse_args()

    if options.canary:
        raise NotImplementedError("Canary mode is not yet implemented.")

    setup_logging(options.debug)

    config = dict()
    with open(options.config) as config_file:
        exec(compile(config_file.read(), options.config, 'exec'), config)

    doit(options, config)
    return 0


################################################
# overrides for httplib because we're
# handling keepalives ourself
################################################
class myHTTPResponse(httplib.HTTPResponse):
    def begin(self):
        httplib.HTTPResponse.begin(self)
        self.will_close = False

    def isclosed(self):
        """This is a hack, because otherwise httplib will fail getresponse()"""
        return True

    def keepalive_ok(self):
        # HTTP/1.1 connections stay open until closed
        if self.version == 11:
            ka = self.msg.getheader('connection')
            if ka and "close" in ka.lower():
                return False
            else:
                return True

        # other HTTP connections may have a connection: keep-alive header
        ka = self.msg.getheader('connection')
        if ka and "keep-alive" in ka.lower():
            return True

        try:
            ka = self.msg.getheader('keep-alive')
            if ka is not None:
                maxidx = ka.index('max=')
                maxval = ka[maxidx+4:]
                if maxval == '1':
                    return False
                return True
            else:
                ka = self.msg.getheader('connection')
                if ka and "keep-alive" in ka.lower():
                    return True
                return False
        except:
            return False
        return False


class myHTTPConnection(httplib.HTTPConnection):
    response_class = myHTTPResponse

    def end_request(self):
        self.__response = None


################################################
# the magic begins


def timeout_check(options):
    delta = time.time() - threadlocal.starttime
    if delta > (options.timeout_minutes * 60):
        raise TimeoutException("Timed out after %rs" % delta)


class hostState:
    def __init__(self, http_debuglevel=0, ftp_debuglevel=0):
        self.httpconn = {}
        self.ftpconn = {}
        self.http_debuglevel = http_debuglevel
        self.ftp_debuglevel = ftp_debuglevel
        self.ftp_dir_results = None
        self.keepalives_available = False

    def get_connection(self, url):
        scheme, netloc, path, query, fragment = urlparse.urlsplit(url)
        if scheme == 'ftp':
            if self.ftpconn.has_key(netloc):
                return self.ftpconn[netloc]
        elif scheme == 'http':
            if self.httpconn.has_key(netloc):
                return self.httpconn[netloc]
        return None

    def open_http(self, url):
        scheme, netloc, path, query, fragment = urlparse.urlsplit(url)
        if not self.httpconn.has_key(netloc):
            self.httpconn[netloc] = myHTTPConnection(netloc)
            self.httpconn[netloc].set_debuglevel(self.http_debuglevel)
        return self.httpconn[netloc]

    def _open_ftp(self, netloc):
        if not self.ftpconn.has_key(netloc):
            self.ftpconn[netloc] = FTP(netloc)
            self.ftpconn[netloc].set_debuglevel(self.ftp_debuglevel)
            self.ftpconn[netloc].login()

    def check_ftp_dir_callback(self, line):
        if self.ftp_debuglevel > 0:
            logger.info(line)
        self.ftp_dir_results.append(line)

    def ftp_dir(self, url):
        scheme, netloc, path, query, fragment = urlparse.urlsplit(url)
        self._open_ftp(netloc)
        c = self.ftpconn[netloc]
        self.ftp_dir_results = []
        c.dir(path, self.check_ftp_dir_callback)


    def close_http(self, url):
        scheme, netloc, path, query, fragment = urlparse.urlsplit(url)
        if self.httpconn.has_key(netloc):
            self.httpconn[netloc].close()
            del self.httpconn[netloc]

    def close_ftp(self, url):
        scheme, netloc, path, query, fragment = urlparse.urlsplit(url)
        if self.ftpconn.has_key(netloc):
            try:
                self.ftpconn[netloc].quit()
            except:
                pass
            del self.ftpconn[netloc]

    def close(self):
        for c in self.httpconn.keys():
            self.close_http(c)

        for c in self.ftpconn.keys():
            self.close_ftp(c)


class TryLater(Exception): pass
class ForbiddenExpected(Exception): pass
class TimeoutException(Exception): pass
class HTTPUnknown(Exception): pass
class HTTP500(Exception): pass


def get_ftp_dir(hoststate, url, readable, i=0):
    if i > 1:
        raise TryLater()

    try:
        hoststate.ftp_dir(url)
    except ftplib.error_perm, e:
        # Returned by Princeton University when directory does not exist
        if str(e).startswith('550'):
            return []
        # Returned by Princeton University when directory isn't readable
        # (pre-bitflip)
        if str(e).startswith('553'):
            if readable:
                return []
            else:
                raise ForbiddenExpected()
        # Returned by ftp2.surplux.net when cannot log in due to connection
        # restrictions
        if str(e).startswith('530'):
            hoststate.close_ftp(url)
            return get_ftp_dir(hoststate, url, readable, i+1)
        if str(e).startswith('500'): # Oops
            raise TryLater()
        else:
            logger.error("unknown permanent error %s on %s" % (e, url))
            raise
    except ftplib.error_temp, e:
        # Returned by Boston University when directory does not exist
        if str(e).startswith('450'):
            return []
        # Returned by Princeton University when cannot log in due to
        # connection restrictions
        if str(e).startswith('421'):
            logger.info("Connections Exceeded %s" % url)
            raise TryLater()
        if str(e).startswith('425'):
            logger.info("Failed to establish connection on %s" % url)
            raise TryLater()
        else:
            logger.error("unknown error %s on %s" % (e, url))
            raise
    except (EOFError, socket.error):
        hoststate.close_ftp(url)
        return get_ftp_dir(hoststate, url, readable, i+1)

    return hoststate.ftp_dir_results


def check_ftp_file(hoststate, url, filedata, readable):
    if url.endswith('/'):
        url = url[:-1]
    try:
        results = get_ftp_dir(hoststate, url, readable)
    except TryLater:
        raise
    except ForbiddenExpected:
        return None
    if results is None:
        return None
    if len(results) == 1:
        line = results[0].split()
        if line[4] == filedata['size']:
            return True
    return False


def check_url(hoststate, url, filedata, recursion, readable):
    if url.startswith('http:'):
        return check_head(hoststate, url, filedata, recursion, readable)
    elif url.startswith('ftp:'):
        return check_ftp_file(hoststate, url, filedata, readable)


def handle_redirect(hoststate, url, location, filedata, recursion, readable):
    if recursion > 10:
        raise HTTPUnknown()
    if location.startswith('/'):
        scheme, netloc, path, query, fragment = urlparse.urlsplit(url)
        location = '%s:%s%s' % (scheme, netloc, location)
    return check_url(hoststate, location, filedata, recursion+1, readable)


def check_head(hoststate, url, filedata, recursion, readable, retry=0):
    """ Returns tuple:
    True - URL exists
    False - URL doesn't exist
    None - we don't know
    """

    try:
        conn = hoststate.open_http(url)
    except:
        return None

    scheme, netloc, path, query, fragment = urlparse.urlsplit(url)
    reqpath = path
    if len(query) > 0:
        reqpath += "?%s" % query
    if len(fragment) > 0:
        reqpath += "#%s" % fragment
    conn.request('HEAD', reqpath,
                 headers={'Connection':'Keep-Alive',
                          'Pragma':'no-cache',
                          'User-Agent':'mirrormanager-crawler/0.1 (+http://fedorahosted.org/mirrormanager)'})

    r = None
    try:
        r = conn.getresponse()
        status = r.status
    except:
        if retry == 0:
            # retry once
            hoststate.close_http(url)
            return check_head(hoststate, url, filedata, recursion, readable, retry=1)
        else:
            raise HTTPUnknown()

    conn.end_request()
    keepalive_ok = r.keepalive_ok()
    if keepalive_ok:
        hoststate.keepalives_available = True
    if not keepalive_ok:
        hoststate.close_http(url)

    content_length = r.getheader('Content-Length')
    #last_modified  = r.getheader('Last-Modified')

    if status >= 200 and status < 300:
        # fixme should check last_modified too
        if filedata['size'] == content_length or content_length is None: # handle no content-length header, streaming/chunked return or zero-length file
            return True
        else:
            return False
    if status >= 300 and status < 400:
        return handle_redirect(hoststate, url, r.getheader('Location'), filedata, recursion, readable)
    elif status >= 400 and status < 500:
        if status == 403: # forbidden
            # may be a hidden dir still
            if readable:
                return False
            else:
                raise ForbiddenExpected()
        elif status == 404 or status == 410: # not found / gone
            return False
        # we don't know
        return None
    elif status >= 500:
        raise HTTP500()

    logger.info("status = %s" % status)
    raise HTTPUnknown()


def report_stats(stats):
    msg = "Total directories: %d" % stats['numkeys']
    logger.info(msg)
    msg = "Changed to up2date: %d" % stats['up2date']
    logger.info(msg)
    msg = "Changed to not up2date: %d" % stats['not_up2date']
    logger.info(msg)
    msg = "Unchanged: %d" % stats['unchanged']
    logger.info(msg)
    msg = "Unknown disposition: %d" % stats['unknown']
    logger.info(msg)
    msg = "New HostCategoryDirs created: %d" % stats['newdir']
    logger.info(msg)
    msg = "HostCategoryDirs now deleted on the master, marked not "\
        "up2date: %d" % stats['deleted_on_master']
    logger.info(msg)


def sync_hcds(session, host, host_category_dirs):
    stats = dict(up2date = 0, not_up2date = 0, unchanged = 0,
                 unknown = 0, newdir = 0, deleted_on_master = 0)
    current_hcds = {}
    now = datetime.datetime.utcnow()
    host.lastCrawled = now
    keys = host_category_dirs.keys()
    keys = sorted(keys, key = lambda t: t[1].name)
    stats['numkeys'] = len(keys)
    for (hc, d) in keys:
        up2date = host_category_dirs[(hc, d)]
        if up2date is None:
            stats['unknown'] += 1
            continue

        topname = hc.category.topdir.name
        path = d.name[len(topname)+1:]

        hcd = mirrormanager2.lib.get_hostcategorydir_by_hostcategoryid_and_path(
            session, host_category_id=hc.id, path=path)
        if len(hcd) > 0:
            hcd = hcd[0]
        else:
            # don't create HCDs for directories which aren't up2date on the
            # mirror chances are the mirror is excluding that directory
            if not up2date: continue
            hcd = HostCategoryDir(
                host_category_id=hc.id, path=path, directory_id=d.id)
            stats['newdir'] += 1

        if hcd.directory is None:
            hcd.directory = d
        if hcd.up2date != up2date:
            hcd.up2date = up2date
            session.add(hcd)
            if up2date == False:
                logger.info(
                    "Directory %s is not up-to-date on this host." % d.name)
                stats['not_up2date'] += 1
            else:
                logger.info(d.name)
                stats['up2date'] += 1
        else:
            stats['unchanged'] += 1

        current_hcds[hcd] = True

    # now-historical HostCategoryDirs are not up2date
    # we wait for a cascading Directory delete to delete this
    for hc in list(host.categories):
        for hcd in list(hc.directories):
            if hcd.directory is not None and not hcd.directory.readable:
                stats['unreadable'] += 1
                continue
            if hcd not in current_hcds:
                if hcd.up2date != False:
                    hcd.up2date = False
                    session.add(hcd)
                    stats['deleted_on_master'] += 1
    session.commit()
    report_stats(stats)


def method_pref(urls, prev=""):
    """ return which of the hosts connection method should be used
    rsync > http > ftp """
    pref = None
    for u in urls:
        if prev.startswith('rsync:'):
            break
        if u.startswith('rsync:'):
            return u
    for u in urls:
        if u.startswith('http:'):
            pref = u
            break
    if pref is None:
        for u in urls:
            if u.startswith('ftp:'):
                pref = u
                break
    return pref


def parent(session, directory):
    parentDir = None
    splitpath = directory.name.split(u'/')
    if len(splitpath[:-1]) > 0:
        parentPath = u'/'.join(splitpath[:-1])
        parentDir = mirrormanager2.lib.get_directory_by_name(
            session, parentPath)
    return parentDir


def add_parents(session, host_category_dirs, hc, d):
    parentDir = parent(session, d)
    if parentDir is not None:
        if (hc, parentDir) not in host_category_dirs:
            host_category_dirs[(hc, parentDir)] = None
        if parentDir != hc.category.topdir: # stop at top of the category
            return add_parents(session, host_category_dirs, hc, parentDir)

    return host_category_dirs


def compare_sha256(d, filename, graburl):
    """ looks for a FileDetails object that matches the given URL """
    found = False
    s = urlgrabber.urlread(graburl)
    sha256 = hashlib.sha256(s).hexdigest()
    for fd in list(d.fileDetails):
        if fd.filename == filename and fd.sha256 is not None:
            if fd.sha256 == sha256:
                found = True
                break
    return found


def try_per_file(d, hoststate, url):
    if d.files is None:
        return None
    exists = None
    for filename in d.files.keys():
        exists = None
        graburl = "%s/%s" % (url, filename)
        try:
            exists = check_url(
                hoststate, graburl, d.files[filename], 0, d.readable)
            if exists == False:
                return False
        except TryLater:
            raise
        except ForbiddenExpected:
            return None
        except ftplib.all_errors:
            hoststate.close_ftp(url)
            return None
        except:
            return None

        if filename == 'repomd.xml':
            try:
                exists = compare_sha256(d, filename, graburl)
            except:
                pass
            if exists == False:
                return False

    if exists is None:
        return None

    return True


def try_per_category(
        session, trydirs, url, host_category_dirs, hc, host,
        categoryPrefixLen, options, config):
    """ In addition to the crawls using http and ftp, this rsync crawl
    scans the complete category with one connection instead perdir (ftp)
    or perfile(http). """

    if not url.startswith('rsync'):
        return None

    # rsync URL available, let's use it; it requires only one network
    # connection instead of multiples like with http and ftp
    rsync = {}
    if not url.endswith('/'):
        url += '/'

    rsync_start_time = datetime.datetime.utcnow()
    try:
        result, listing = run_rsync(url, '--no-motd')
    except:
        logger.warning('Failed to run rsync.', exc_info = True)
        return False
    rsync_stop_time = datetime.datetime.utcnow()
    msg = "rsync time: %s" % str(rsync_stop_time - rsync_start_time)
    logger.info(msg)
    if result == 10:
        # no rsync content, fail!
        logger.warning(
            'Connection to host %s Refused.  Please check that the URL is '
            'correct and that the host has an rsync module still available.'
            % host.name)
        return False
    if result > 0:
        logger.info('rsync returned exit code %d' % result)

    # put the rsync listing in a dict for easy access
    while True:
        line = listing.readline()
        if not line: break
        fields = line.split()
        try:
            rsync[fields[4]] = {
                'mode': fields[0], 'size': fields[1],
                'date': fields[2], 'time': fields[3]}
        except IndexError:
            logger.debug("invalid rsync line: %s\n" % line)

    logger.debug("rsync listing has %d lines" % len(rsync))
    if len(rsync) == 0:
        # no rsync content, fail!
        return False
    # for all directories in this category
    for d in trydirs:
        d = d.directory
        timeout_check(options)

        # ignore unreadable directories - we can't really know about them
        if not d.readable:
            host_category_dirs[(hc, d)] = None
            continue

        all_files = True
        # the rsync listing is missing the category part of the url
        # remove if from the ones we are comparing it with
        name = d.name[categoryPrefixLen:]
        for filename in sorted(d.files.keys()):
            if len(name) == 0:
                key = filename
            else:
                key = os.path.join(name, filename)
            try:
                logger.debug('trying with key %s' % key)
                if rsync[key]['size'] != d.files[filename]['size'] \
                        and not rsync[key]['mode'].startswith('l'):
                    # ignore symlink size differences
                    logger.debug(
                        'rsync: file size mismatch %s %s != %s\n'
                        % (filename, d.files[filename]['size'],
                           rsync[key]['size']))
                    all_files = False
                    break
            except KeyError: # file is not in the rsync listing
                msg = 'Missing remote file %s\n' % key
                logger.debug(msg)
                all_files = False
                break
            except: # something else went wrong
                exception_msg = "Exception caught in try_per_category()\n"
                logger.exception(exception_msg)
                all_files = False
                break

        if all_files is False:
            host_category_dirs[(hc, d)] = False
        else:
            host_category_dirs[(hc, d)] = True
            host_category_dirs = add_parents(
                session, host_category_dirs, hc, d)

    if len(host_category_dirs) > 0:
        return True

    mark_not_up2date(
        session, config,
        None, host, "No host category directories found.  "
        "Check that your Host Category URLs are correct.")
    return False


def try_per_dir(d, hoststate, url):
    if d.files is None:
        return None
    if not url.startswith('ftp'):
        return None
    results = {}
    if not url.endswith('/'):
        url += '/'
    listing = get_ftp_dir(hoststate, url, d.readable)
    if listing is None:
        return None

    if len(listing) == 0:
        return False

    for line in listing:
        if line.startswith('total'): # some servers first include a line starting with the word 'total' that we can ignore
            continue
        fields = line.split()
        try:
            results[fields[8]] = {'size': fields[4]}
        except IndexError: # line doesn't have 8 fields, it's not a dir line
            pass

    for filename in d.files.keys():
        try:
            if results[filename]['size'] != d.files[filename]['size']:
                return False
        except:
            return False
    return True


def send_email(config, host, report_str, exc):
    if not config.get('crawler.send_email', False):
        return

    SMTP_DATE_FORMAT = "%a, %d %b %Y %H:%M:%S %z"
    msg = """From: %s
To: %s
Subject: %s MirrorManager crawler report
Date: %s

""" % (config.get('crawler.mail_from'),
       config.get('crawler.admin_mail_to'),
       host.name,
       time.strftime(SMTP_DATE_FORMAT))

    msg += report_str + '\n'
    msg += 'Log can be found at %s/%s.log\n' % (config.get('crawler.logdir'), str(host.id))
    if exc is not None:
        msg += "Exception info: type %s; value %s\n" % (exc[0], exc[1])
        msg += str(exc[2])
    try:
        smtp = smtplib.SMTP(config.get('crawler.smtp_host'),
                            config.get('crawler.smtp_port'))

        username = config.get('crawler.smtp_username')
        password = config.get('crawler.smtp_password')

        if username and password:
            smtp.login(username, password)

        smtp.sendmail(config.get('crawler.smtp_from'),
                      config.get('crawler.admin_mail_to'),
                      msg)
    except:
        logger.exception("Error sending email")
        logger.debug("Email message follows:")
        logger.debug(msg)

    try:
        smtp.quit()
    except:
        pass


def mark_not_up2date(session, config, exc, host, reason="Unknown"):
    host.set_not_up2date(session, )
    host.lastCrawled = datetime.datetime.utcnow()
    msg = "Host marked not up2date: %s" % reason
    logger.warning(msg)
    if exc is not None:
        logger.debug("%s %s %s" % (exc[0], exc[1], exc[2]))
    send_email(config, host, msg, exc)


def select_host_categories_to_scan(session, options, host):
    result = []
    if options.categories:
        for category in options.categories:
            hc = mirrormanager2.lib.get_host_category_by_hostid_category(
                session, host_id=host.id, category=category)
            for entry in hc:
                result.append(hc)
    else:
        result = list(host.categories)
    return result


def per_host(session, host, options, config):
    """Canary mode looks for 2 things:
    directory.path ends in 'iso' or directory.path ends in 'repodata'.  In
    this case it checks for availability of each of the files in those
    directories.
    """
    rc = 0
    host = mirrormanager2.lib.get_host(session, host)
    host_category_dirs = {}
    if host.private and not options.include_private:
        return 1
    http_debuglevel = 0
    ftp_debuglevel = 0
    if options.debug:
        http_debuglevel = 2
        ftp_debuglevel = 2

    hoststate = hostState(
        http_debuglevel=http_debuglevel,
        ftp_debuglevel=ftp_debuglevel)

    categoryUrl = ''
    host_categories_to_scan = select_host_categories_to_scan(
        session, options, host)
    if len(host_categories_to_scan) == 0:
        mark_not_up2date(
            session, config,
            None, host, "No host category directories found.  "
            "Check that your Host Category URLs are correct.")
        return 1

    for hc in host_categories_to_scan:
        if hc.always_up2date:
            continue
        category = hc.category

        logger.info("scanning Category %s" % category.name)

        host_category_urls = [hcurl.url for hcurl in hc.urls]
        categoryUrl = method_pref(host_category_urls)
        if categoryUrl is None:
            continue
        categoryPrefixLen = len(category.topdir.name)+1

        trydirs = list(hc.directories)
        # check the complete category in one go with rsync
        try:
            has_all_files = try_per_category(
                session, trydirs, categoryUrl, host_category_dirs, hc,
                host, categoryPrefixLen, options, config)
        except TimeoutException:
            raise

        if type(has_all_files) == type(True):
            # all files in this category are up to date, or not
            # no further checks necessary
            # do the next category
            continue

        # has_all_files is None, we don't know what failed, but something did
        # change preferred protocol if necessary to http or ftp
        categoryUrl = method_pref(host_category_urls, categoryUrl)

        try_later_delay = 1
        for d in trydirs:
            timeout_check(options)

            d = d.directory

            if not d.readable:
                continue

            if options.canary:
                if not (d.name.endswith('/repodata') \
                        or d.name.endswith('/iso')):
                    continue

            dirname = d.name[categoryPrefixLen:]
            url = '%s/%s' % (categoryUrl, dirname)

            try:
                has_all_files = try_per_dir(d, hoststate, url)
                if has_all_files is None:
                    has_all_files = try_per_file(d, hoststate, url)

                if has_all_files == False:
                    logger.warning("Not up2date: %s" % (d.name))
                    host_category_dirs[(hc, d)] = False
                elif has_all_files == True:
                    host_category_dirs[(hc, d)] = True
                    logger.info(url)
                    # make sure our parent dirs appear on the list too
                    host_category_dirs = add_parents(
                        session, host_category_dirs, hc, d)
                else:
                    # could be a dir with no files, or an unreadable dir.
                    # defer decision on this dir, let a child decide.
                    pass
            except TryLater:
                msg = "Server load exceeded - try later (%s seconds)" % (
                    try_later_delay)
                logger.warning(msg)
                if categoryUrl.startswith('http') \
                        and not hoststate.keepalives_available:
                    logger.warning(
                        "Host %s (id=%d) does not have HTTP Keep-Alives "
                        "enabled." % (host.name, host.id))

                time.sleep(try_later_delay)
                if try_later_delay < 8:
                    try_later_delay = try_later_delay << 1

            except:
                logging.exception("Unhandled exception raised.")
                mark_not_up2date(
                    session, config,
                    sys.exc_info(), host, "Unhandled exception raised.  "
                    "This is a bug in the MM crawler.")
                rc = 1
                break
        if categoryUrl.startswith('http') and not hoststate.keepalives_available:
            logger.warning(
                "Host %s (id=%d) does not have HTTP Keep-Alives enabled."
                % (host.name, host.id))
    hoststate.close()

    if rc == 0:
        if len(host_category_dirs) > 0:
            sync_hcds(session, host, host_category_dirs)
    return rc


def worker(options, config, host):
    logger.info("Worker %r looking at host %r" % (thread_id(), host))
    threadlocal.starttime = time.time()

    # Each worker gets its own thread
    session = mirrormanager2.lib.create_session(config['DB_URL'])

    try:
        rc = per_host(session, host.id, options, config)
        session.commit()
        session.close()
    except TimeoutException:
        rc = 2
#        mark_not_up2date(
#            session, config,
#            None, host.id,
#            "Crawler timed out before completing.  "
#            "Host is likely overloaded.")
    except Exception:
        logger.exception("Failure in thread %r, host %r" % (thread_id(), host))
        rc = 3

    logger.info("Ending crawl with status %r" % rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
