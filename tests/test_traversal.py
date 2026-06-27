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
                            page_url, is_thread, is_forum, child_trail,
                            host_root, focus_chain, focus_path_of, within_focus)
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

    def test_depth_cap_blocks_descents_but_not_pagination(self):
        # At the depth cap, descents into *other* sections are blocked, but the
        # current thread's pagination keeps going so coverage stays complete.
        other = "https://kiwifarms.st/threads/other.999"
        links = _thread_page_links(THREAD, 65) + [{"href": other}]
        urls = [k["url"] for k in plan_children(THREAD, 500, links, 500)]
        self.assertIn(THREAD + "/page-2", urls)   # pagination survives the cap
        self.assertNotIn(other, urls)             # the descent is blocked by it

    def test_thread_entered_at_last_page_steps_to_previous(self):
        # Posts open on the most recent page; from the last page the systematic
        # step is to the PREVIOUS page (…/page-700 -> …/page-699), walking down.
        last = THREAD + "/page-700"
        links = _thread_page_links(THREAD, 700)   # nav advertises pages 1..700
        kids = [k["url"] for k in plan_children(last, 9, links, 500)]
        self.assertEqual(kids[0], THREAD + "/page-699")   # descends
        self.assertNotIn(THREAD + "/page-701", kids)      # nothing beyond the last

    def test_post_anchor_is_not_queued_as_a_separate_page(self):
        # A per-post permalink (…/page-700#post-N) must collapse onto its page.
        anchor = THREAD + "/page-700#post-24855389"
        self.assertEqual(urls.normalize_url(anchor), THREAD + "/page-700")
        # Even handed an un-normalised anchor href, plan_children never emits one.
        links = [{"href": anchor}, {"href": THREAD + "/page-701"}]
        kids = [k["url"] for k in plan_children(THREAD + "/page-700", 9,
                                                links, 500)]
        self.assertTrue(all("#" not in u for u in kids), kids)


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


class ThreadCoverage(unittest.TestCase):
    """A thread must be archived in full — every page — by progressing through
    it systematically, even when entered on its most recent page and even when
    it is far longer than ``max_depth``."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kiwi-test-")
        _point_config_at(self.tmp)
        from kiwieater.storage import ArchiveStore
        self.store = ArchiveStore()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _entered_at_last_page(self, pages):
        """The forum links the thread at its *latest* page only (as KiwiFarms
        does — posts open on the most recent page); every thread page shows the
        full 1..N pagination nav."""
        last = THREAD + f"/page-{pages}"

        def link_map(url):
            base, _ = split_pagination(url)
            if url == HOME:
                return [{"href": FORUM}]
            if base == FORUM:
                return [{"href": last}]
            if base == THREAD:
                return _thread_page_links(THREAD, pages)
            return []
        return FakeSite(link_map)

    def test_full_coverage_from_last_page_even_past_depth_cap(self):
        pages = 8
        # max_depth 3 just reaches the thread (home=1, forum=2, thread=3) and is
        # far below the page count, yet pagination must still cover every page.
        order = run_crawl(self._entered_at_last_page(pages), self.store,
                          max_depth=3)
        seq = [u for u, _ in order]
        self.assertEqual(seq[0], HOME)
        self.assertEqual(seq[2], THREAD + f"/page-{pages}")     # entered last
        self.assertEqual(seq[3], THREAD + f"/page-{pages - 1}")  # stepped down
        # Every page 1..N archived exactly once (page 1 == the bare base URL).
        expected_pages = [THREAD] + [THREAD + f"/page-{n}"
                                     for n in range(2, pages + 1)]
        for p in expected_pages:
            self.assertEqual(seq.count(p), 1, p)
        # …and the walk down is contiguous: page-8, page-7, …, page-2, page-1.
        self.assertEqual(seq[2:2 + pages],
                         [THREAD + f"/page-{n}" for n in range(pages, 1, -1)]
                         + [THREAD])

    def test_a_new_post_appending_a_page_mid_crawl_is_still_covered(self):
        # The page count is re-read from each page, never cached, so a thread
        # that grows while the backup runs is still completed in full.
        visited = set()
        state = {"pages": 3}

        def link_map(url):
            base, _ = split_pagination(url)
            if url == HOME:
                return [{"href": THREAD}]
            if base == THREAD:
                visited.add(split_pagination(url)[1])
                if 2 in visited:        # a new post appends a page mid-crawl
                    state["pages"] = 4
                return _thread_page_links(THREAD, state["pages"])
            return []
        order = run_crawl(FakeSite(link_map), self.store)
        crawled = [u for u, _ in order]
        self.assertIn(THREAD + "/page-4", crawled)   # the appended page is caught
        for n in range(2, 5):
            self.assertEqual(crawled.count(THREAD + f"/page-{n}"), 1)


# --------------------------------------------------------------------------- #
#  Focused (sub-section) crawling
#
#  The task: let the operator point the crawler at one section (e.g.
#  ".../forums/lolcows.16/") instead of the whole site.  The crawler must first
#  archive the breadcrumb path leading down to it (main page -> /forums ->
#  the section) so the saved copy is navigable, then spiderweb *within* that
#  section only, and — across focus changes — accumulate into the one archive,
#  detecting already-saved locations instead of duplicating them.
# --------------------------------------------------------------------------- #
FORUMS = "https://kiwifarms.st/forums"            # the forum index listing
LOLCOWS = "https://kiwifarms.st/forums/lolcows.16"
BEAUTY = "https://kiwifarms.st/forums/beauty-parlour.20"
MEMBERS = "https://kiwifarms.st/members"
T_A = "https://kiwifarms.st/threads/alpha.1"       # threads inside lolcows.16
T_B = "https://kiwifarms.st/threads/bravo.2"
T_C = "https://kiwifarms.st/threads/charlie.3"     # a thread inside beauty.20


def _focus_site():
    """A small forum/thread site with links that climb back up and sideways, so
    confinement (not wandering out of the focused section) is actually tested."""
    def link_map(url):
        base, _ = split_pagination(url)
        if url == HOME:
            return [{"href": FORUMS}, {"href": MEMBERS}]
        if base == FORUMS:                       # index lists the two forums
            return [{"href": HOME}, {"href": LOLCOWS}, {"href": BEAUTY}]
        if base == MEMBERS:
            return [{"href": HOME}]
        if base == LOLCOWS:                      # forum: its threads + climbs
            return [{"href": HOME}, {"href": FORUMS}, {"href": BEAUTY},
                    {"href": T_A}, {"href": T_B}]
        if base == BEAUTY:
            return [{"href": HOME}, {"href": FORUMS}, {"href": LOLCOWS},
                    {"href": T_C}]
        if base == T_A:                          # thread w/ 2 pages + cross-links
            return _thread_page_links(T_A, 2) + [{"href": T_C}, {"href": LOLCOWS}]
        if base == T_B:
            return _thread_page_links(T_B, 1) + [{"href": LOLCOWS}]
        if base == T_C:
            return _thread_page_links(T_C, 1) + [{"href": BEAUTY}]
        return []
    return FakeSite(link_map)


def seed_focus(store, root=HOME, focus=None):
    """Mirror ``Crawler._seed``: lay the breadcrumb chain down to the focus and
    re-open the active section so a (possibly previously breadcrumbed) section
    expands.  Idempotent, exactly like the real seeding."""
    chain = focus_chain(focus) if focus else [root]
    trail = config.ROOT_TRAIL
    parent = None
    for i, u in enumerate(chain):
        active = (i == len(chain) - 1)
        store.enqueue(u, i + 1, trail=trail, parent=parent,
                      section=section_key(u), page_no=1,
                      breadcrumb=0 if active else 1)
        parent = u
        trail = child_trail(trail, 0)
    store.reopen_active(chain[-1])


def run_focus_crawl(site, store, root=HOME, focus=None, max_depth=500,
                    stop_after=None, seed=True):
    """Drive the exact queue/plan/enqueue cycle the real crawler uses *with*
    focus + breadcrumb semantics (no browser): breadcrumb pages are saved but
    never expanded, descents are focus-confined, and children go through
    ``enqueue_or_reopen``/``is_settled`` so already-saved locations are reused."""
    focus_path = focus_path_of(focus) if focus else None
    if seed:
        seed_focus(store, root, focus)
    order = []
    while True:
        if stop_after is not None and len(order) >= stop_after:
            return order
        item = store.next_pending()
        if not item:
            return order
        url, depth = item["url"], item["depth"]
        trail = item["trail"] or config.ROOT_TRAIL
        is_bc = bool(item.get("breadcrumb"))
        links = site.links(url)
        if is_bc:
            children = []
        else:
            children = plan_children(url, depth, links, max_depth,
                                     is_known=store.is_settled,
                                     focus_path=focus_path)
        for i, child in enumerate(children):
            cu = child["url"]
            store.enqueue_or_reopen(cu, child["depth"],
                                    trail=child_trail(trail, i), parent=url,
                                    section=section_key(cu),
                                    page_no=split_pagination(cu)[1])
        store.save_page(url, url, "<html></html>", "", depth, links, set(),
                        trail=trail, parent=item["parent"],
                        section=item["section"], page_no=item["page_no"])
        store.mark(url, "done")
        order.append((url, depth))


class FocusHelpers(unittest.TestCase):
    def test_focus_chain_is_the_breadcrumb_path(self):
        self.assertEqual(focus_chain(LOLCOWS + "/"), [HOME, FORUMS, LOLCOWS])
        self.assertEqual(focus_chain(LOLCOWS + "/page-3"), [HOME, FORUMS, LOLCOWS])
        self.assertEqual(focus_chain(T_A),
                         [HOME, "https://kiwifarms.st/threads", T_A])
        self.assertEqual(focus_chain(FORUMS), [HOME, FORUMS])

    def test_host_root_and_focus_path(self):
        self.assertEqual(host_root(LOLCOWS + "/page-2"), HOME)
        self.assertEqual(focus_path_of(LOLCOWS + "/"), "/forums/lolcows.16")
        self.assertIsNone(focus_path_of(""))            # blank -> whole site
        self.assertIsNone(focus_path_of(HOME + "/"))    # main page -> whole site

    def test_within_focus_confines_to_the_section(self):
        fp = focus_path_of(LOLCOWS)
        # A forum's own threads are pulled in (its content), pagination too.
        self.assertTrue(within_focus(LOLCOWS, T_A, fp))
        self.assertTrue(within_focus(LOLCOWS, LOLCOWS + "/page-2", fp))
        # Climbing back up or sideways is refused.
        self.assertFalse(within_focus(LOLCOWS, HOME, fp))
        self.assertFalse(within_focus(LOLCOWS, FORUMS, fp))
        self.assertFalse(within_focus(LOLCOWS, BEAUTY, fp))
        # A thread does not leak into other threads.
        self.assertFalse(within_focus(T_A, T_C, fp))

    def test_within_focus_on_index_includes_every_forum(self):
        fp = focus_path_of(FORUMS)
        self.assertTrue(within_focus(FORUMS, LOLCOWS, fp))
        self.assertTrue(within_focus(FORUMS, BEAUTY, fp))
        self.assertFalse(within_focus(FORUMS, HOME, fp))
        self.assertFalse(within_focus(FORUMS, MEMBERS, fp))

    def test_no_focus_allows_everything(self):
        self.assertTrue(within_focus(LOLCOWS, BEAUTY, None))
        self.assertTrue(within_focus(T_A, T_C, None))


class FocusedCrawl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kiwi-test-")
        _point_config_at(self.tmp)
        from kiwieater.storage import ArchiveStore
        self.Store = ArchiveStore
        self.store = ArchiveStore()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _urls(self, order):
        return [u for u, _ in order]

    def test_fresh_focus_archives_breadcrumbs_then_spiderwebs_section(self):
        order = run_focus_crawl(_focus_site(), self.store, focus=LOLCOWS + "/")
        urls_in_order = self._urls(order)
        # Exactly the task's worked example: main page, then /forums, then the
        # focused section is crawled (its threads + their pages), and nothing
        # outside the section.
        self.assertEqual(urls_in_order,
                         [HOME, FORUMS, LOLCOWS,
                          T_A, T_A + "/page-2", T_B])
        # Out-of-focus sections are untouched.
        for off in (BEAUTY, T_C, MEMBERS):
            self.assertNotIn(off, urls_in_order)
        # The breadcrumb ancestors really are stored as breadcrumbs (archived
        # for navigation, not spiderwebbed); the section root is a full node.
        self.assertTrue(self._is_breadcrumb(HOME))
        self.assertTrue(self._is_breadcrumb(FORUMS))
        self.assertFalse(self._is_breadcrumb(LOLCOWS))
        # Depths follow the trail: section at 3, its threads at 4, page-2 at 5.
        depth_of = dict(order)
        self.assertEqual(depth_of[HOME], 1)
        self.assertEqual(depth_of[FORUMS], 2)
        self.assertEqual(depth_of[LOLCOWS], 3)
        self.assertEqual(depth_of[T_A], 4)
        self.assertEqual(depth_of[T_A + "/page-2"], 5)

    def _is_breadcrumb(self, url):
        with self.store._conn() as c:
            row = c.execute("SELECT breadcrumb FROM queue WHERE url=?",
                            (url,)).fetchone()
        return bool(row["breadcrumb"])

    def test_widening_focus_to_parent_skips_already_archived_child(self):
        site = _focus_site()
        # 1) Archive the lolcows.16 forum.
        first = self._urls(run_focus_crawl(site, self.store, focus=LOLCOWS + "/"))
        # 2) Now widen the focus to the whole /forums index in the SAME archive.
        second = self._urls(run_focus_crawl(site, self.store, focus=FORUMS + "/"))
        # The forums index is (re)expanded and the *other* forum + its thread are
        # crawled; the already-archived lolcows.16 section is detected and NOT
        # crawled again.
        self.assertIn(BEAUTY, second)
        self.assertIn(T_C, second)
        self.assertNotIn(LOLCOWS, second)
        self.assertNotIn(T_A, second)
        self.assertNotIn(T_B, second)
        # One archive, no duplicates: every distinct page saved exactly once.
        all_pages = [HOME, FORUMS, LOLCOWS, T_A, T_A + "/page-2", T_B,
                     BEAUTY, T_C]
        self.assertEqual(self.store.stats()["pages"], len(all_pages))
        for p in all_pages:
            self.assertTrue(self.store.page_exists(p), p)

    def test_widening_back_to_whole_site_covers_the_rest_once(self):
        site = _focus_site()
        run_focus_crawl(site, self.store, focus=LOLCOWS + "/")     # focused first
        rest = self._urls(run_focus_crawl(site, self.store, focus=None))  # whole site
        # Switching back to the whole site re-opens the breadcrumbed pages and
        # crawls everything that was skipped — without redoing the focused part.
        self.assertIn(MEMBERS, rest)
        self.assertIn(BEAUTY, rest)
        self.assertIn(T_C, rest)
        self.assertNotIn(T_A, rest)        # already captured under the focus
        self.assertNotIn(T_B, rest)
        # The home/forums pages flip from breadcrumb to fully-expanded.
        self.assertFalse(self._is_breadcrumb(HOME))
        self.assertFalse(self._is_breadcrumb(FORUMS))
        # The whole site is now present, each page exactly once.
        every = [HOME, FORUMS, MEMBERS, LOLCOWS, T_A, T_A + "/page-2", T_B,
                 BEAUTY, T_C]
        self.assertEqual(self.store.stats()["pages"], len(every))

    def test_focused_crawl_resumes_exactly_after_a_crash(self):
        site = _focus_site()
        # Process the breadcrumbs + section root, then "crash" mid-section.
        first = run_focus_crawl(site, self.store, focus=LOLCOWS + "/",
                                stop_after=4)
        self.assertEqual(len(first), 4)
        crashed = self.store.next_pending()        # one URL checked out…
        self.assertIsNotNone(crashed)
        self.assertEqual(self.store.stats()["processing"], 1)

        # A new process attaches to the same DB and resumes (no re-seed needed,
        # but re-seeding is idempotent so we exercise it as the console would).
        store2 = self.Store()
        store2.requeue_processing()
        second = run_focus_crawl(site, store2, focus=LOLCOWS + "/")

        done = self._urls(first) + self._urls(second)
        expected = {HOME, FORUMS, LOLCOWS, T_A, T_A + "/page-2", T_B}
        self.assertEqual(set(done), expected)            # full coverage
        self.assertEqual(len(done), len(expected))       # …and no duplicates
        self.assertIn(crashed["url"], done)


if __name__ == "__main__":
    unittest.main(verbosity=2)
