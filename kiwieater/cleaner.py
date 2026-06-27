"""Structural HTML cleaning.

The goal is to preserve the *structure and own content* of each page —
threads, posts, navigation, gallery markup, the site's own stylesheets — while
dropping everything extraneous or external: scripts, ad/analytics/challenge
containers, inline event handlers, cross-site embeds and resource hints.

URLs are absolutised (and kept absolute, in-scope) so the serving layer and the
standalone viewer can localise them later without re-resolving against a base.
"""

from bs4 import BeautifulSoup

from . import config
from .urls import normalize_url, in_scope

# Substrings in id/class that mark ad / tracking / challenge containers.
_JUNK_IDCLASS = ("advert", "-ads", "ad-", "adsbygoogle", "analytics", "gtm-",
                 "cookie-banner", "challenge", "cf-", "kiwiflare", "turnstile",
                 "onetrust", "consent")

_DROP_LINK_REL = ("preconnect", "dns-prefetch", "preload", "prefetch",
                  "modulepreload")


def _soup(html):
    try:
        return BeautifulSoup(html or "", "lxml")
    except Exception:
        return BeautifulSoup(html or "", "html.parser")


def clean_html(html, base_url):
    """Return ``(cleaned_html, plain_text, asset_urls, links)``.

    ``asset_urls`` is a set of in-scope media/stylesheet URLs to fetch as
    BLOBs; ``links`` is a list of ``{href, text, internal}`` records describing
    every anchor so navigation can be reconstructed and audited.
    """
    soup = _soup(html)

    for tag in soup(list(config.STRIP_TAGS)):
        tag.decompose()

    for el in soup.find_all(True):
        cid = " ".join(filter(None, [
            el.get("id", ""), " ".join(el.get("class", []) or [])])).lower()
        if any(k in cid for k in _JUNK_IDCLASS):
            el.decompose()
            continue
        for attr in list(el.attrs):
            if attr.startswith("on"):           # inline event handlers
                del el[attr]
        if el.name == "link":
            rel = " ".join(el.get("rel", []) or []).lower()
            if any(r in rel for r in _DROP_LINK_REL):
                el.decompose()

    asset_urls = set()

    # Images: normalise lazy-load variants down to a single in-scope src.
    for img in soup.find_all("img"):
        chosen = None
        for attr in ("src", "data-src", "data-url", "data-original"):
            if img.get(attr):
                u = normalize_url(img[attr], base_url)
                if u and in_scope(u):
                    chosen = u
                    break
        if chosen:
            img["src"] = chosen
            asset_urls.add(chosen)
        for junk in ("srcset", "data-srcset", "data-src", "data-url",
                     "data-original"):
            if img.get(junk):
                del img[junk]
        if not img.get("loading"):
            img["loading"] = "lazy"

    for media in soup.find_all(["video", "audio", "source"]):
        if media.get("src"):
            u = normalize_url(media["src"], base_url)
            if u and in_scope(u):
                media["src"] = u
                asset_urls.add(u)
        if media.get("poster"):
            u = normalize_url(media["poster"], base_url)
            if u and in_scope(u):
                media["poster"] = u
                asset_urls.add(u)

    # Same-domain stylesheets keep the archive looking like the site.
    for link in soup.find_all("link", rel=lambda r: r and "stylesheet" in r):
        if link.get("href"):
            u = normalize_url(link["href"], base_url)
            if u and in_scope(u):
                link["href"] = u
                asset_urls.add(u)
            else:
                link.decompose()

    # Anchors: absolutise; record structure for navigation/audit.
    links = []
    for a in soup.find_all("a", href=True):
        u = normalize_url(a["href"], base_url)
        if not u:
            continue
        a["href"] = u
        links.append({"href": u,
                      "text": a.get_text(" ", strip=True)[:120],
                      "internal": in_scope(u)})

    text = soup.get_text(" ", strip=True)
    return str(soup), text, asset_urls, links
