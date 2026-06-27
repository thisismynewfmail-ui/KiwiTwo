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
from .cleaner import clean_html, extract_css_refs
from .urls import (normalize_url, in_scope, looks_like_asset, section_key,
                   split_pagination, next_page_url, page_url, is_thread,
                   child_trail, within_focus, focus_chain, focus_path_of)


def _is_stylesheet(content_type, url):
    """True if a downloaded asset is CSS — by content-type (XenForo serves
    themes from ``/css.php`` with ``text/css``) or by a ``.css``/``.less`` path."""
    if content_type and "css" in content_type.lower():
        return True
    path = (url or "").split("?")[0].lower()
    return path.endswith((".css", ".less"))


def plan_children(url, depth, links, max_depth, is_known=None, focus_path=None):
    """Plan the ordered children to enqueue when expanding ``url`` — the core of
    the spiderweb traversal, kept pure so it can be reasoned about and tested.

    Returns a list of ``{"url", "depth"}`` in the exact order they should be
    queued.  Ordering by the resulting trails then yields the desired walk:

    * **Pagination steps one page at a time and is never cut off.**  Other pages
      of the current section (``/page-N``) are not queued all at once; we step to
      an adjacent page so the section is crawled as one contiguous run.  A thread
      opens on its *most recent* page, so we step to the **previous** page first
      (``…/page-700 → …/page-699 → …``) and systematically walk down to page 1;
      the next page is queued too when one is advertised, which covers a lower
      entry point and any page a new post appends mid-crawl.  ``max_advertised``
      is re-read from each page's own nav, so a page count that changes while the
      backup runs is handled rather than cached.

    * **Content sections dive, listings fan out.**  On a thread we put its
      pagination *first* so the thread is followed to its end before its outbound
      links; on a forum/index listing we put the section's own pagination *last*
      so every thread on the current page is taken before advancing the listing.

    * **Only descents into new sections consume depth.**  ``max_depth`` bounds
      how far the crawl fans out (main → forum → sub-forum → thread); a section's
      own pages are *exempt*, so even a thread hundreds of pages long is archived
      in full instead of being truncated at the cap.

    * **Focused crawls stay inside their section.**  When ``focus_path`` is set,
      descents that fall outside the focused section (climbing back to the
      home/ancestor pages, sibling forums, threads of other forums) are dropped,
      so the spiderweb explores only within the chosen section.  Pagination of
      an in-focus page is always a same-section continuation and is kept.
    """
    is_known = is_known or (lambda u: False)
    section = section_key(url)

    seen = set()
    same_section_pages = []     # page numbers this section advertises
    descents = []               # ordered, de-duplicated links into other sections
    for link in links:
        # Normalise here too (drops ``#post-NNN`` anchors and the like) so a
        # per-post permalink collapses onto its page and is never queued or
        # archived as a separate, duplicate page.
        nu = normalize_url(link.get("href"))
        if not nu or not in_scope(nu) or looks_like_asset(nu):
            continue
        if section_key(nu) == section:
            same_section_pages.append(split_pagination(nu)[1])
            continue
        if nu in seen:
            continue
        seen.add(nu)
        if not within_focus(url, nu, focus_path):
            continue
        descents.append(nu)

    # Step through the current section one page at a time, toward BOTH ends, so
    # it is covered in full regardless of which page we entered on.  Threads open
    # on the most recent page, so the previous page leads (a systematic walk down
    # to page 1); the next page follows when one is advertised, picking up a lower
    # entry point and any page a new post adds while the backup is running.
    # ``max_advertised`` is re-derived from this page every call, never cached.
    cur_page = split_pagination(url)[1]
    max_advertised = max(same_section_pages) if same_section_pages else cur_page
    prev_pg = page_url(section, cur_page - 1) if cur_page > 1 else None
    next_pg = next_page_url(url) if max_advertised > cur_page else None
    pagination = [p for p in (prev_pg, next_pg) if p]

    # Pagination is a same-section continuation and must NOT be cut off by the
    # depth cap, or a long thread would be archived only part-way; only descents
    # into other sections consume the structural depth budget.
    capped_descents = descents if depth + 1 <= max_depth else []

    if is_thread(url):
        ordered = pagination + capped_descents
    else:
        ordered = capped_descents + pagination

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

        # Optional sub-section focus.  Treat a blank value, or the main page
        # itself, as "no focus" (a whole-site crawl).
        focus = normalize_url(merged.get("focus_url") or "") or None
        if focus and not in_scope(focus):
            return False, "Focus section must be on kiwifarms.st."
        if focus and focus_path_of(focus) is None:
            focus = None
        self.settings["focus_url"] = focus or ""

        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        open_session_log(self.session_id)
        self.store.set_meta("settings", self.settings)
        self.store.set_meta("session_id", self.session_id)
        self.store.set_meta("root_url", root)
        self.store.set_meta("focus_url", focus or "")

        if mode == "new":
            self.store.wipe_archive()
            self._seed(root, focus)
            if focus:
                log("INFO", f"Started a fresh archive focused on {focus} "
                            "(archiving the path leading to it first).")
            else:
                log("INFO", "Started a fresh archive (previous data cleared).")
        else:
            recovered = self.store.requeue_processing()
            pending_before = self.store.stats()["pending"]
            # Re-seed idempotently so a newly chosen focus is honoured and any
            # section saved only as a breadcrumb is re-opened — without wiping,
            # so everything accumulates into the one archive.
            self._seed(root, focus)
            pending = self.store.stats()["pending"]
            if focus:
                log("INFO", f"Resuming archive, focused on {focus}: "
                            f"{pending} URL(s) pending "
                            f"({recovered} recovered from an interrupted run).")
            elif pending_before:
                log("INFO", f"Resuming spiderweb: {pending_before} URL(s) pending "
                            f"({recovered} recovered from an interrupted run).")
            else:
                log("INFO", "No pending work found; seeding from the main page.")

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

    def _seed(self, root, focus=None):
        """Seed the work queue.

        With no focus this is just the site's main page (the trail's origin), so
        the archive's first page is the main page and every later URL hangs off
        it.  Depth is 1-based (main page = 1) so pagination depth matches the
        trail: a thread reached via main→forum→subforum sits at depth 4, and its
        ``page-65`` at depth 68.

        With a focus it is the breadcrumb chain from the main page *down to* the
        focused section — each ancestor archived but not spiderwebbed — followed
        by the focused section itself, which is the node that spiderwebs.  So
        focusing on ``…/forums/lolcows.16`` archives the main page, then
        ``…/forums``, then crawls within ``…/forums/lolcows.16``; the saved copy
        can then be navigated from the main page straight down to the section.

        Re-seeding is idempotent: ancestors already archived are left untouched
        and the focused section is re-opened only if it had been saved merely as
        a breadcrumb — so changing focus accumulates into the one archive and
        never duplicates it."""
        chain = focus_chain(focus) if focus else [root]
        trail = config.ROOT_TRAIL
        parent = None
        for i, u in enumerate(chain):
            active = (i == len(chain) - 1)
            self.store.enqueue(u, i + 1, trail=trail, parent=parent,
                               section=section_key(u), page_no=1,
                               breadcrumb=0 if active else 1)
            parent = u
            trail = child_trail(trail, 0)
        # Make sure the active section actually expands, even if a previous,
        # narrower crawl had archived it only as a breadcrumb.
        self.store.reopen_active(chain[-1])

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
        # Bound of the focused section, or None for a whole-site crawl.
        focus_path = focus_path_of(s.get("focus_url") or "")

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
                is_breadcrumb = bool(item.get("breadcrumb"))
                cap = max_pages or "∞"
                self.current_url = url
                tag = " (breadcrumb)" if is_breadcrumb else ""
                log("INFO", f"[{processed+1}/{cap} depth={depth}]{tag} {url}")

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

                    # Plan the spiderweb descent (pagination-aware, section-aware,
                    # focus-confined) and enqueue children with materialised-path
                    # trails so the depth-first order is reproduced — and
                    # resumable — from the queue alone.  Breadcrumb pages (the
                    # ancestors on the path down to a focused section) are
                    # archived but never expanded, so the focus stays put.
                    if is_breadcrumb:
                        children = []
                    else:
                        children = plan_children(
                            url, depth, links, max_depth,
                            is_known=self.store.is_settled, focus_path=focus_path)
                    for i, child in enumerate(children):
                        cu = child["url"]
                        self.store.enqueue_or_reopen(
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
        shell instead of binary data is skipped rather than stored.

        Stylesheets are mined recursively: every downloaded CSS file is parsed
        for ``@import`` and ``url(...)`` references (fonts, icon sprites,
        background textures, smilies) and those are queued for download too, so
        the archived theme is complete instead of a stylesheet pointing at
        assets that were never saved.  A worklist + seen-set keeps the recursion
        bounded and resume-safe (CSS already on disk is re-parsed for any new
        references without being re-downloaded)."""
        seen = set()
        queue = list(urls)
        while queue:
            if self._stop.is_set():
                return
            u = queue.pop(0)
            if not u or u in seen:
                continue
            seen.add(u)
            if not in_scope(u):
                continue

            ct, data = None, None
            if self.store.has_asset(u):
                # Already saved: only pull the body back off disk when it's a
                # stylesheet we still need to mine for dependencies on resume —
                # never re-read large media just to type-check it.
                ct = self.store.asset_content_type(u)
                if _is_stylesheet(ct, u):
                    got = self.store.get_asset(u)
                    if got:
                        ct, data = got
            else:
                try:
                    r = self.http.get(u, timeout=30,
                                      headers={"Referer": referer})
                    if r.status_code != 200 or not r.content:
                        continue
                    ct = r.headers.get(
                        "Content-Type",
                        "application/octet-stream").split(";")[0].strip()
                    if ct.startswith("text/html"):   # asset came back as a gate
                        continue
                    self.store.save_asset(u, ct, r.content, source_page=referer)
                    data = r.content
                except Exception as exc:
                    log("WARNING", f"Asset failed {u}: {exc}")
                    continue
                time.sleep(0.05)     # gentle pacing so bursts don't trip limits

            # Follow a stylesheet's own dependencies (fonts/sprites/@imports).
            if data and _is_stylesheet(ct, u):
                try:
                    imports, sub = extract_css_refs(
                        data.decode("utf-8", "ignore"), u)
                    for nu in imports + sub:
                        if nu not in seen:
                            queue.append(nu)
                except Exception as exc:
                    log("WARNING", f"CSS parse failed {u}: {exc}")
