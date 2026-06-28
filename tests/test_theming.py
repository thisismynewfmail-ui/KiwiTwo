"""Network-free tests for the theming / asset-capture repair.

These cover the pipeline that makes an archived page look like the real site
instead of skeletal, unstyled HTML:

* ``extract_css_refs`` — finding the fonts/sprites/backgrounds/@imports a
  stylesheet depends on.
* ``clean_html`` — keeping inline SVG (logo/icons) and harvesting theme assets
  from inline ``<style>``/``style=""`` and ``srcset``.
* ``Crawler._grab_assets`` — recursively downloading every asset a stylesheet
  pulls in (so a captured theme is complete, not a stylesheet pointing at
  assets that were never saved), driven against a synthetic in-memory "site"
  with no browser, no Flask and no kiwifarms.st access.
"""

import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kiwieater import config
from kiwieater.cleaner import clean_html, extract_css_refs, _is_junk_token


ROOT = "https://kiwifarms.st/"
CSS_URL = "https://kiwifarms.st/css.php?css=public:core.css&d=1"


def _point_config_at(tmp):
    config.DATA_DIR = os.path.join(tmp, "data")
    config.PROFILE_DIR = os.path.join(config.DATA_DIR, "browser_profile")
    config.LOG_DIR = os.path.join(config.DATA_DIR, "logs")
    config.STATE_DB = os.path.join(config.DATA_DIR, "state.db")
    config.ARCHIVE_DIR = os.path.join(tmp, "Archive")
    config.PAGES_DIR = os.path.join(config.ARCHIVE_DIR, "pages")
    config.BLOBS_DIR = os.path.join(config.ARCHIVE_DIR, "blobs")
    config.VIEWER_DIR = os.path.join(config.ARCHIVE_DIR, "viewer")
    config.MANIFEST_PATH = os.path.join(config.ARCHIVE_DIR, "manifest.json")
    config.SEARCH_INDEX_PATH = os.path.join(config.ARCHIVE_DIR, "search_index.json")
    config.GALLERY_PATH = os.path.join(config.ARCHIVE_DIR, "gallery.json")
    config.BLOB_INDEX_PATH = os.path.join(config.BLOBS_DIR, "blob_index.json")
    config.ensure_dirs()


class CssExtraction(unittest.TestCase):
    def test_finds_imports_and_url_refs_in_scope_only(self):
        css = (
            '@import "sub.css";\n'
            '@import url(/themes/extra.css);\n'
            '@font-face{src:url("fonts/x.woff2") format("woff2"),'
            'url(/data/x.ttf?v=2#iefix)}\n'
            ".smilie{background:url('/data/smilies/smile.png')}\n"
            ".ext{background:url(https://cdn.example.com/track.png)}\n"
            ".data{background:url(data:image/gif;base64,AAAA)}\n"
            ".frag{background:url(#gradient)}\n"
        )
        imports, assets = extract_css_refs(css, CSS_URL)
        self.assertEqual(imports, ["https://kiwifarms.st/sub.css",
                                   "https://kiwifarms.st/themes/extra.css"])
        # Relative font path resolves against the stylesheet URL; #iefix dropped.
        self.assertIn("https://kiwifarms.st/fonts/x.woff2", assets)
        self.assertIn("https://kiwifarms.st/data/x.ttf?v=2", assets)
        self.assertIn("https://kiwifarms.st/data/smilies/smile.png", assets)
        # External host, data: URIs and bare #fragments are never captured.
        joined = " ".join(imports + assets)
        self.assertNotIn("cdn.example.com", joined)
        self.assertNotIn("data:image", joined)
        self.assertNotIn("#gradient", joined)


class CleanerTheme(unittest.TestCase):
    def test_keeps_svg_and_harvests_theme_assets(self):
        html = (
            '<html><head>'
            '<link rel="preload" href="/x.js" as="script">'
            '<link rel="stylesheet" href="/css.php?css=public:core.css&amp;d=1">'
            '<style>.b{background:url(/data/inline-bg.png)}</style>'
            '</head><body>'
            '<svg class="logo"><use href="#kiwi"></use></svg>'
            '<img src="/data/logo.png">'
            '<picture><source srcset="/data/h1.png 1x, /data/h2.png 2x"></picture>'
            '<div style="background:url(/data/attr-bg.png)">x</div>'
            '</body></html>'
        )
        cleaned, _text, assets, _links = clean_html(html, ROOT)
        self.assertIn("<svg", cleaned)                    # logo/icons survive
        self.assertIn("https://kiwifarms.st/css.php?css=public:core.css&d=1",
                      assets)
        self.assertIn("https://kiwifarms.st/data/inline-bg.png", assets)
        self.assertIn("https://kiwifarms.st/data/attr-bg.png", assets)
        self.assertIn("https://kiwifarms.st/data/logo.png", assets)
        self.assertIn("https://kiwifarms.st/data/h1.png", assets)   # srcset
        # The preload resource hint is dropped, not turned into an asset.
        self.assertNotIn("https://kiwifarms.st/x.js", assets)

    def test_attachment_lightbox_target_is_captured_but_profile_links_are_not(self):
        # A thumbnail links to its full-size attachment (the actual posted media,
        # which is not an <img src> and carries no file extension) — that target
        # must be harvested so the full image lands in the archive.  The
        # profile/member link wrapped around an avatar must NOT be mistaken for
        # media.
        html = (
            '<html><body>'
            '<a href="/members/falur.1/" class="avatar avatar--m">'
            '  <img src="/data/avatars/m/1/1.jpg"></a>'
            '<a href="/attachments/full-png.55501/" class="js-lbImage">'
            '  <img src="/data/attachments/55/55501-thumb.jpg" class="bbImage"></a>'
            '</body></html>'
        )
        _cleaned, _text, assets, _links = clean_html(html, ROOT)
        self.assertIn("https://kiwifarms.st/attachments/full-png.55501", assets)
        self.assertIn("https://kiwifarms.st/data/attachments/55/55501-thumb.jpg",
                      assets)                              # the thumbnail too
        self.assertNotIn("https://kiwifarms.st/members/falur.1", assets)


class JunkFilterIsContentSafe(unittest.TestCase):
    """The ad/tracking/challenge filter must strip junk *without* eating the
    forum's own content.  The original substring filter matched ``ad-`` inside
    ``thread-list`` (and every ``thread-…`` class), so it silently deleted whole
    thread/post blocks — and crashed cleaning a populated container outright —
    which is what left backups skeletal and unthemed."""

    def test_thread_and_forum_tokens_are_never_treated_as_ads(self):
        # The names a forum is actually built from must survive cleaning.
        for tok in ("thread-list", "block--thread-list", "structItem--thread",
                    "js-threadList", "node-thread", "upload", "uploads",
                    "header", "p-header", "breadcrumb", "download", "load-more"):
            self.assertFalse(_is_junk_token(tok),
                             "content token wrongly flagged as junk: " + tok)

    def test_real_ad_tracking_and_challenge_tokens_are_flagged(self):
        for tok in ("ad", "ads", "ad-slot", "ads-top", "header-ad", "advert",
                    "adsbygoogle", "analytics", "gtm-container", "challenge",
                    "cf-wrapper", "cf_chl_opt", "kiwiflare", "turnstile",
                    "onetrust-banner", "cookieconsent"):
            self.assertTrue(_is_junk_token(tok),
                            "junk token not flagged: " + tok)

    def test_thread_list_and_posts_survive_cleaning(self):
        # A forum listing whose container class contains the literal "ad-"
        # (thre-AD-list) plus thread rows and posts — all must remain.
        html = (
            '<html><body>'
            '<div class="block block--thread-list"><div class="block-body">'
            '<div class="structItem structItem--thread">'
            '<div class="structItem-title"><a href="/threads/x.1/">A Thread</a></div>'
            '</div>'
            '<div class="structItem structItem--thread">'
            '<div class="structItem-title"><a href="/threads/y.2/">B Thread</a></div>'
            '</div>'
            '</div></div>'
            '<article class="message message--post"><div class="bbWrapper">'
            'post body text</div></article>'
            '</body></html>'
        )
        cleaned, text, _assets, _links = clean_html(html, ROOT)
        self.assertIn("structItem--thread", cleaned)
        self.assertEqual(cleaned.count("structItem--thread"), 2)
        self.assertIn("A Thread", cleaned)
        self.assertIn("B Thread", cleaned)
        self.assertIn("message--post", cleaned)
        self.assertIn("post body text", text)

    def test_populated_junk_container_is_removed_without_crashing(self):
        # Decomposing a junk container that has descendants must not raise
        # (the freed children used to be revisited mid-iteration → AttributeError)
        # and must not take neighbouring real content with it.
        html = (
            '<html><body>'
            '<div class="adsbygoogle"><span class="inner"><b>buy now</b></span></div>'
            '<div id="ad-slot"><iframe src="/x"></iframe><p>sponsor</p></div>'
            '<div class="message--post"><div class="bbWrapper">keep me</div></div>'
            '</body></html>'
        )
        cleaned, text, _assets, _links = clean_html(html, ROOT)   # must not raise
        self.assertNotIn("adsbygoogle", cleaned)
        self.assertNotIn("buy now", cleaned)
        self.assertNotIn("sponsor", cleaned)
        self.assertIn("keep me", cleaned)


# --------------------------------------------------------------------------- #
#  A synthetic "site" of assets the crawler downloads via a fake requests
#  session (no real HTTP).  CSS files reference fonts, sprites and @imports.
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status, content, content_type):
        self.status_code = status
        self.content = content
        self.headers = {"Content-Type": content_type}


class _FakeSession:
    def __init__(self, site):
        self.site = site
        self.requested = []

    def get(self, url, timeout=None, headers=None):
        self.requested.append(url)
        if url in self.site:
            ct, body = self.site[url]
            data = body.encode("utf-8") if isinstance(body, str) else body
            return _FakeResponse(200, data, ct)
        return _FakeResponse(404, b"", "text/plain")


class CrawlerAssetHarvest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kiwi-theme-")
        _point_config_at(self.tmp)
        from kiwieater.storage import ArchiveStore
        from kiwieater.crawler import Crawler
        self.store = ArchiveStore()
        self.crawler = Crawler(self.store, builder=None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stylesheet_dependencies_are_followed_recursively(self):
        core = "https://kiwifarms.st/css.php?css=public:core.css&d=1"
        sub = "https://kiwifarms.st/css.php?css=public:sub.css&d=1"
        font = "https://kiwifarms.st/fonts/fa.woff2"
        sprite = "https://kiwifarms.st/data/sprite.png"
        bg = "https://kiwifarms.st/data/bg.png"
        site = {
            core: ("text/css; charset=utf-8",
                   '@import url("/css.php?css=public:sub.css&d=1");\n'
                   '.icon{background:url(/fonts/fa.woff2)}\n'
                   '.s{background:url(/data/sprite.png)}'),
            sub: ("text/css", ".body{background:url('/data/bg.png')}"),
            font: ("font/woff2", b"\x00FONTDATA"),
            sprite: ("image/png", b"\x89PNG-sprite"),
            bg: ("image/png", b"\x89PNG-bg"),
        }
        self.crawler.http = _FakeSession(site)

        # The page only knew about the top stylesheet; the rest must be found by
        # mining the CSS itself.
        self.crawler._grab_assets({core}, referer=ROOT)

        for u in (core, sub, font, sprite, bg):
            self.assertTrue(self.store.has_asset(u),
                            "asset not captured: " + u)
        # The sub-stylesheet's own background was reached through the @import.
        self.assertIsNotNone(self.store.get_asset(bg))

    def test_already_stored_css_is_reparsed_on_resume_not_refetched(self):
        core = "https://kiwifarms.st/css.php?css=public:core.css&d=1"
        late = "https://kiwifarms.st/data/late.png"
        site = {
            core: ("text/css", ".x{background:url(/data/late.png)}"),
            late: ("image/png", b"\x89PNG-late"),
        }
        # Pre-seed the CSS as if a previous run had already saved it.
        self.store.save_asset(core, "text/css",
                              b".x{background:url(/data/late.png)}",
                              source_page=ROOT)
        sess = _FakeSession(site)
        self.crawler.http = sess
        self.crawler._grab_assets({core}, referer=ROOT)

        # The CSS was not re-downloaded, but its dependency still got captured.
        self.assertNotIn(core, sess.requested)
        self.assertTrue(self.store.has_asset(late))


class ViewerThemePackaging(unittest.TestCase):
    """The archive must carry, and the viewer must apply, the 1950s theme — this
    is what makes a backup themed instead of skeletal/white."""

    _CFG_KEYS = ("DATA_DIR", "PROFILE_DIR", "LOG_DIR", "STATE_DB",
                 "ARCHIVE_DIR", "PAGES_DIR", "BLOBS_DIR", "VIEWER_DIR",
                 "MANIFEST_PATH", "SEARCH_INDEX_PATH", "GALLERY_PATH",
                 "BLOB_INDEX_PATH")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kiwi-viewer-")
        self._saved = {k: getattr(config, k) for k in self._CFG_KEYS}
        _point_config_at(self.tmp)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read(self, name):
        with open(os.path.join(config.VIEWER_DIR, name), encoding="utf-8") as fh:
            return fh.read()

    def test_theme_is_bundled_into_the_archive(self):
        from kiwieater.archive_builder import ArchiveBuilder
        ArchiveBuilder(store=None)._sync_viewer()
        # Every file the offline viewer needs travels into the backup, including
        # the theme stylesheet — the backup is openable on its own.
        for name in ("index.html", "viewer.js", "viewer.css",
                     "archive-theme.css"):
            self.assertTrue(
                os.path.isfile(os.path.join(config.VIEWER_DIR, name)),
                "viewer bundle missing " + name)

    def test_theme_is_1950s_and_styles_xenforo_structure(self):
        from kiwieater.archive_builder import ArchiveBuilder
        ArchiveBuilder(store=None)._sync_viewer()
        theme = self._read("archive-theme.css")
        # Retro mainframe palette + identity (matches the console aesthetic).
        self.assertIn("--ke-amber", theme)
        self.assertRegex(theme.lower(), r"1950s|mainframe|phosphor")
        # Fleshed-out, not skeletal: the XenForo structures KiwiFarms uses are
        # actually given styling, so threads/forums/posts render deliberately.
        for sel in (".p-nav", ".block-header", ".structItem", ".message",
                    ".bbWrapper", ".pageNav"):
            self.assertIn(sel, theme)

    def test_viewer_drops_site_css_and_injects_the_theme(self):
        from kiwieater.archive_builder import ArchiveBuilder
        ArchiveBuilder(store=None)._sync_viewer()
        js = self._read("viewer.js")
        # The viewer fetches the bundled theme and injects it into each page…
        self.assertIn("archive-theme.css", js)
        self.assertIn("ke-archive-theme", js)
        # …and removes the live site's own stylesheets so a page can't be white.
        self.assertIn("link[rel~='stylesheet']", js)
        # The old white content frame is gone (the regression in the report).
        css = self._read("viewer.css")
        self.assertNotIn("background:#fff", css)

    def test_viewer_links_anchors_to_captured_blobs(self):
        from kiwieater.archive_builder import ArchiveBuilder
        ArchiveBuilder(store=None)._sync_viewer()
        js = self._read("viewer.js")
        # A link straight to a captured asset (a thumbnail's full-size lightbox
        # target) is rewritten to the on-disk BLOB so the media opens from the
        # archive rather than a dead in-archive page route.
        self.assertIn("assetBlob", js)


class _StubAssetBrowser:
    """A no-network ``BrowserEngine`` stand-in that serves asset bytes the way
    the *cleared* browser does (``fetch_binary``), so ``_grab_assets`` can be
    tested preferring the browser over the ``requests`` session."""

    def __init__(self, assets):
        self.assets = assets            # url -> (content_type, bytes) | None
        self.asked = []

    def fetch_binary(self, url, referer=None, timeout=30):
        self.asked.append(url)
        return self.assets.get(url)


class AssetCapturePrefersBrowser(unittest.TestCase):
    """Attachment images live behind the same Kiwiflare gate as the pages, and
    the clearance is bound to the browser that earned it — so assets must be
    pulled through that browser, with ``requests`` only as a fallback.  This is
    the fix for downloaded images that never made it into the archive."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kiwi-asset-")
        _point_config_at(self.tmp)
        from kiwieater.storage import ArchiveStore
        from kiwieater.crawler import Crawler
        self.store = ArchiveStore()
        self.crawler = Crawler(self.store, builder=None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_browser_fetch_is_preferred_over_requests(self):
        img = "https://kiwifarms.st/data/attachments/1/1-a.png"
        self.crawler.browser = _StubAssetBrowser(
            {img: ("image/png", b"\x89PNG\r\n\x1a\nFROM-BROWSER")})
        # The requests session would hand back *different* bytes; it must not be
        # consulted at all when the browser can serve the asset.
        sess = _FakeSession({img: ("image/png", b"\x89PNG\r\n\x1a\nFROM-REQUESTS")})
        self.crawler.http = sess

        self.crawler._grab_assets({img}, referer=ROOT)

        self.assertTrue(self.store.has_asset(img))
        _ct, data = self.store.get_asset(img)
        self.assertEqual(data, b"\x89PNG\r\n\x1a\nFROM-BROWSER")
        self.assertNotIn(img, sess.requested)        # requests left untouched

    def test_falls_back_to_requests_when_the_browser_cannot(self):
        img = "https://kiwifarms.st/data/attachments/1/2-b.jpg"
        # Browser returns None (blocked / failed) -> requests must take over.
        self.crawler.browser = _StubAssetBrowser({img: None})
        sess = _FakeSession({img: ("image/jpeg", b"\xff\xd8\xffFROM-REQUESTS")})
        self.crawler.http = sess

        self.crawler._grab_assets({img}, referer=ROOT)

        self.assertTrue(self.store.has_asset(img))
        _ct, data = self.store.get_asset(img)
        self.assertEqual(data, b"\xff\xd8\xffFROM-REQUESTS")
        self.assertIn(img, sess.requested)           # fell through to requests

    def test_html_challenge_shell_is_never_stored_as_an_asset(self):
        img = "https://kiwifarms.st/data/attachments/1/3-c.png"
        self.crawler.browser = None                  # no browser attached
        sess = _FakeSession(
            {img: ("text/html; charset=utf-8", "<html>just a moment…</html>")})
        self.crawler.http = sess

        self.crawler._grab_assets({img}, referer=ROOT)

        self.assertFalse(self.store.has_asset(img))   # a gate page is not an asset


class BlobExtensionSniffing(unittest.TestCase):
    """A downloaded image must land on disk with a renderable extension — the
    archive is served statically and the browser only paints media whose type
    the static server infers from the file name.  An image delivered with a
    vague content-type must not become an inert ``.bin``."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kiwi-ext-")
        _point_config_at(self.tmp)
        from kiwieater.storage import ArchiveStore
        self.store = ArchiveStore()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ext(self, url, ct, data):
        rel = self.store.save_asset(url, ct, data, source_page=ROOT)
        self.assertTrue(os.path.isfile(os.path.join(config.ARCHIVE_DIR, rel)))
        return os.path.splitext(rel)[1]

    def test_declared_content_type_wins(self):
        self.assertEqual(
            self._ext("https://kiwifarms.st/a", "image/webp", b"RIFF????WEBPxx"),
            ".webp")

    def test_magic_bytes_rescue_an_unknown_content_type(self):
        self.assertEqual(
            self._ext("https://kiwifarms.st/p", "application/octet-stream",
                      b"\x89PNG\r\n\x1a\n" + b"\x00" * 16), ".png")
        self.assertEqual(
            self._ext("https://kiwifarms.st/j", "", b"\xff\xd8\xff\xe0" + b"\x00" * 8),
            ".jpg")
        self.assertEqual(
            self._ext("https://kiwifarms.st/g", None, b"GIF89a" + b"\x00" * 8),
            ".gif")

    def test_url_extension_then_bin_as_last_resorts(self):
        # Unknown type *and* unknown bytes, but the URL names an extension.
        self.assertEqual(
            self._ext("https://kiwifarms.st/x.svgz?z=1",
                      "application/octet-stream", b"\x1f\x8bgzipped"), ".svgz")
        # Nothing to go on at all -> .bin (never guessed wrong).
        self.assertEqual(
            self._ext("https://kiwifarms.st/mystery",
                      "application/octet-stream", b"\x00\x01junkjunk"), ".bin")


class ManifestRootIsNormalised(unittest.TestCase):
    """The viewer finds the "main page" by the normalised root key; the builder
    must emit a normalised ``root_url`` (``DEFAULT_ROOT`` carries a trailing
    slash) so that lookup never misses the home page."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kiwi-root-")
        _point_config_at(self.tmp)
        from kiwieater.storage import ArchiveStore
        self.store = ArchiveStore()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_trailing_slash_root_is_normalised_to_the_page_key(self):
        from kiwieater.archive_builder import ArchiveBuilder
        self.store.set_meta("root_url", "https://kiwifarms.st/")
        self.store.save_page("https://kiwifarms.st", "Home",
                             "<html><body>home</body></html>", "home", 1,
                             [], set(), trail="000000")
        manifest = ArchiveBuilder(self.store).build()
        self.assertEqual(manifest["root_url"], "https://kiwifarms.st")
        self.assertIn("https://kiwifarms.st",
                      [p["url"] for p in manifest["pages"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
