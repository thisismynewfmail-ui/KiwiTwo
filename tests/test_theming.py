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
from kiwieater.cleaner import clean_html, extract_css_refs


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
