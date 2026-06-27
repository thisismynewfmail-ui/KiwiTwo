"""Background crawl worker: a section-aware, spiderwebbing, resumable scrape.

The crawler owns one browser and drains a persistent work queue **depth-first**,
following a section's trail to its end before backtracking — exactly how a
person clicking the site's own buttons would explore it.  Starting at the main
page it dives into a forum, then a thread, follows that thread's pagination all
the way down (``…/page-2 → …/page-3 → …``), then unwinds to the listing and
takes the next thread, then the next forum, and so on.

Two ideas make this work and make it resumable:

* **Dynamic, section-relative depth.**  Depth is the length of the navigation
  *trail*, and each extra page of a section is one step deeper.  So
  ``/threads/kino-casino.110845/page-65`` is 64 steps below the thread's first
  page — its depth adapts to how deep its section sits.  A ``max_depth`` of 500
  therefore means "dig up to 500 pages deep within a single subsection".

* **A stored trail.**  Every queue row carries a materialised-path ``trail``;
  ordering the queue by it reproduces the depth-first spiderweb *and*, because
  the trail is persisted, the identical order after a Stop/crash.  That single
  fact gives both full coverage and an exact resume.

Resilience details: every unit of work is a queue row in SQLite (a Stop leaves
it ``pending``; a crash leaves ``processing`` rows that the next start
recovers); per-URL retries use exponential backoff with jitter; inter-page
sleep, a warm-up handshake, persistent clearance cookies and a shared
``requests`` session for assets all reduce timeouts and blocks.
"""

import time
import random
import threading
from datetime import datetime

import requests

from . import config
from .logbook import log, open_session_log
from .browser import BrowserEngine
from .cleaner import clean_html
from .urls import (normalize_url, in_scope, looks_like_asset, section_key,
                   split_pagination, next_page_url, is_thread, child_trail)


def plan_children(url, depth, links, max_depth, is_known=None):
    """Plan the ordered children to enqueue when expanding ``url`` — the core of
    the spiderweb traversal, kept pure so it can be reasoned about and tested.

    Returns a list of ``{"url", "depth"}`` in the exact order they should be
    queued.  Ordering by the resulting trails then yields the desired walk:

    * **Pagination is a same-section continuation, not a branch.**  Other pages
      of the current section (``/page-N``) are *not* queued individually; we
      step exactly one page at a time so depth increases by one per page and the
      whole section is crawled as one contiguous run.

    * **Content sections dive, listings fan out.**  On a thread we put its next
      page *first* so the thread is followed to its end before its outbound
      links; on a forum/index listing we put the section's own next page *last*
      so every thread on the current page is taken before advancing the listing
      — i.e. "finish this thread, back to the listing, next thread, … then the
      listing's next page, then the next forum".
    """
    is_known = is_known or (lambda u: False)
    section = section_key(url)

    seen = set()
    same_section_pages = []     # page numbers this section advertises
    descents = []               # ordered, de-duplicated links into other sections
    for link in links:
        nu = link.get("href")
        if not nu or not in_scope(nu) or looks_like_asset(nu):
            continue
        if section_key(nu) == section:
            same_section_pages.append(split_pagination(nu)[1])
            continue
        if nu in seen:
            continue
        seen.add(nu)
        descents.append(nu)

    # A next page exists only if the section advertises one beyond the current.
    cur_page = split_pagination(url)[1]
    max_advertised = max(same_section_pages) if same_section_pages else cur_page
    next_pg = next_page_url(url) if max_advertised > cur_page else None

    if is_thread(url):
        ordered = ([next_pg] if next_pg else []) + descents
    else:
        ordered = descents + ([next_pg] if next_pg else [])

    if depth + 1 > max_depth:
        return []
    children = []
    for u in ordered:
        if is_known(u):
            continue
        children.append({"url": u, "depth": depth + 1})
    return children


class Crawler:
    def __init__(self, store, builder):
        self.store = store
        self.builder = builder
        self.thread = None
        self.state = "idle"      # idle|running|paused|stopping|error|done
        self.current_url = ""
        self.session_id = None
        self.settings = {}
        self._pause = threading.Event()
        self._stop = threading.Event()
        self.browser = None
        self.http = None

    # ------------------------------------------------------------------ #
    #  Lifecycle controls
    # ------------------------------------------------------------------ #
    def start(self, settings, mode="resume"):
        if self.state in ("running", "paused"):
            return False, "A crawl is already active."
        if self.thread and self.thread.is_alive():
            return False, ("Previous crawl is still shutting down — wait a "
                           "moment and press RESUME / RUN again.")

        merged = dict(config.DEFAULT_SETTINGS)
        merged.update(settings or {})
        self.settings = merged

        root = normalize_url(merged.get("root_url") or config.DEFAULT_ROOT)
        if not root or not in_scope(root):
            return False, "Root URL must be on kiwifarms.st."

        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        open_session_log(self.session_id)
        self.store.set_meta("settings", merged)
        self.store.set_meta("session_id", self.session_id)
        self.store.set_meta("root_url", root)

        if mode == "new":
            self.store.wipe_archive()
            log("INFO", "Started a fresh archive (previous data cleared).")
            self._seed_root(root)
        else:
            recovered = self.store.requeue_processing()
            pending = self.store.stats()["pending"]
            if pending == 0:
                self._seed_root(root)
                log("INFO", "No pending work found; seeding from the main page.")
            else:
                log("INFO", f"Resuming spiderweb: {pending} URL(s) pending "
                            f"({recovered} recovered from an interrupted run).")

        self._pause.clear()
        self._stop.clear()
        self.state = "running"
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return True, "Crawl started."

    def pause(self):
        if self.state == "running":
            self._pause.set()
            self.state = "paused"
            log("INFO", "Crawl paused.")
            return True
        return False

    def resume(self):
        if self.state == "paused":
            self._pause.clear()
            self.state = "running"
            log("INFO", "Crawl resumed.")
            return True
        return False

    def stop(self):
        if self.state in ("running", "paused"):
            self.state = "stopping"
            self._stop.set()
            self._pause.clear()
            log("INFO", "Stop requested; finishing current page…")
            return True
        return False

    def status(self):
        return {"state": self.state, "current_url": self.current_url,
                "session_id": self.session_id,
                "engine": (self.browser.kind if self.browser else None)}

    def _seed_root(self, root):
        """Seed the queue with the site's main page as the trail's origin, so the
        archive's first page is the main page and every later URL hangs off it.
        Depth is 1-based (main page = 1) so pagination depth matches the trail:
        a thread reached via main→forum→subforum sits at depth 4, and its
        ``page-65`` at depth 68."""
        self.store.enqueue(root, 1, trail=config.ROOT_TRAIL, parent=None,
                           section=section_key(root), page_no=1)

    # ------------------------------------------------------------------ #
    #  Worker thread
    # ------------------------------------------------------------------ #
    def _run(self):
        s = self.settings
        max_depth = int(s.get("max_depth", config.DEFAULT_SETTINGS["max_depth"]))
        max_pages = int(s.get("max_pages", config.DEFAULT_SETTINGS["max_pages"]))
        sleep_base = float(s.get("sleep", 3.0))
        jitter = float(s.get("jitter", 1.5))
        grab_assets = bool(s.get("assets", True))
        max_attempts = int(s.get("max_attempts", 4))
        root = self.store.get_meta("root_url", config.DEFAULT_ROOT)

        try:
            self.browser = BrowserEngine(
                headless=bool(s.get("headless", False)),
                engine=s.get("engine", "auto"),
                challenge_timeout=int(s.get("challenge_timeout", 90)),
                manual_solve=bool(s.get("manual_solve", True)))
            self.browser.start()
        except Exception as exc:
            self.state = "error"
            log("ERROR", f"Could not start browser: {exc}")
            return

        self.http = requests.Session()
        self.http.headers.update({"User-Agent": config.USER_AGENT,
                                  "Accept-Language": config.ACCEPT_LANGUAGE,
                                  "Referer": root})

        log("INFO", f"Warming up session against {root} (clearing Kiwiflare)…")
        self.browser.warm_up(root, should_stop=self._stop.is_set)
        self._sync_cookies()

        processed = 0
        try:
            while not self._stop.is_set():
                while self._pause.is_set() and not self._stop.is_set():
                    time.sleep(0.4)
                if self._stop.is_set():
                    break
                if max_pages and processed >= max_pages:
                    log("INFO", f"Reached page limit ({max_pages}).")
                    break

                item = self.store.next_pending()
                if not item:
                    log("INFO", "Queue empty — full coverage reached.")
                    break

                url, depth, attempts = item["url"], item["depth"], item["attempts"]
                trail = item.get("trail") or config.ROOT_TRAIL
                cap = max_pages or "∞"
                self.current_url = url
                log("INFO", f"[{processed+1}/{cap} depth={depth}] {url}")

                try:
                    self._sync_cookies()
                    html, title = self.browser.fetch(
                        url, should_stop=self._stop.is_set)
                    cleaned, text, assets, links = clean_html(html, url)

                    # Record the link graph and harvest in-scope static assets.
                    for link in links:
                        nu = link.get("href")
                        if not nu or not in_scope(nu):
                            continue
                        self.store.add_link(url, nu)
                        if grab_assets and looks_like_asset(nu):
                            assets.add(nu)

                    # Plan the spiderweb descent (pagination-aware, section-aware)
                    # and enqueue children with materialised-path trails so the
                    # depth-first order is reproduced — and resumable — from the
                    # queue alone.
                    children = plan_children(url, depth, links, max_depth,
                                             is_known=self.store.is_known)
                    for i, child in enumerate(children):
                        cu = child["url"]
                        self.store.enqueue(
                            cu, child["depth"], trail=child_trail(trail, i),
                            parent=url, section=section_key(cu),
                            page_no=split_pagination(cu)[1])

                    self.store.save_page(
                        url, title, cleaned, text, depth, links, assets,
                        trail=trail, parent=item.get("parent"),
                        section=item.get("section") or section_key(url),
                        page_no=item.get("page_no") or split_pagination(url)[1])
                    if grab_assets:
                        self._grab_assets(assets, url)

                    self.store.mark(url, "done")
                    processed += 1
                    log("INFO", f"Saved '{(title or url)[:70]}' "
                                f"(+{len(children)} trail links, "
                                f"{len(assets)} assets)")

                except Exception as exc:
                    if self._stop.is_set():
                        self.store.mark(url, "pending")   # leave resumable
                        break
                    attempts += 1
                    if attempts >= max_attempts:
                        self.store.mark(url, "failed", bump_attempt=True)
                        log("ERROR", f"Giving up on {url}: {exc}")
                    else:
                        self.store.mark(url, "pending", bump_attempt=True)
                        backoff = min(45, 2 ** attempts) + random.random()
                        log("WARNING", f"Error on {url} ({exc}); retry "
                                       f"{attempts}/{max_attempts} in "
                                       f"{backoff:.1f}s")
                        self._interruptible_sleep(backoff)
                    continue

                self._interruptible_sleep(
                    max(0.0, sleep_base + random.uniform(-jitter, jitter)))

            if self._stop.is_set():
                self.state = "idle"
                log("INFO", "Crawl stopped. Progress saved; resume any time.")
            else:
                self.state = "done"

            log("INFO", "Building index, gallery, search…")
            try:
                self.builder.build()
            except Exception as exc:
                log("WARNING", f"Index build issue: {exc}")
            st = self.store.stats()
            log("INFO", f"=== Crawl end: {st['pages']} pages, "
                        f"{st['assets']} assets ===")
        finally:
            try:
                self.browser.quit()
            except Exception:
                pass
            self.current_url = ""

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #
    def _interruptible_sleep(self, seconds):
        slept = 0.0
        while slept < seconds and not self._stop.is_set():
            time.sleep(0.2)
            slept += 0.2

    def _sync_cookies(self):
        try:
            for k, v in self.browser.cookies().items():
                self.http.cookies.set(k, v)
        except Exception:
            pass

    def _grab_assets(self, urls, referer):
        """Download in-scope media/assets as BLOB files via the requests session
        that shares the browser's clearance cookies, so downloads pass the gate
        without re-rendering each file.  Anything that returns an HTML challenge
        shell instead of binary data is skipped rather than stored."""
        for u in list(urls):
            if self._stop.is_set():
                return
            if not u or not in_scope(u) or self.store.has_asset(u):
                continue
            try:
                r = self.http.get(u, timeout=30, headers={"Referer": referer})
                if r.status_code != 200 or not r.content:
                    continue
                ct = r.headers.get("Content-Type",
                                   "application/octet-stream").split(";")[0].strip()
                if ct.startswith("text/html"):      # an asset came back as a gate
                    continue
                self.store.save_asset(u, ct, r.content, source_page=referer)
            except Exception as exc:
                log("WARNING", f"Asset failed {u}: {exc}")
            time.sleep(0.05)        # gentle pacing so bursts don't trip limits
