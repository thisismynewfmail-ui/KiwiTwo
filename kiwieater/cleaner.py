"""Structural HTML cleaning.

The goal is to preserve the *structure and own content* of each page —
threads, posts, navigation, gallery markup, the site's own stylesheets — while
dropping everything extraneous or external: scripts, ad/analytics/challenge
containers, inline event handlers, cross-site embeds and resource hints.

URLs are absolutised (and kept absolute, in-scope) so the serving layer and the
standalone viewer can localise them later without re-resolving against a base.
"""

import re

from bs4 import BeautifulSoup

from . import config
from .urls import normalize_url, in_scope

# Markers that betray ad / tracking / consent / anti-bot containers.
#
# These are matched against whole id/class *tokens*, not as raw substrings of
# the joined attribute string.  Substring matching is what made earlier backups
# skeletal: the bare fragment ``ad-`` is contained in ``thread-list`` (thre-AD-
# list), ``upload-…`` and the like, so on a forum built entirely around
# *threads* the junk filter silently decomposed every thread/post block — and,
# because decomposing a populated container then revisiting its freed children
# raised ``AttributeError``, often failed the whole page outright.

# Distinctive fragments that only ever occur in ad/tracking/consent/challenge
# markup — safe to match anywhere inside a token.
_JUNK_CONTAINS = ("advert", "adsbygoogle", "analytics", "doubleclick",
                  "googletag", "gtm", "challenge", "kiwiflare", "turnstile",
                  "onetrust", "cookieconsent", "cookie-banner", "consent")

# The bare "ad" family, matched only at token boundaries so legitimate names
# (thread-list, upload, header, breadcrumb, download, load-more) are never
# mistaken for ads.
_JUNK_AD_EXACT = {"ad", "ads", "adbox", "adunit", "adslot", "adsense"}
_JUNK_AD_PREFIX = ("ad-", "ads-", "ad_", "ads_", "cf-", "cf_chl", "consent-")
_JUNK_AD_SUFFIX = ("-ad", "-ads", "_ad", "_ads")


def _is_junk_token(tok):
    """True if a single id/class token marks ad/tracking/consent/challenge
    markup.  Token-boundary aware so it never fires on ``thread-…`` & friends."""
    if not tok:
        return False
    if any(j in tok for j in _JUNK_CONTAINS):
        return True
    if tok in _JUNK_AD_EXACT:
        return True
    return tok.startswith(_JUNK_AD_PREFIX) or tok.endswith(_JUNK_AD_SUFFIX)


def _is_junk_el(el):
    """True if an element's id or any class token marks it as junk to strip."""
    idv = el.get("id")
    if idv and _is_junk_token(str(idv).lower()):
        return True
    return any(_is_junk_token(str(c).lower()) for c in (el.get("class") or []))


_DROP_LINK_REL = ("preconnect", "dns-prefetch", "preload", "prefetch",
                  "modulepreload")

# CSS reference parsers — used to pull every theme asset a stylesheet (or an
# inline ``<style>`` / ``style=""``) pulls in: ``url(...)`` for fonts, icon
# sprites, background textures and smilies, and ``@import`` for sub-stylesheets.
_CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)(?P<u>[^)'"]+)\1\s*\)""", re.I)
_CSS_IMPORT_RE = re.compile(
    r"""@import\s+(?:url\(\s*(['"]?)(?P<u1>[^)'"]+)\1\s*\)"""
    r"""|(['"])(?P<u2>[^'"]+)\3)""", re.I)


def _css_ref_ok(ref):
    """Keep only refs we can localise: not data URIs, not bare svg fragments."""
    ref = (ref or "").strip()
    if not ref or ref.startswith(("data:", "#")):
        return None
    return ref


def extract_css_refs(css_text, base_url):
    """Return ``(imports, assets)``: the in-scope absolute URLs of ``@import``
    targets and ``url(...)`` references in a chunk of CSS.

    Both lists are de-duplicated and absolutised against ``base_url`` so the
    crawler can fetch the fonts/sprites/backgrounds a theme depends on and the
    viewer can localise them — without this, captured stylesheets reference
    assets that were never archived and the page renders unthemed.
    """
    imports, assets, seen = [], [], set()
    for m in _CSS_IMPORT_RE.finditer(css_text or ""):
        ref = _css_ref_ok(m.group("u1") or m.group("u2"))
        if not ref:
            continue
        u = normalize_url(ref, base_url)
        if u and in_scope(u) and u not in seen:
            seen.add(u)
            imports.append(u)
    for m in _CSS_URL_RE.finditer(css_text or ""):
        ref = _css_ref_ok(m.group("u"))
        if not ref:
            continue
        u = normalize_url(ref, base_url)
        if u and in_scope(u) and u not in seen:
            seen.add(u)
            assets.append(u)
    return imports, assets


def _srcset_candidates(srcset):
    """Yield the URL of each candidate in a ``srcset``/``data-srcset`` list,
    dropping the width/density descriptor that follows it."""
    for part in (srcset or "").split(","):
        part = part.strip()
        if part:
            yield part.split()[0]


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

    # Drop ad/tracking/consent/challenge containers.  Collect the matches first,
    # then decompose, so removing a populated container never strands a child
    # node we are still iterating over (a freed node has ``attrs == None`` and
    # would raise) — and skip anything already freed by an ancestor's removal.
    for el in [e for e in soup.find_all(True) if _is_junk_el(e)]:
        if not getattr(el, "decomposed", False) and el.parent is not None:
            el.decompose()

    # Strip inline event handlers and drop resource-hint <link>s.  Re-query the
    # now-pruned tree so no freed node is ever visited.
    for el in soup.find_all(True):
        for attr in [a for a in el.attrs if a.startswith("on")]:
            del el[attr]                          # inline event handlers
        if el.name == "link":
            rel = " ".join(el.get("rel", []) or []).lower()
            if any(r in rel for r in _DROP_LINK_REL):
                el.decompose()

    asset_urls = set()

    # Images: normalise lazy-load variants (including srcset) to one in-scope src.
    for img in soup.find_all("img"):
        chosen = None
        for attr in ("src", "data-src", "data-url", "data-original"):
            if img.get(attr):
                u = normalize_url(img[attr], base_url)
                if u and in_scope(u):
                    chosen = u
                    break
        if not chosen:
            for cand in _srcset_candidates(img.get("srcset")
                                           or img.get("data-srcset")):
                u = normalize_url(cand, base_url)
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
        # <picture>/<source> responsive candidates: collapse to one in-scope URL.
        if media.get("srcset"):
            picked = None
            for cand in _srcset_candidates(media["srcset"]):
                u = normalize_url(cand, base_url)
                if u and in_scope(u):
                    picked = u
                    break
            if picked:
                media["srcset"] = picked
                asset_urls.add(picked)
            else:
                del media["srcset"]
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

    # Inline <style> blocks and style="" attributes pull in theme assets via
    # url(...)/@import too; harvest the in-scope ones so they are archived.
    for style in soup.find_all("style"):
        imp, refs = extract_css_refs(style.get_text() or "", base_url)
        asset_urls.update(imp)
        asset_urls.update(refs)
    for el in soup.find_all(style=True):
        imp, refs = extract_css_refs(el.get("style") or "", base_url)
        asset_urls.update(imp)
        asset_urls.update(refs)

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
