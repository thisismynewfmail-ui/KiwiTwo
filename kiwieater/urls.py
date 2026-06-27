"""URL helpers: normalisation, scope checks and asset detection.

Keeping these pure (no I/O, no globals beyond config constants) makes the
crawler and the server agree on exactly what counts as the same page.
"""

import re
import hashlib
from urllib.parse import urlparse, urljoin, urldefrag, parse_qsl, urlencode

from . import config


def normalize_url(url, base=None):
    """Resolve ``url`` against ``base``, drop fragments, normalise host and
    trailing slash.  Returns ``None`` for anything we never want to follow
    (javascript:, mailto:, data:, bare fragments, non-http schemes)."""
    if not url:
        return None
    url = url.strip()
    if url.startswith(("javascript:", "mailto:", "tel:", "data:", "#",
                       "blob:", "about:")):
        return None
    if base:
        url = urljoin(base, url)
    url, _frag = urldefrag(url)
    try:
        p = urlparse(url)
    except Exception:
        return None
    if p.scheme not in ("http", "https"):
        return None
    host = (p.netloc or "").lower().replace(":80", "").replace(":443", "")
    path = p.path or "/"
    # Drop trailing slashes (including the bare root "/") so the root and every
    # page have one canonical key.  This must match the viewer's JS stripSlash.
    if path == "/":
        path = ""
    elif path.endswith("/"):
        path = path.rstrip("/")
    rebuilt = f"{p.scheme}://{host}{path}"
    if p.query:
        rebuilt += "?" + p.query
    return rebuilt


def in_scope(url):
    """True only for kiwifarms.st and its sub-domains — the tool's entire
    purpose is this one site, so the scope rule is deliberately strict."""
    try:
        host = (urlparse(url).netloc or "").lower().split(":")[0]
    except Exception:
        return False
    return host == config.TARGET_HOST or host.endswith("." + config.TARGET_HOST)


def looks_like_asset(url):
    """Heuristic: does the path end in a known static-asset extension?"""
    try:
        path = urlparse(url).path.lower()
    except Exception:
        return False
    return path.endswith(config.ASSET_EXT)


def url_key(url):
    """Stable short filename-safe key for a URL (used for page JSON files)."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]


# --------------------------------------------------------------------------- #
#  Section / pagination model
#
#  These are what make depth *dynamic and section-relative* instead of a flat
#  hop-count.  A "section" is one thread/forum/listing; its pages all share a
#  single ``section_key``.  Pagination (``/page-N``) is treated as a deepening
#  of the same trail, so digging through a thread's pages consumes depth one
#  page at a time — e.g. ``/threads/kino-casino.110845/page-65`` sits 64 pages
#  deeper than the thread's first page along the navigation trail.
# --------------------------------------------------------------------------- #

# XenForo (KiwiFarms) paginates path-style: ".../page-N".  We also tolerate a
# "?page=N" query just in case.
_PAGE_PATH_RE = re.compile(r"^(?P<base>.*?)/page-(?P<n>\d+)$")
# A specific thread: /threads/<slug>.<id> — a *content* section whose pages we
# dig through deeply.  A forum is a *listing* hub we fan out from.
_THREAD_RE = re.compile(r"^/threads/[^/]+\.\d+(?:/|$)")
_FORUM_RE = re.compile(r"^/forums/[^/]+\.\d+(?:/|$)")


def split_pagination(url):
    """Split a URL into ``(section_base, page_number)``.

    ``section_base`` is the page-independent address of a thread/forum/listing
    (``https://kiwifarms.st/threads/kino-casino.110845``); ``page_number`` is 1
    for the first page, 65 for ``…/page-65``.  All pages of one section share a
    base, which is what lets a crawl follow pagination as one continuous trail.
    """
    if not url:
        return url, 1
    p = urlparse(url)
    path = p.path or ""
    page = 1
    m = _PAGE_PATH_RE.match(path)
    if m:
        path = m.group("base") or "/"
        page = int(m.group("n"))
    kept = []
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        if k == "page" and v.isdigit():
            page = int(v)
        else:
            kept.append((k, v))
    rebuilt = f"{p.scheme}://{p.netloc}{path}"
    if kept:
        rebuilt += "?" + urlencode(kept)
    return (normalize_url(rebuilt) or rebuilt), page


def section_key(url):
    """The page-independent identity of the subsection a URL belongs to, so all
    pages of one thread/forum collapse to a single key."""
    return split_pagination(url)[0]


def page_number(url):
    """The 1-based page number within the URL's section."""
    return split_pagination(url)[1]


def page_url(base, n):
    """Build the Nth page URL for a section base (path-style ``/page-N``)."""
    if n <= 1:
        return base
    return base.rstrip("/") + "/page-" + str(int(n))


def next_page_url(url):
    """The next page within the same section (page-2 → page-3, base → page-2)."""
    base, page = split_pagination(url)
    return page_url(base, page + 1)


def is_thread(url):
    """True for a specific thread — a *content* section whose pagination we dig
    through deeply, as opposed to a forum/index *listing* we fan out from."""
    try:
        path = urlparse(url).path or ""
    except Exception:
        return False
    return bool(_THREAD_RE.match(path))


def is_forum(url):
    """True for a specific forum listing (``/forums/<slug>.<id>``)."""
    try:
        path = urlparse(url).path or ""
    except Exception:
        return False
    return bool(_FORUM_RE.match(path))


def child_trail(parent_trail, ordinal, width=6):
    """Materialised-path token for a child node.

    Appending zero-padded ordinals makes a plain ``ORDER BY trail`` reproduce a
    depth-first (spiderweb) pre-order — dive into the first child's whole
    subtree, then the next sibling — which is exactly the traversal *and* the
    resume order we want.  Fixed-width segments keep the lexical sort numeric.
    """
    return f"{parent_trail}.{int(ordinal):0{width}d}"


# --------------------------------------------------------------------------- #
#  Focused (sub-section) crawling
#
#  A "focus" lets the operator point the crawler at one section
#  (``…/forums/lolcows.16``) instead of the whole site.  Two helpers make that
#  work while keeping the saved copy naturally navigable and never duplicated:
#
#  * ``focus_chain`` gives the breadcrumb path from the main page down to the
#    section, so those ancestor pages get archived first and the section can be
#    reached by clicking through the backup exactly like the live site.
#  * ``within_focus`` confines the spiderweb so it explores *inside* the focused
#    section and does not climb back up to the home/ancestor pages or wander off
#    into sibling sections.
# --------------------------------------------------------------------------- #

def host_root(url):
    """The site's main-page/origin URL (``scheme://host``) for any in-scope URL,
    normalised the same way every other key is (no path, no trailing slash)."""
    p = urlparse(url)
    return normalize_url(f"{p.scheme}://{p.netloc}/")


def focus_chain(focus_url):
    """Ordered ancestor chain from the main page down to ``focus_url`` inclusive.

    Each step adds one path segment, so focusing on ``…/forums/lolcows.16``
    yields ``[main page, …/forums, …/forums/lolcows.16]``.  Archiving the chain
    before spiderwebbing the focus is what lets the saved copy be navigated
    naturally from the main page straight down to the focused section.
    """
    base = section_key(focus_url)          # drop pagination + trailing slash
    p = urlparse(base)
    chain = [host_root(base)]
    accum = ""
    for seg in [s for s in p.path.split("/") if s]:
        accum += "/" + seg
        chain.append(normalize_url(f"{p.scheme}://{p.netloc}{accum}"))
    return chain


def focus_path_of(focus_url):
    """The path that bounds a focused crawl (e.g. ``/forums/lolcows.16``), or
    ``None`` for the main page / an empty focus — both of which mean "no focus,
    crawl the whole site"."""
    if not focus_url:
        return None
    path = urlparse(section_key(focus_url)).path
    return path or None


def within_focus(parent_url, child_url, focus_path):
    """Confinement test for a *descent* (a link into a different section) while a
    focus is active.  Returns ``True`` if ``child_url`` belongs inside the
    focused section and so should be followed.

    Kept inside the focus:

    * a child whose section is the focus itself or sits beneath it in the URL
      tree (this is what makes focusing on ``/forums`` pull in every
      ``/forums/<slug>`` listing), and
    * a thread linked from an in-focus *listing* (a forum/index): a forum's
      threads live under ``/threads/`` yet are that forum's actual content.

    Dropped: climbing back to the home/ancestor sections, wandering to sibling
    forums, and threads linked from other threads — so the spiderweb stays put.
    ``focus_path is None`` disables confinement (whole-site crawl).
    """
    if focus_path is None:
        return True
    child_path = urlparse(section_key(child_url)).path
    if child_path == focus_path or child_path.startswith(focus_path + "/"):
        return True
    if not is_thread(parent_url) and is_thread(child_url):
        return True
    return False
