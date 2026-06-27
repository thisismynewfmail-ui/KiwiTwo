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
