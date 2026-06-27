"""Background crawl worker: pausable, stoppable, and reliably resumable.

The crawler owns one browser, drains the persistent work queue breadth-first,
cleans each page, writes it to the portable archive, harvests its in-scope
links and BLOB assets, and rebuilds the navigation indexes when it finishes.

Resilience is the whole point here:

* Every unit of work is a queue row in SQLite; a Stop/crash leaves the row as
  ``pending`` (or recovers ``processing`` rows on the next start), so a resumed
  run continues exactly where it left off.
* Per-URL retries use exponential backoff with jitter.
* Inter-page sleep + jitter, a warm-up handshake, persistent clearance cookies
  and a shared ``requests`` session for assets all reduce timeouts and blocks.
"""

import time
import random
import threading
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from . import config
from .logbook import log, open_session_log
from .browser import BrowserEngine
from .cleaner import clean_html
from .urls import normalize_url, in_scope, looks_like_asset


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
            self.store.enqueue(root, 0)
        else:
            recovered = self.store.requeue_processing()
            pending = self.store.stats()["pending"]
            if pending == 0:
                self.store.enqueue(root, 0)
                log("INFO", "No pending work found; seeding from root.")
            else:
                log("INFO", f"Resuming: {pending} URL(s) pending "
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

    # ------------------------------------------------------------------ #
    #  Worker thread
    # ------------------------------------------------------------------ #
    def _run(self):
        s = self.settings
        max_depth = int(s.get("max_depth", 2))
        max_pages = int(s.get("max_pages", 500))
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
                if processed >= max_pages:
                    log("INFO", f"Reached page limit ({max_pages}).")
                    break

                item = self.store.next_pending()
                if not item:
                    log("INFO", "Queue empty, finished.")
                    break

                url, depth, attempts = item["url"], item["depth"], item["attempts"]
                self.current_url = url
                log("INFO", f"[{processed+1}/{max_pages} depth={depth}] {url}")

                try:
                    self._sync_cookies()
                    html, title = self.browser.fetch(
                        url, should_stop=self._stop.is_set)
                    cleaned, text, assets, links = clean_html(html, url)

                    new_links = 0
                    for link in links:
                        nu = link["href"]
                        if not nu or not in_scope(nu):
                            continue
                        self.store.add_link(url, nu)
                        if looks_like_asset(nu):
                            if grab_assets:
                                assets.add(nu)
                            continue
                        if depth + 1 <= max_depth and not self.store.is_known(nu):
                            self.store.enqueue(nu, depth + 1)
                            new_links += 1

                    self.store.save_page(url, title, cleaned, text, depth,
                                         links, assets)
                    if grab_assets:
                        self._grab_assets(assets, url)

                    self.store.mark(url, "done")
                    processed += 1
                    log("INFO", f"Saved '{(title or url)[:70]}' "
                                f"(+{new_links} links, {len(assets)} assets)")

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
