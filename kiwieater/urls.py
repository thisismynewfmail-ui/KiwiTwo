"""URL helpers: normalisation, scope checks and asset detection.

Keeping these pure (no I/O, no globals beyond config constants) makes the
crawler and the server agree on exactly what counts as the same page.
"""

import hashlib
from urllib.parse import urlparse, urljoin, urldefrag

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
