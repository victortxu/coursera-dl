import re
import urllib
import urllib2
from urlparse import urlsplit
import argparse
import os
import errno
import unicodedata
import getpass
import netrc
import mechanize
import cookielib
from bs4 import BeautifulSoup
import tempfile
from os import path
import platform
import sys
import _version

class CourseraDownloader(object):
    """
    Class to download content (videos, lecture notes, ...) from coursera.org for
    use offline.

    https://github.com/dgorissen/coursera-dl

    :param username: username
    :param password: password
    :keyword proxy: http proxy, eg: foo.bar.com:1234
    :keyword parser: xml parser 
    :keyword ignorefiles: comma separated list of file extensions to skip (e.g., "ppt,srt")
    """
    BASE_URL =    'https://class.coursera.org/%s'
    HOME_URL =    BASE_URL + '/class/index'
    LECTURE_URL = BASE_URL + '/lecture/index'
    QUIZ_URL =    BASE_URL + '/quiz/index'
    AUTH_URL =    BASE_URL + "/auth/auth_redirector?type=login&subtype=normal"
    LOGIN_URL =   "https://accounts.coursera.org/api/v1/login"

    #see http://www.crummy.com/software/BeautifulSoup/bs4/doc/#installing-a-parser
    DEFAULT_PARSER = "html.parser"

    # how long to try to open a URL before timing out
    TIMEOUT=60.0

    def __init__(self,username,password,proxy=None,parser=DEFAULT_PARSER,ignorefiles=None, max_path_len=None):
        self.username = username
        self.password = password
        self.parser = parser

        # Split "ignorefiles" argument on commas, strip, remove prefixing dot
        # if there is one, and filter out empty tokens.
        self.ignorefiles =  [x.strip()[1:] if x[0]=='.' else x.strip()
                             for x in ignorefiles.split(',') if len(x)]

        self.browser = None
        self.proxy = proxy
        self.max_path_len = max_path_len

    def login(self,className):
        """
        Automatically generate a cookie file for the coursera site.
        """
        #TODO: use proxy here
        hn,fn = tempfile.mkstemp()
        cookies = cookielib.LWPCookieJar()
        handlers = [
            urllib2.HTTPHandler(),
            urllib2.HTTPSHandler(),
            urllib2.HTTPCookieProcessor(cookies)
        ]
        opener = urllib2.build_opener(*handlers)

        url = self.lecture_url_from_name(className)
        req = urllib2.Request(url)

        try:
            res = opener.open(req)
        except urllib2.HTTPError as e:
            if e.code == 404:
                raise Exception("Unknown class %s" % className)

        # get the csrf token
        csrfcookie = [c for c in cookies if c.name == "csrf_token"]
        if not csrfcookie: raise Exception("Failed to find csrf cookie")
        csrftoken = csrfcookie[0].value
        opener.close()

        # call the authenticator url:
        cj = cookielib.MozillaCookieJar(fn)
        opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(cj),
                                    urllib2.HTTPHandler(),
                                    urllib2.HTTPSHandler())

        opener.addheaders.append(('Cookie', 'csrftoken=%s' % csrftoken))
        opener.addheaders.append(('Referer', 'https://accounts.coursera.org/signin'))
        opener.addheaders.append(('X-CSRFToken', csrftoken))
        req = urllib2.Request(self.LOGIN_URL)

        data = urllib.urlencode({'email': self.username,'password': self.password})
        req.add_data(data)

        try:
            opener.open(req)
        except urllib2.HTTPError as e:
            if e.code == 401:
                raise Exception("Invalid username or password")

        # check if we managed to login
        sessionid = [c.name for c in cj if c.name == "CAUTH"]
        if not sessionid:
            raise Exception("Failed to authenticate as %s" % self.username)

        # all should be ok now, mechanize can handle the rest if we give it the
        # cookies
        br = mechanize.Browser()
        #br.set_debug_http(True)
        #br.set_debug_responses(False)
        #br.set_debug_redirects(True)
        br.set_handle_robots(False)
        br.set_cookiejar(cj)

        if self.proxy:
            br.set_proxies({"http":self.proxy})

        self.browser = br

        # also use this cookiejar for other mechanize operations (e.g., urlopen)
        opener = mechanize.build_opener(mechanize.HTTPCookieProcessor(cj))
        mechanize.install_opener(opener)

    def course_name_from_url(self,course_url):
        """Given the course URL, return the name, e.g., algo2012-p2"""
        return course_url.split('/')[3]

    def lecture_url_from_name(self,course_name):
        """Given the name of a course, return the video lecture url"""
        return self.LECTURE_URL % course_name

    def get_downloadable_content(self,course_url):
        """Given the video lecture URL of the course, return a list of all
        downloadable resources."""

        cname = self.course_name_from_url(course_url)

        print "* Collecting downloadable content from " + course_url

        # get the course name, and redirect to the course lecture page
        vidpage = self.browser.open(course_url,timeout=self.TIMEOUT)

        # extract the weekly classes
        soup = BeautifulSoup(vidpage,self.parser)

        # extract the weekly classes
        weeks = soup.findAll("div", { "class" : "course-item-list-header" })

        weeklyTopics = []
        allClasses = {}

        # for each weekly class
        for week in weeks:
            h3 = week.findNext('h3')
            sanitisedHeaderName = sanitise_filename(h3.text)
            weeklyTopics.append(sanitisedHeaderName)
            ul = week.next_sibling
            lis = ul.findAll('li')
            weekClasses = {}

            # for each lecture in a weekly class
            classNames = []
            for li in lis:
                # the name of this lecture/class
                className = li.a.find(text=True).strip()

                # Many class names have the following format: 
                #   "Something really cool (12:34)"
                # If the class name has this format, replace the colon in the
                # time with a hyphen.
                if re.match(".+\(\d?\d:\d\d\)$",className):
                    head,sep,tail = className.rpartition(":")
                    className = head  + "-" + tail

                className = sanitise_filename(className)
                classNames.append(className)
                classResources = li.find('div', {'class':'course-lecture-item-resource'})

                hrefs = classResources.findAll('a')

                # collect the resources for a particular lecture (slides, pdf,
                # links,...)
                resourceLinks = []

                for a in hrefs:
                    # get the hyperlink itself
                    h = a.get('href')
                    if not h: continue

                    # Sometimes the raw, uncompresed source videos are available as
                    # well. Don't download them as they are huge and available in
                    # compressed form anyway.
                    if h.find('source_videos') > 0:
                        print "   - will skip raw source video " + h
                    else:
                        # Dont set a filename here, that will be inferred from the week
                        # titles
                        resourceLinks.append( (h,None) )
 
                # check if the video is included in the resources, if not, try
                # do download it directly
                hasvid = [x for x,_ in resourceLinks if x.find('.mp4') > 0]
                if not hasvid:
                    ll = li.find('a',{'class':'lecture-link'})
                    lurl = ll['data-modal-iframe']

                    try:
                        pg = self.browser.open(lurl,timeout=self.TIMEOUT)

                        bb = BeautifulSoup(pg,self.parser)
                        vobj = bb.find('source',type="video/mp4")

                        if not vobj:
                            print " Warning: Failed to find video for %s" %  className
                        else:
                            vurl = vobj['src']
                            # build the matching filename
                            fn = className + ".mp4"
                            resourceLinks.append( (vurl,fn) )

                    except urllib2.HTTPError as e:
                        # sometimes there is a lecture without a vidio (e.g.,
                        # genes-001) so this can happen.
                        print " Warning: failed to open the direct video link %s: %s" % (lurl,e)

                weekClasses[className] = resourceLinks

            # keep track of the list of classNames in the order they appear in the html
            weekClasses['classNames'] = classNames

            allClasses[sanitisedHeaderName] = weekClasses

        return (weeklyTopics, allClasses)

    def get_headers(self,url):
        """
        Get the headers
        """
        r = self.browser.open(url,timeout=self.TIMEOUT)
        return r.info()

    def download(self, url, target_dir=".", target_fname=None):
        """
        Download the url to the given filename
        """

        # get the headers
        headers = self.get_headers(url)

        # get the content length (if present)
        clen = int(headers.get('Content-Length',-1))

        # build the absolute path we are going to write to
        fname = target_fname or filename_from_header(headers) or filename_from_url(url)

        # split off the extension
        _,ext = path.splitext(fname)

        # check if we should skip it (remember to remove the leading .)
        if ext and ext[1:] in self.ignorefiles:
            print '    - skipping "%s" (extension ignored)' % fname 
            return

        filepath = path.join(target_dir,fname)

        if self.max_path_len:
            filepath = trim_path(filepath,max_path_len=self.max_path_len)

        dl = True
        if path.exists(filepath):
            if clen > 0: 
                fs = path.getsize(filepath)
                delta = clen - fs

                # all we know is that the current filesize may be shorter than it should be and the content length may be incorrect
                # overwrite the file if the reported content length is bigger than what we have already by at least k bytes (arbitrary)

                # TODO this is still not foolproof as the fundamental problem is that the content length cannot be trusted
                # so this really needs to be avoided and replaced by something else, eg., explicitly storing what downloaded correctly
                if delta > 2:
                    print '    - "%s" seems incomplete, downloading again' % fname
                else:
                    print '    - "%s" already exists, skipping' % fname
                    dl = False
            else:
                # missing or invalid content length
                # assume all is ok...
                dl = False
        else:
            # Detect renamed files
            existing, short = find_renamed(filepath, clen)
            if existing:
                print '    - "%s" seems to be a copy of "%s", renaming existing file' % (fname, short)
                os.rename(existing, filepath)
                dl = False

        try:
            if dl:
                self.browser.retrieve(url,filepath,timeout=self.TIMEOUT)
        except Exception as e:
            print "Failed to download url %s to %s: %s" % (url,filepath,e)

    def download_course(self,cname,dest_dir=".",reverse_sections=False):
        """
        Download all the contents (quizzes, videos, lecture notes, ...) of the course to the given destination directory (defaults to .)
        """
        # open the main class page
        self.browser.open(self.AUTH_URL % cname,timeout=self.TIMEOUT)

        # get the lecture url
        course_url = self.lecture_url_from_name(cname)

        (weeklyTopics, allClasses) = self.get_downloadable_content(course_url)

        if not weeklyTopics:
            print " Warning: no downloadable content found for %s, did you accept the honour code?" % cname
            return
        else:
            print '* Got all downloadable content for ' + cname

        if reverse_sections:
            weeklyTopics.reverse()
            print "* Sections reversed"

        course_dir = path.abspath(path.join(dest_dir,cname))

        # ensure the target dir exists
        if not path.exists(course_dir):
            os.mkdir(course_dir)

        print "* " + cname + " will be downloaded to " + course_dir

        # ensure the course directory exists
        if not path.exists(course_dir):
            os.makedirs(course_dir)

        # download the standard pages
        print " - Downloading lecture/syllabus pages"
        self.download(self.HOME_URL % cname,target_dir=course_dir,target_fname="index.html")
        self.download(course_url,target_dir=course_dir,target_fname="lectures.html")

        # now download the actual content (video's, lecture notes, ...)
        for j,weeklyTopic in enumerate(weeklyTopics,start=1):
            if weeklyTopic not in allClasses:
                #TODO: refactor
                print 'Warning: Weekly topic not in all classes:', weeklyTopic
                continue

            # ensure the week dir exists
            # add a numeric prefix to the week directory name to ensure chronological ordering
            wkdirname = str(j).zfill(2) + " - " + weeklyTopic
            wkdir = path.join(course_dir,wkdirname)
            if not path.exists(wkdir):
                os.makedirs(wkdir)

            weekClasses = allClasses[weeklyTopic]
            classNames = weekClasses['classNames']

            print " - " + weeklyTopic

            for i,className in enumerate(classNames,start=1):
                if className not in weekClasses:
                    #TODO: refactor
                    print "Warning:",className,"not in",weekClasses.keys()
                    continue

                classResources = weekClasses[className]

                # ensure the class dir exists
                clsdirname = str(i).zfill(2) + " - " + className
                clsdir = path.join(wkdir,clsdirname)
                if not path.exists(clsdir): 
                    os.makedirs(clsdir)

                print "  - Downloading resources for " + className

                for classResource,tfname in classResources:
                    if not isValidURL(classResource):
                        absoluteURLGen = AbsoluteURLGen(course_url)
                        classResource = absoluteURLGen.get_absolute(classResource)
                        print "  -" + classResource, ' - is not a valid url'

                        if not isValidURL(classResource):
                            print "  -" + classResource, ' - is not a valid url'
                            continue

                    try:
                       #print '  - Downloading ', classResource
                       self.download(classResource,target_dir=clsdir,target_fname=tfname)
                    except Exception as e:
                       print "    - failed: ",classResource,e


def filename_from_header(header):
    try:
        cd = header['Content-Disposition']
        pattern = 'attachment; filename="(.*?)"'
        m = re.search(pattern, cd)
        g = m.group(1)
        return sanitise_filename(g)
    except Exception:
        return ''

def filename_from_url(url):
    # parse the url into its components
    u = urlsplit(url)

    # split the path into parts and unquote
    parts = [urllib2.unquote(x).strip() for x in u.path.split('/')]

    # take the last component as filename
    fname = parts[-1]

    # if empty, url ended with a trailing slash
    # so join up the hostnam/path  and use that as a filename
    if len(fname) < 1:
        s = u.netloc + u.path[:-1]
        fname = s.replace('/','_')
    else:
        # unquoting could have cuased slashes to appear again
        # split and take the last element if so
        fname = fname.split('/')[-1]

    # add an extension if none
    ext = path.splitext(fname)[1]
    if len(ext) < 1 or len(ext) > 5: fname += ".html"

    # remove any illegal chars and return
    return sanitise_filename(fname)

def sanitise_filename(fileName):
    # ensure a clean, valid filename (arg may be both str and unicode)

    # ensure a unicode string, problematic ascii chars will get removed
    if isinstance(fileName,str):
        fn = unicode(fileName,errors='ignore')
    else:
        fn = fileName

    # normalize it
    fn = unicodedata.normalize('NFKD',fn)

    # encode it into ascii, again ignoring problematic chars
    s = fn.encode('ascii','ignore')

    # remove any characters not in the whitelist
    s = re.sub('[^\w\-\(\)\[\]\., ]','',s).strip()

    # ensure it is within a sane maximum
    max = 250

    # split off extension, trim, and re-add the extension
    fn,ext = path.splitext(s)
    s = fn[:max-len(ext)] + ext

    return s
    
def trim_path(pathname, max_path_len=255, min_len=5):
    """
    Trim file name in given path name to fit max_path_len characters. Only file name is trimmed,
    path names are not affected to avoid creating multiple folders for the same lecture.
    """
    if len(pathname) <= max_path_len:
        return pathname

    fpath, name = path.split(pathname)
    name, ext = path.splitext(name)

    to_cut = len(pathname) - max_path_len
    to_keep = len(name) - to_cut

    if to_keep < min_len:
        print ' Warning: Cannot trim filename "%s" to fit required path length (%d)' % (pathname, max_path_len)
        return pathname

    name = name[:to_keep]
    new_pathname = path.join(fpath, name + ext)
    print ' Trimmed path name "%s" to "%s" to fit required length (%d)' % (pathname, new_pathname, max_path_len)

    return new_pathname


# TODO: simplistic
def isValidURL(url):
    return url.startswith('http') or url.startswith('https')

# TODO: is this really still needed
class AbsoluteURLGen(object):
    """
    Generate absolute URLs from relative ones
    Source: AbsoluteURLGen copy pasted from http://www.python-forum.org/pythonforum/viewtopic.php?f=5&t=12515
    """
    def __init__(self, base='', replace_base=False):
        self.replace_base = replace_base
        self.base_regex = re.compile('^(https?://)(.*)$')
        self.base = self.normalize_base(base)
   
    def normalize_base(self, url):
        base = url
        if self.base_regex.search(base):
            # rid thyself of 'http(s)://'
            base = self.base_regex.search(url).group(2)
            if not base.rfind('/') == -1:
                # keep only the directory, not the filename
                base = base[:base.rfind('/')+1]
            base = self.base_regex.search(url).group(1) + base
        return base

    def get_absolute(self, url=''):
        if not self.base or (
                self.replace_base and self.base_regex.search(url)):
            self.base = self.normalize_base(url)
            return url
        elif self.base_regex.search(url):
            # it's an absolute url, but we don't want to keep it's base
            return url
        else:
            # now, it's time to do some converting.
            if url.startswith("../"):
                # they want the parent dir
                if not self.base[:-2].rfind("/") == -1:
                    base = self.base[:self.base[:-2].rfind("/")+1]
                    return base + url[3:]
                else:
                    # there are no subdirs... broken link?
                    return url
            elif url.startswith("/"):
                # file is in the root dir
                protocol, base = self.base_regex.search(self.base).groups()
                # remove subdirs until we're left with the root
                while not base[:-2].rfind("/") == -1:
                    base = base[:base[:-2].rfind('/')]
                return protocol + base + url
            else:
                if url.startswith("./"):
                    url = url[2:]
                return self.base + url

def get_netrc_creds():
    """
    Read username/password from the users' netrc file. Returns None if no
    coursera credentials can be found.
    """
    # inspired by https://github.com/jplehmann/coursera

    if platform.system() == 'Windows':
        # where could the netrc file be hiding, try a number of places 
        env_vars = ["HOME","HOMEDRIVE", "HOMEPATH","USERPROFILE","SYSTEMDRIVE"]
        env_dirs = [os.environ[e] for e in env_vars if os.environ.get(e,None)]

        # also try the root/cur dirs
        env_dirs += ["C:", ""]

        # possible filenames
        file_names = [".netrc", "_netrc"]

        # all possible paths
        paths = [path.join(dir,fn) for dir in env_dirs for fn in file_names]
    else:
        # on *nix just put None, and the correct default will be used
        paths = [None]

    # try the paths one by one and return the first one that works
    creds = None
    for p in paths:
        try:
            auths = netrc.netrc(p).authenticators('coursera-dl')
            creds = (auths[0], auths[2])
            print "Credentials found in .netrc file"
            break
        except (IOError, TypeError, netrc.NetrcParseError) as e:
            pass

    return creds


def normalize_string(str):
    return ''.join(x for x in str if x not in ' \t-_()"01234567890').lower()


def find_renamed(filename, size):
    fpath, name = path.split(filename)
    name, ext = path.splitext(name)
    name = normalize_string(name)

    if not path.exists(fpath):
        return None, None

    files = os.listdir(fpath)
    if files:
        for f in files:
            fname, fext = path.splitext(f)
            fname = normalize_string(fname)
            if fname == name and fext == ext:
                fullname = os.path.join(fpath, f)
                if path.getsize(fullname) == size:
                    return fullname, f

    return None, None


def main():
    # parse the commandline arguments
    parser = argparse.ArgumentParser(description='Download Coursera.org course videos/docs for offline use.')
    parser.add_argument("-u", dest='username', type=str, help='coursera username (.netrc used if omitted)')
    parser.add_argument("-p", dest='password', type=str, help='coursera password')
    parser.add_argument("-d", dest='dest_dir', type=str, default=".", help='destination directory where everything will be saved')
    parser.add_argument("-n", dest='ignorefiles', type=str, default="", help='comma-separated list of file extensions to skip, e.g., "ppt,srt,pdf"')
    parser.add_argument("-q", dest='parser', type=str, default=CourseraDownloader.DEFAULT_PARSER,
                        help="the html parser to use, see http://www.crummy.com/software/BeautifulSoup/bs4/doc/#installing-a-parser")
    parser.add_argument("-x", dest='proxy', type=str, default=None, help="proxy to use, e.g., foo.bar.com:3125")
    parser.add_argument("--reverse-sections", dest='reverse', action="store_true",
                        default=False, help="download and save the sections in reverse order")
    parser.add_argument('course_names', nargs="+", metavar='<course name>',
                        type=str, help='one or more course names from the url (e.g., comnets-2012-001)')
    parser.add_argument("--trim-path", dest='trim_path', action='store_true', default=True, help='Trim path names to fit OS constraints (windows only)')
    args = parser.parse_args()

    # check the parser
    html_parser = args.parser
    if html_parser == "html.parser" and sys.version_info < (2,7,3):
        print " Warning: built-in 'html.parser' may cause problems on Python < 2.7.3"

    print "Coursera-dl v%s (%s)" % (_version.__version__,html_parser)

    # search for login credentials in .netrc file if username hasn't been provided in command-line args
    username, password = args.username, args.password
    if not username:
        creds = get_netrc_creds()
        if not creds:
            raise Exception("No username passed and no .netrc credentials found, unable to login")
        else:
            username, password = creds
    else:
        # prompt the user for his password if not specified
        if not password:
            password = getpass.getpass()

    # should we be trimming paths?
    max_path_len = None
    if args.trim_path:
        if platform.system() == "Windows":
            max_path_len = 257
            print "Maximum path length set to %s" % max_path_len
        else:
            # linux max path length is typically around 4060 so assume thats ok
            pass   
 
    # instantiate the downloader class
    d = CourseraDownloader(username,password,proxy=args.proxy,parser=html_parser,ignorefiles=args.ignorefiles,max_path_len=max_path_len)

    # authenticate, only need to do this once but need a classaname to get hold
    # of the csrf token, so simply pass the first one
    print "Logging in as '%s'..." % username
    d.login(args.course_names[0])

    # download the content
    for i,cn in enumerate(args.course_names,start=1):
        print
        print "Course %s of %s" % (i,len(args.course_names))
        d.download_course(cn,dest_dir=args.dest_dir,reverse_sections=args.reverse)

if __name__ == '__main__':
    main()
