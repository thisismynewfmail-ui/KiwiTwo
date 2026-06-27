"""Network-free tests for the section-aware spiderweb crawl.

These exercise the real logic the task is about — dynamic, section-relative
depth; depth-first "spiderweb" ordering with backtracking; and a trail that
makes resume exact — by driving the actual ``urls`` helpers, ``plan_children``
planner and SQLite ``ArchiveStore`` against a synthetic site (no browser, no
Flask, no kiwifarms.st access).
"""

import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kiwieater import config
from kiwieater import urls
from kiwieater.urls import (split_pagination, section_key, next_page_url,
                            page_url, is_thread, is_forum, child_trail)
from kiwieater.crawler import plan_children


HOME = "https://kiwifarms.st"
FORUM = "https://kiwifarms.st/forums/lolcows.16"
SUBFORUM = "https://kiwifarms.st/forums/beauty-parlour.20"
THREAD = "https://kiwifarms.st/threads/kino-casino.110845"


def _point_config_at(tmp):
    """Redirect every config path at a throwaway directory so a real store can
    be built without touching the repo's data/Archive."""
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


# --------------------------------------------------------------------------- #
#  A tiny synthetic "site": maps a URL to the in-scope links it would expose.
# --------------------------------------------------------------------------- #
def _thread_page_links(base, total_pages):
    """A thread/forum page advertises its full pagination (1..total)."""
    return [{"href": page_url(base, n)} for n in range(1, total_pages + 1)]


class FakeSite:
    def __init__(self, link_map):
        self.link_map = link_map  # callable(url) -> list[{"href": ...}]

    def links(self, url):
        return self.link_map(url)


def run_crawl(site, store, root=HOME, max_depth=500, stop_after=None):
    """Drive the exact queue/plan/enqueue cycle the real crawler uses, minus the
    browser.  Returns the processed-to-done order as ``[(url, depth), ...]``.

    ``stop_after`` simulates a crash: it stops *before* marking the Nth page
    done, leaving it ``processing`` (resumable) and the rest ``pending``.
    """
    store.enqueue(root, 1, trail=config.ROOT_TRAIL, parent=None,
                  section=section_key(root), page_no=1)
    order = []
    while True:
        if stop_after is not None and len(order) >= stop_after:
            return order
        item = store.next_pending()
        if not item:
            return order
        url, depth = item["url"], item["depth"]
        trail = item["trail"] or config.ROOT_TRAIL
        links = site.links(url)
        children = plan_children(url, depth, links, max_depth,
                                 is_known=store.is_known)
        for i, child in enumerate(children):
            cu = child["url"]
            store.enqueue(cu, child["depth"], trail=child_trail(trail, i),
                          parent=url, section=section_key(cu),
                          page_no=split_pagination(cu)[1])
        store.save_page(url, url, "<html></html>", "", depth, links, set(),
                        trail=trail, parent=item["parent"],
                        section=item["section"], page_no=item["page_no"])
        store.mark(url, "done")
        order.append((url, depth))


class UrlHelpers(unittest.TestCase):
    def test_split_pagination_path_style(self):
        self.assertEqual(split_pagination(THREAD + "/page-65"),
                         (THREAD, 65))
        self.assertEqual(split_pagination(THREAD), (THREAD, 1))
        self.assertEqual(split_pagination(THREAD + "/"), (THREAD, 1))

    def test_split_pagination_query_style(self):
        self.assertEqual(split_pagination(FORUM + "?page=3")[1], 3)

    def test_section_key_groups_pages(self):
        self.assertEqual(section_key(THREAD + "/page-65"), THREAD)
        self.assertEqual(section_key(THREAD + "/page-2"), THREAD)
        self.assertEqual(section_key(THREAD), THREAD)

    def test_next_page_url(self):
        self.assertEqual(next_page_url(THREAD), THREAD + "/page-2")
        self.assertEqual(next_page_url(THREAD + "/page-2"), THREAD + "/page-3")

    def test_thread_vs_forum_classification(self):
        self.assertTrue(is_thread(THREAD))
        self.assertTrue(is_thread(THREAD + "/page-9"))
        self.assertFalse(is_thread(FORUM))
        self.assertFalse(is_thread(HOME))
        self.assertFalse(is_thread("https://kiwifarms.st/threads"))  # the index
        self.assertTrue(is_forum(FORUM))

    def test_child_trail_sorts_depth_first(self):
        # A grandchild must sort before its parent's later sibling (pre-order).
        root = config.ROOT_TRAIL
        first_child = child_trail(root, 0)
        grandchild = child_trail(first_child, 0)
        second_child = child_trail(root, 1)
        self.assertLess(grandchild, second_child)
        self.assertLess(first_child, grandchild)


class PlanChildren(unittest.TestCase):
    def test_thread_dives_into_next_page_first(self):
        links = _thread_page_links(THREAD, 65) + [{"href": FORUM}]
        kids = plan_children(THREAD, 4, links, 500)
        # Pagination next-page leads (deep dive), the off-thread link follows.
        self.assertEqual(kids[0]["url"], THREAD + "/page-2")
        self.assertEqual(kids[0]["depth"], 5)
        self.assertIn(FORUM, [k["url"] for k in kids])
        # Other pages (page-3..65) are NOT queued as branches — only page-2.
        page_children = [k for k in kids if section_key(k["url"]) == THREAD]
        self.assertEqual(len(page_children), 1)

    def test_listing_takes_threads_before_its_own_next_page(self):
        threadA = "https://kiwifarms.st/threads/a.1"
        threadB = "https://kiwifarms.st/threads/b.2"
        links = [{"href": threadA}, {"href": threadB},
                 {"href": page_url(SUBFORUM, 2)}]  # listing advertises page 2
        kids = [k["url"] for k in plan_children(SUBFORUM, 3, links, 500)]
        # Threads first, listing's own next page last.
        self.assertEqual(kids, [threadA, threadB, page_url(SUBFORUM, 2)])

    def test_depth_cap_blocks_descent(self):
        links = _thread_page_links(THREAD, 65)
        self.assertEqual(plan_children(THREAD, 500, links, 500), [])


class DepthExample(unittest.TestCase):
    """The task's worked example: page-65 of a thread reached via
    main -> forum -> sub-forum -> thread lands at depth 68."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kiwi-test-")
        _point_config_at(self.tmp)
        from kiwieater.storage import ArchiveStore
        self.store = ArchiveStore()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _linear_site(self, pages=65):
        def link_map(url):
            base, _ = split_pagination(url)
            if url == HOME:
                return [{"href": FORUM}]
            if base == FORUM:
                return [{"href": SUBFORUM}]
            if base == SUBFORUM:
                return [{"href": THREAD}]
            if base == THREAD:
                return _thread_page_links(THREAD, pages)
            return []
        return FakeSite(link_map)

    def test_page65_is_depth_68(self):
        order = run_crawl(self._linear_site(65), self.store)
        depth_of = dict(order)
        self.assertEqual(depth_of[HOME], 1)
        self.assertEqual(depth_of[FORUM], 2)
        self.assertEqual(depth_of[SUBFORUM], 3)
        self.assertEqual(depth_of[THREAD], 4)             # thread first page
        self.assertEqual(depth_of[THREAD + "/page-2"], 5)
        self.assertEqual(depth_of[THREAD + "/page-65"], 68)

    def test_pages_are_contiguous_and_complete(self):
        order = run_crawl(self._linear_site(65), self.store)
        urls_in_order = [u for u, _ in order]
        # Main page is first (archive's first page == the site's main page).
        self.assertEqual(urls_in_order[0], HOME)
        # Every thread page 1..65 present exactly once.
        expected_pages = [THREAD] + [THREAD + f"/page-{n}" for n in range(2, 66)]
        for p in expected_pages:
            self.assertEqual(urls_in_order.count(p), 1, p)
        # The thread's pages form one contiguous, monotonically deepening run.
        first = urls_in_order.index(THREAD)
        run = urls_in_order[first:first + 65]
        self.assertEqual(run, expected_pages)
        self.assertEqual([d for _, d in order[first:first + 65]],
                         list(range(4, 69)))


class SpiderwebBacktracking(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kiwi-test-")
        _point_config_at(self.tmp)
        from kiwieater.storage import ArchiveStore
        self.store = ArchiveStore()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _forum_site(self):
        tA = "https://kiwifarms.st/threads/alpha.1"
        tB = "https://kiwifarms.st/threads/bravo.2"
        tC = "https://kiwifarms.st/threads/charlie.3"
        self.tA, self.tB, self.tC = tA, tB, tC

        def link_map(url):
            base, _ = split_pagination(url)
            if url == HOME:
                return [{"href": SUBFORUM}]
            if base == SUBFORUM:
                # Listing page 1 lists threads A & B and advertises page 2.
                page = split_pagination(url)[1]
                if page == 1:
                    return [{"href": tA}, {"href": tB},
                            {"href": page_url(SUBFORUM, 2)}]
                return [{"href": tC}]  # listing page 2 lists thread C
            if base == tA:
                return _thread_page_links(tA, 3)
            if base == tB:
                return _thread_page_links(tB, 2)
            if base == tC:
                return _thread_page_links(tC, 1)
            return []
        return FakeSite(link_map)

    def test_finish_thread_then_next_then_listing_page(self):
        order = [u for u, _ in run_crawl(self._forum_site(), self.store)]
        expected = [
            HOME,
            SUBFORUM,
            self.tA, self.tA + "/page-2", self.tA + "/page-3",  # thread A, fully
            self.tB, self.tB + "/page-2",                       # then thread B
            page_url(SUBFORUM, 2),                              # then listing p2
            self.tC,                                            # then its thread
        ]
        self.assertEqual(order, expected)


class ResumeExactness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kiwi-test-")
        _point_config_at(self.tmp)
        from kiwieater.storage import ArchiveStore
        self.Store = ArchiveStore
        self.store = ArchiveStore()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _site(self, pages=20):
        def link_map(url):
            base, _ = split_pagination(url)
            if url == HOME:
                return [{"href": THREAD}]
            if base == THREAD:
                return _thread_page_links(THREAD, pages)
            return []
        return FakeSite(link_map)

    def test_crash_midway_then_resume_covers_everything_once(self):
        site = self._site(20)
        # First leg: cleanly process 8 pages …
        first = run_crawl(site, self.store, stop_after=8)
        self.assertEqual(len(first), 8)
        # … then a hard crash mid-page: a 9th URL is checked out (marked
        # ``processing``) but never finished.
        crashed = self.store.next_pending()
        self.assertIsNotNone(crashed)
        self.assertEqual(self.store.stats()["processing"], 1)

        # A new process attaches to the same DB and resumes.
        store2 = self.Store()
        recovered = store2.requeue_processing()   # the in-flight page comes back
        self.assertEqual(recovered, 1)
        second = run_crawl(site, store2)

        done_urls = [u for u, _ in first] + [u for u, _ in second]
        # 1 home + 20 thread pages, each archived exactly once (no dup, no gap),
        # including the page that was in flight when the crash happened.
        self.assertEqual(len(done_urls), 21)
        self.assertEqual(len(set(done_urls)), 21)
        self.assertIn(crashed["url"], done_urls)
        self.assertEqual(self.store.stats()["pages"], 21)


class ManifestOrder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kiwi-test-")
        _point_config_at(self.tmp)
        from kiwieater.storage import ArchiveStore
        self.store = ArchiveStore()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_manifest_lists_main_page_first_in_trail_order(self):
        def link_map(url):
            base, _ = split_pagination(url)
            if url == HOME:
                return [{"href": THREAD}]
            if base == THREAD:
                return _thread_page_links(THREAD, 4)
            return []
        run_crawl(FakeSite(link_map), self.store)
        from kiwieater.archive_builder import ArchiveBuilder
        self.store.set_meta("root_url", HOME)
        manifest = ArchiveBuilder(self.store).build()
        page_urls = [p["url"] for p in manifest["pages"]]
        self.assertEqual(page_urls[0], HOME)            # first page == main page
        self.assertEqual(page_urls,
                         [HOME, THREAD, THREAD + "/page-2",
                          THREAD + "/page-3", THREAD + "/page-4"])
        # Every page carries its stored trail (navigation structure in backup).
        self.assertTrue(all(p["trail"] for p in manifest["pages"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
