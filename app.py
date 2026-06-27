#!/usr/bin/env python3
"""
KiwiEater  -  Offline Archive Engine for kiwifarms.st
=====================================================

A single-file, themed Web-UI backup utility purpose-built to create a fully
offline, navigable archive of https://kiwifarms.st .

What it does
------------
* Opens an in-universe "1950s mainframe" control console in your browser the
  moment the script is launched (no text UI).
* Crawls the site with a *real* browser engine (Playwright or Selenium) so the
  Kiwiflare proof-of-work / "checking your browser" challenge is solved the same
  way an ordinary visitor's browser solves it -- by executing the JavaScript and
  *waiting* for the work to finish, instead of backing off (the original bug).
* Stores every page's cleaned structural HTML in SQLite and every image / video
  as a compressed BLOB, de-duplicated by SHA-256.
* Serves the archive back as a self-contained website: the original site's own
  navigation buttons work because internal links are rewritten to local archive
  routes; external/extraneous content is stripped.
* Generates Index, Gallery and a database-backed Search ("character" lookup).
* Keeps a per-session log + a persistent work queue so a cancelled or crashed
  crawl resumes exactly where it left off.
* Optional local-network sharing, configurable inter-page sleep/jitter, depth
  and page limits, headless toggle, and several anti-block strategies.

Run it:   python app.py
"""

import os
import sys
import re
import io
import csv
import json
import html as _html
import time
import math
import queue
import random
import socket
import sqlite3
import hashlib
import logging
import threading
import subprocess
import webbrowser
import collections
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin, urldefrag, quote, unquote

# --------------------------------------------------------------------------- #
#  Dependency bootstrap (best-effort auto-install of real libraries)
# --------------------------------------------------------------------------- #

def _ensure(import_name, pip_name=None):
    """Import a module, attempting a pip install once if it is missing."""
    try:
        return __import__(import_name)
    except Exception:
        try:
            print(f"[setup] Installing {pip_name or import_name} ...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet",
                 pip_name or import_name],
                check=False,
            )
            return __import__(import_name)
        except Exception as exc:  # pragma: no cover - environment dependent
            print(f"[setup] Could not install {pip_name or import_name}: {exc}")
            return None


_ensure("flask", "flask")
_ensure("bs4", "beautifulsoup4")
_ensure("requests", "requests")

from flask import Flask, request, jsonify, Response, abort  # noqa: E402
import requests  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

# Browser engines are optional at import time so the console + archive viewer
# still run even if neither is installed yet; the crawler reports a clear error.
_PLAYWRIGHT = None
_SELENIUM = None
try:
    from playwright.sync_api import sync_playwright  # noqa: E402
    _PLAYWRIGHT = sync_playwright
except Exception:
    _PLAYWRIGHT = None
try:
    from selenium import webdriver  # noqa: E402
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    _SELENIUM = webdriver
except Exception:
    _SELENIUM = None


# --------------------------------------------------------------------------- #
#  Configuration & paths
# --------------------------------------------------------------------------- #

TARGET_HOST = "kiwifarms.st"          # registrable domain this tool is built for
DEFAULT_ROOT = "https://kiwifarms.st/"
APP_PORT = int(os.environ.get("KIWIEATER_PORT", "8777"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "kiwieater_data")
PROFILE_DIR = os.path.join(DATA_DIR, "browser_profile")   # persistent cookies
LOG_DIR = os.path.join(DATA_DIR, "logs")
DB_PATH = os.path.join(DATA_DIR, "archive.db")
for _d in (DATA_DIR, PROFILE_DIR, LOG_DIR):
    os.makedirs(_d, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Optional explicit Chromium binary (env override + common pre-install paths).
# Most users can ignore this: `playwright install chromium` is auto-detected.
def _detect_chromium():
    env = os.environ.get("KIWIEATER_CHROMIUM")
    if env and os.path.exists(env):
        return env
    import glob
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""), "/opt/pw-browsers"]
    for root in roots:
        if not root:
            continue
        for pat in ("chromium-*/chrome-linux/chrome",
                    "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
                    "chromium-*/chrome-win/chrome.exe"):
            hits = sorted(glob.glob(os.path.join(root, pat)))
            if hits:
                return hits[-1]
    return None

CHROMIUM_PATH = _detect_chromium()

# Strings that betray an anti-bot / proof-of-work interstitial.
CHALLENGE_MARKERS = (
    "kiwiflare", "checking your browser", "just a moment",
    "verifying you are human", "verify you are human", "ddos protection",
    "challenge-platform", "cf-challenge", "proof of work", "please wait",
    "enable javascript and cookies", "attention required",
)

# Elements/attributes that are extraneous and stripped during cleaning.
STRIP_TAGS = ("script", "noscript", "iframe", "embed", "object", "svg",
              "ins", "template")
ASSET_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".svg",
             ".mp4", ".webm", ".mov", ".m4v", ".ogg", ".mp3", ".wav",
             ".css", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".pdf")


# --------------------------------------------------------------------------- #
#  Logging  (ring buffer for the UI + per-session file + DB)
# --------------------------------------------------------------------------- #

LOG_BUFFER = collections.deque(maxlen=600)
_LOG_LOCK = threading.Lock()
_SESSION_LOG_FH = None


def _open_session_log(session_id):
    global _SESSION_LOG_FH
    try:
        if _SESSION_LOG_FH:
            _SESSION_LOG_FH.close()
    except Exception:
        pass
    path = os.path.join(LOG_DIR, f"session_{session_id}.log")
    _SESSION_LOG_FH = open(path, "a", encoding="utf-8")
    return path


def log(level, msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [{level}] {msg}"
    with _LOG_LOCK:
        LOG_BUFFER.append({"ts": ts, "level": level, "msg": msg})
        print(line, flush=True)
        if _SESSION_LOG_FH:
            try:
                _SESSION_LOG_FH.write(line + "\n")
                _SESSION_LOG_FH.flush()
            except Exception:
                pass
    try:
        DB.add_log(level, msg)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  URL helpers
# --------------------------------------------------------------------------- #

def normalize_url(url, base=None):
    """Resolve against base, drop fragments, normalise host/trailing slash."""
    if not url:
        return None
    url = url.strip()
    if url.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
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
    host = (p.netloc or "").lower()
    # strip default ports
    host = host.replace(":80", "").replace(":443", "")
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    rebuilt = f"{p.scheme}://{host}{path}"
    if p.query:
        rebuilt += "?" + p.query
    return rebuilt


def in_scope(url):
    """True only for kiwifarms.st and its sub-domains (this tool's whole point)."""
    try:
        host = (urlparse(url).netloc or "").lower().split(":")[0]
    except Exception:
        return False
    return host == TARGET_HOST or host.endswith("." + TARGET_HOST)


def looks_like_asset(url):
    path = urlparse(url).path.lower()
    return path.endswith(ASSET_EXT)


# --------------------------------------------------------------------------- #
#  Database layer  (SQLite, WAL, thread-safe via short-lived connections)
# --------------------------------------------------------------------------- #

class ArchiveDB:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS pages(
                    url TEXT PRIMARY KEY,
                    title TEXT,
                    html TEXT,
                    text TEXT,
                    depth INTEGER,
                    fetched_at TEXT
                );
                CREATE TABLE IF NOT EXISTS assets(
                    url TEXT PRIMARY KEY,
                    content_type TEXT,
                    data BLOB,
                    size INTEGER,
                    sha256 TEXT,
                    fetched_at TEXT
                );
                CREATE TABLE IF NOT EXISTS queue(
                    url TEXT PRIMARY KEY,
                    depth INTEGER,
                    status TEXT,        -- pending / processing / done / failed
                    attempts INTEGER DEFAULT 0,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS links(
                    src TEXT, dst TEXT,
                    PRIMARY KEY(src, dst)
                );
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY, value TEXT
                );
                CREATE TABLE IF NOT EXISTS log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT, level TEXT, msg TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);
                CREATE INDEX IF NOT EXISTS idx_assets_ct ON assets(content_type);
                """
            )
            # Full-text search if FTS5 is compiled in (graceful fallback to LIKE).
            try:
                c.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts "
                    "USING fts5(url, title, text)"
                )
                self.fts = True
            except Exception:
                self.fts = False

    # ---- meta / settings -------------------------------------------------- #
    def set_meta(self, key, value):
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def get_meta(self, key, default=None):
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default

    # ---- log -------------------------------------------------------------- #
    def add_log(self, level, msg):
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO log(ts,level,msg) VALUES(?,?,?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, msg),
            )

    # ---- queue ------------------------------------------------------------ #
    def enqueue(self, url, depth):
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO queue(url,depth,status,updated_at) "
                "VALUES(?,?, 'pending', ?)",
                (url, depth, datetime.now().isoformat()),
            )

    def next_pending(self):
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT url,depth,attempts FROM queue WHERE status='pending' "
                "ORDER BY depth ASC, rowid ASC LIMIT 1"
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE queue SET status='processing', updated_at=? WHERE url=?",
                    (datetime.now().isoformat(), row["url"]),
                )
                return dict(row)
        return None

    def mark(self, url, status, bump_attempt=False):
        with self._lock, self._conn() as c:
            if bump_attempt:
                c.execute(
                    "UPDATE queue SET status=?, attempts=attempts+1, updated_at=? "
                    "WHERE url=?",
                    (status, datetime.now().isoformat(), url),
                )
            else:
                c.execute(
                    "UPDATE queue SET status=?, updated_at=? WHERE url=?",
                    (status, datetime.now().isoformat(), url),
                )

    def requeue_processing(self):
        """On resume, any 'processing' rows from a crash become pending again."""
        with self._lock, self._conn() as c:
            n = c.execute(
                "UPDATE queue SET status='pending' WHERE status='processing'"
            ).rowcount
        return n

    def is_known(self, url):
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM queue WHERE url=?", (url,)
            ).fetchone() is not None

    # ---- pages ------------------------------------------------------------ #
    def save_page(self, url, title, html, text, depth):
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO pages(url,title,html,text,depth,fetched_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
                "title=excluded.title, html=excluded.html, text=excluded.text, "
                "depth=excluded.depth, fetched_at=excluded.fetched_at",
                (url, title, html, text, depth, datetime.now().isoformat()),
            )
            if self.fts:
                try:
                    c.execute("DELETE FROM pages_fts WHERE url=?", (url,))
                    c.execute(
                        "INSERT INTO pages_fts(url,title,text) VALUES(?,?,?)",
                        (url, title, text),
                    )
                except Exception:
                    pass

    def get_page(self, url):
        with self._conn() as c:
            row = c.execute("SELECT * FROM pages WHERE url=?", (url,)).fetchone()
        return dict(row) if row else None

    # ---- assets ----------------------------------------------------------- #
    def has_asset(self, url):
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM assets WHERE url=?", (url,)
            ).fetchone() is not None

    def save_asset(self, url, content_type, data):
        sha = hashlib.sha256(data).hexdigest()
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO assets"
                "(url,content_type,data,size,sha256,fetched_at) "
                "VALUES(?,?,?,?,?,?)",
                (url, content_type, sqlite3.Binary(data), len(data), sha,
                 datetime.now().isoformat()),
            )

    def get_asset(self, url):
        with self._conn() as c:
            row = c.execute(
                "SELECT content_type,data FROM assets WHERE url=?", (url,)
            ).fetchone()
        return dict(row) if row else None

    def add_link(self, src, dst):
        with self._lock, self._conn() as c:
            c.execute("INSERT OR IGNORE INTO links(src,dst) VALUES(?,?)",
                      (src, dst))

    # ---- stats / listings ------------------------------------------------- #
    def stats(self):
        with self._conn() as c:
            q = {r[0]: r[1] for r in c.execute(
                "SELECT status, COUNT(*) FROM queue GROUP BY status")}
            pages = c.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            assets = c.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            nbytes = c.execute(
                "SELECT COALESCE(SUM(size),0) FROM assets").fetchone()[0]
        return {
            "pending": q.get("pending", 0),
            "processing": q.get("processing", 0),
            "done": q.get("done", 0),
            "failed": q.get("failed", 0),
            "pages": pages,
            "assets": assets,
            "bytes": nbytes,
        }

    def list_pages(self, limit=5000):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT url,title,depth FROM pages ORDER BY url LIMIT ?",
                (limit,))]

    def list_images(self, limit=5000):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT url,content_type,size FROM assets "
                "WHERE content_type LIKE 'image/%' ORDER BY rowid DESC LIMIT ?",
                (limit,))]

    def search(self, q, limit=200):
        q = (q or "").strip()
        if not q:
            return []
        with self._conn() as c:
            if self.fts:
                try:
                    safe = q.replace('"', '""')
                    rows = c.execute(
                        "SELECT p.url, p.title, "
                        "snippet(pages_fts,2,'<<','>>','…',12) AS snip "
                        "FROM pages_fts f JOIN pages p ON p.url=f.url "
                        "WHERE pages_fts MATCH ? LIMIT ?",
                        (f'"{safe}"', limit),
                    ).fetchall()
                    return [dict(r) for r in rows]
                except Exception:
                    pass
            like = f"%{q}%"
            rows = c.execute(
                "SELECT url, title, substr(text,1,180) AS snip FROM pages "
                "WHERE title LIKE ? OR text LIKE ? LIMIT ?",
                (like, like, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def wipe_archive(self):
        with self._lock, self._conn() as c:
            for t in ("pages", "assets", "queue", "links", "log"):
                c.execute(f"DELETE FROM {t}")
            if self.fts:
                try:
                    c.execute("DELETE FROM pages_fts")
                except Exception:
                    pass


DB = ArchiveDB(DB_PATH)


# --------------------------------------------------------------------------- #
#  Browser engine  -  the Kiwiflare solver
# --------------------------------------------------------------------------- #
#
#  The original script "detected a challenge and backed off", which guaranteed
#  failure: Kiwiflare is a JavaScript proof-of-work gate that a real browser
#  clears on its own *given a moment to run the script*.  The fix is to drive a
#  real browser, then WAIT (polling) for the interstitial to disappear, and to
#  reuse a persistent profile so the clearance cookie survives between pages and
#  runs.  Several backoff/retry/jitter strategies guard against rate-limiting.
# --------------------------------------------------------------------------- #

def is_challenge_html(html, title=""):
    blob = (title + " " + (html or "")[:4000]).lower()
    if any(m in blob for m in CHALLENGE_MARKERS):
        # Confirm it's an interstitial, not just a page that mentions the words:
        # challenge pages are tiny and lack real article/thread content.
        return len(html or "") < 15000 or "challenge" in blob
    return False


class BrowserEngine:
    """Pluggable real-browser driver: Playwright preferred, Selenium fallback."""

    def __init__(self, headless=True, engine="auto"):
        self.headless = headless
        self.engine = engine
        self.kind = None
        self._pw = None
        self._ctx = None
        self._page = None
        self._driver = None

    def start(self):
        want = self.engine
        if want in ("auto", "playwright") and _PLAYWRIGHT:
            try:
                self._start_playwright()
                self.kind = "playwright"
                log("INFO", "Browser engine: Playwright (Chromium)")
                return
            except Exception as exc:
                log("WARNING", f"Playwright unavailable: {exc}")
        if want in ("auto", "selenium") and _SELENIUM:
            try:
                self._start_selenium()
                self.kind = "selenium"
                log("INFO", "Browser engine: Selenium (Chrome)")
                return
            except Exception as exc:
                log("WARNING", f"Selenium unavailable: {exc}")
        raise RuntimeError(
            "No browser engine available. Install Playwright "
            "(`pip install playwright && playwright install chromium`) "
            "or Selenium + Chrome.")

    def _start_playwright(self):
        self._pw = _PLAYWRIGHT().start()
        launch_kw = dict(
            headless=self.headless,
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        if CHROMIUM_PATH:
            launch_kw["executable_path"] = CHROMIUM_PATH
        self._ctx = self._pw.chromium.launch_persistent_context(
            PROFILE_DIR, **launch_kw)
        # Hide the most obvious automation tell-tale.
        try:
            self._ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
        except Exception:
            pass
        self._page = (self._ctx.pages[0] if self._ctx.pages
                      else self._ctx.new_page())

    def _start_selenium(self):
        opts = ChromeOptions()
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
        opts.add_argument(f"--user-agent={USER_AGENT}")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--window-size=1366,900")
        if CHROMIUM_PATH:
            opts.binary_location = CHROMIUM_PATH
        self._driver = _SELENIUM.Chrome(options=opts)
        try:
            self._driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator,'webdriver',"
                           "{get:()=>undefined})"},
            )
        except Exception:
            pass

    # -- navigation with challenge solving --------------------------------- #
    def fetch(self, url, challenge_timeout=75, settle=1.2):
        """Return rendered (html, title) after clearing any challenge gate."""
        if self.kind == "playwright":
            self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
        else:
            self._driver.set_page_load_timeout(45)
            self._driver.get(url)
        time.sleep(settle)
        html, title = self._content()

        # Wait out the proof-of-work interstitial instead of giving up.
        if is_challenge_html(html, title):
            log("INFO", f"Challenge gate on {url}; solving (waiting for PoW)…")
            deadline = time.time() + challenge_timeout
            reloaded = False
            while time.time() < deadline:
                time.sleep(2.5)
                html, title = self._content()
                if not is_challenge_html(html, title):
                    log("INFO", "Challenge cleared.")
                    break
                # Halfway through, give it one nudge (some PoW gates re-arm).
                if not reloaded and time.time() > deadline - challenge_timeout / 2:
                    reloaded = True
                    try:
                        if self.kind == "playwright":
                            self._page.reload(wait_until="domcontentloaded",
                                              timeout=45000)
                        else:
                            self._driver.refresh()
                    except Exception:
                        pass
            else:
                raise RuntimeError("challenge_not_cleared")
        return html, title

    def _content(self):
        if self.kind == "playwright":
            try:
                return self._page.content(), (self._page.title() or "")
            except Exception:
                return "", ""
        try:
            return self._driver.page_source, (self._driver.title or "")
        except Exception:
            return "", ""

    def cookies(self):
        try:
            if self.kind == "playwright":
                return {c["name"]: c["value"] for c in self._ctx.cookies()}
            return {c["name"]: c["value"]
                    for c in self._driver.get_cookies()}
        except Exception:
            return {}

    def quit(self):
        try:
            if self.kind == "playwright":
                if self._ctx:
                    self._ctx.close()
                if self._pw:
                    self._pw.stop()
            elif self._driver:
                self._driver.quit()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  HTML cleaning  (strip extraneous/external, keep structure + own assets)
# --------------------------------------------------------------------------- #

def clean_html(html, base_url):
    """Return (cleaned_html_with_absolute_urls, plain_text, asset_urls)."""
    soup = BeautifulSoup(html or "", "lxml")

    for tag in soup(list(STRIP_TAGS)):
        tag.decompose()

    # Drop obvious tracking / ad / challenge containers.
    for el in soup.find_all(True):
        cid = " ".join(filter(None, [
            el.get("id", ""), " ".join(el.get("class", []) or [])])).lower()
        if any(k in cid for k in ("advert", "-ads", "ad-", "adsbygoogle",
                                  "analytics", "gtm", "cookie-banner",
                                  "challenge", "cf-", "kiwiflare")):
            el.decompose()
            continue
        # Strip inline event handlers + external resource hints.
        for attr in list(el.attrs):
            if attr.startswith("on"):
                del el[attr]
        if el.name == "link":
            rel = " ".join(el.get("rel", []) or []).lower()
            if rel in ("preconnect", "dns-prefetch", "preload", "prefetch"):
                el.decompose()

    asset_urls = set()

    def _absolutize(value):
        u = normalize_url(value, base_url)
        return u

    # Images / media -> collect + absolutize so the rewriter can localise them.
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-url"):
            if img.get(attr):
                u = _absolutize(img[attr])
                if u and in_scope(u):
                    img["src"] = u
                    asset_urls.add(u)
                break
        if img.get("srcset"):
            del img["srcset"]
        if img.get("loading") is None:
            img["loading"] = "lazy"

    for media in soup.find_all(["video", "audio", "source"]):
        if media.get("src"):
            u = _absolutize(media["src"])
            if u and in_scope(u):
                media["src"] = u
                asset_urls.add(u)

    # Same-domain stylesheets are kept so the archive *looks* like the site.
    for link in soup.find_all("link", rel=lambda r: r and "stylesheet" in r):
        if link.get("href"):
            u = _absolutize(link["href"])
            if u and in_scope(u):
                link["href"] = u
                asset_urls.add(u)
            else:
                link.decompose()

    # Absolutize anchors (rewriting to local routes happens at serve time).
    for a in soup.find_all("a", href=True):
        u = normalize_url(a["href"], base_url)
        if u:
            a["href"] = u

    text = soup.get_text(" ", strip=True)
    return str(soup), text, asset_urls


# --------------------------------------------------------------------------- #
#  Crawler  (background thread, pausable, resumable)
# --------------------------------------------------------------------------- #

class Crawler:
    def __init__(self):
        self.thread = None
        self.state = "idle"          # idle / running / paused / stopping / error / done
        self.current_url = ""
        self.session_id = None
        self.settings = {}
        self._pause = threading.Event()
        self._stop = threading.Event()
        self.browser = None
        self.http = None             # requests session sharing browser cookies

    # ---- lifecycle -------------------------------------------------------- #
    def start(self, settings, mode="resume"):
        if self.state in ("running", "paused"):
            return False, "A crawl is already active."

        self.settings = settings
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        _open_session_log(self.session_id)
        DB.set_meta("settings", settings)
        DB.set_meta("session_id", self.session_id)

        root = normalize_url(settings.get("root_url") or DEFAULT_ROOT)
        if not root or not in_scope(root):
            return False, "Root URL must be on kiwifarms.st."

        if mode == "new":
            DB.wipe_archive()
            log("INFO", "Started a fresh archive (previous data cleared).")
            DB.enqueue(root, 0)
        else:
            requeued = DB.requeue_processing()
            pending = DB.stats()["pending"]
            if pending == 0:
                DB.enqueue(root, 0)
                log("INFO", "No pending work found; seeding from root.")
            else:
                log("INFO", f"Resuming: {pending} URL(s) pending "
                            f"({requeued} recovered from interrupted run).")
        DB.set_meta("root_url", root)

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
        return {
            "state": self.state,
            "current_url": self.current_url,
            "session_id": self.session_id,
            "engine": (self.browser.kind if self.browser else None),
        }

    # ---- main loop -------------------------------------------------------- #
    def _run(self):
        s = self.settings
        max_depth = int(s.get("max_depth", 2))
        max_pages = int(s.get("max_pages", 500))
        sleep_base = float(s.get("sleep", 3.0))
        jitter = float(s.get("jitter", 1.5))
        headless = bool(s.get("headless", True))
        engine = s.get("engine", "auto")
        grab_assets = bool(s.get("assets", True))
        max_attempts = int(s.get("max_attempts", 3))

        try:
            self.browser = BrowserEngine(headless=headless, engine=engine)
            self.browser.start()
        except Exception as exc:
            self.state = "error"
            log("ERROR", f"Could not start browser: {exc}")
            return

        # requests session shares the browser's clearance cookies so asset
        # downloads also pass the gate without re-rendering each file.
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": USER_AGENT,
                                  "Referer": s.get("root_url", DEFAULT_ROOT)})

        log("INFO", f"Warming up session: {s.get('root_url', DEFAULT_ROOT)}")
        processed = 0
        try:
            while not self._stop.is_set():
                # honour pause
                while self._pause.is_set() and not self._stop.is_set():
                    time.sleep(0.4)
                if self._stop.is_set():
                    break

                if processed >= max_pages:
                    log("INFO", f"Reached page limit ({max_pages}).")
                    break

                item = DB.next_pending()
                if not item:
                    log("INFO", "Queue empty, finished.")
                    break

                url, depth, attempts = item["url"], item["depth"], item["attempts"]
                self.current_url = url
                stats = DB.stats()
                log("INFO", f"[{processed+1}/{max_pages} depth={depth}] {url}")

                try:
                    self._sync_cookies()
                    html, title = self.browser.fetch(url)
                    cleaned, text, assets = clean_html(html, url)
                    DB.save_page(url, title, cleaned, text, depth)

                    # enqueue in-scope links
                    new_links = 0
                    for a in BeautifulSoup(cleaned, "lxml").find_all("a", href=True):
                        nu = normalize_url(a["href"], url)
                        if not nu or not in_scope(nu):
                            continue
                        DB.add_link(url, nu)
                        if looks_like_asset(nu):
                            if grab_assets:
                                assets.add(nu)
                            continue
                        if depth + 1 <= max_depth and not DB.is_known(nu):
                            DB.enqueue(nu, depth + 1)
                            new_links += 1

                    if grab_assets:
                        self._grab_assets(assets, url)

                    DB.mark(url, "done")
                    processed += 1
                    log("INFO", f"Saved '{(title or url)[:70]}' "
                                f"(+{new_links} links, {len(assets)} assets)")

                except Exception as exc:
                    attempts += 1
                    if attempts >= max_attempts:
                        DB.mark(url, "failed", bump_attempt=True)
                        log("ERROR", f"Giving up on {url}: {exc}")
                    else:
                        DB.mark(url, "pending", bump_attempt=True)
                        backoff = min(30, 2 ** attempts) + random.random()
                        log("WARNING", f"Error on {url} ({exc}); retry "
                                       f"{attempts}/{max_attempts} after "
                                       f"{backoff:.1f}s")
                        time.sleep(backoff)
                    continue

                # polite, jittered delay between pages (anti-block + anti-timeout)
                delay = max(0.0, sleep_base + random.uniform(-jitter, jitter))
                slept = 0.0
                while slept < delay and not self._stop.is_set():
                    time.sleep(0.2)
                    slept += 0.2

            # end while
            if self._stop.is_set():
                self.state = "idle"
                log("INFO", "Crawl stopped. Progress saved; resume any time.")
            else:
                self.state = "done"
                log("INFO", "Building index, gallery, search…")
                log("INFO", f"=== Crawl complete: {DB.stats()['pages']} pages, "
                            f"{DB.stats()['assets']} assets ===")
        finally:
            try:
                self.browser.quit()
            except Exception:
                pass
            self.current_url = ""

    def _sync_cookies(self):
        try:
            for k, v in self.browser.cookies().items():
                self.http.cookies.set(k, v)
        except Exception:
            pass

    def _grab_assets(self, urls, referer):
        for u in list(urls):
            if self._stop.is_set():
                return
            if not u or not in_scope(u) or DB.has_asset(u):
                continue
            try:
                r = self.http.get(u, timeout=30, headers={"Referer": referer},
                                  stream=True)
                if r.status_code != 200:
                    continue
                ct = r.headers.get("Content-Type", "application/octet-stream")
                ct = ct.split(";")[0].strip()
                data = r.content
                if not data:
                    continue
                # Skip anything that came back as an HTML challenge page.
                if ct.startswith("text/html"):
                    continue
                DB.save_asset(u, ct, data)
            except Exception as exc:
                log("WARNING", f"Asset failed {u}: {exc}")
            # tiny gap so asset bursts don't trip rate limits
            time.sleep(0.05)


CRAWLER = Crawler()


# --------------------------------------------------------------------------- #
#  Archive serving  (rewrite internal links to local routes at serve time)
# --------------------------------------------------------------------------- #

ARCHIVE_TOOLBAR = """
<div id="kiwieater-bar" style="position:sticky;top:0;z-index:99999;
 font-family:'Courier New',monospace;background:#0c1b0c;color:#7CFC00;
 border-bottom:2px solid #2f6f2f;padding:6px 12px;font-size:13px;
 display:flex;gap:14px;align-items:center;box-shadow:0 2px 8px #000">
 <b style="letter-spacing:2px">KIWIEATER ARCHIVE</b>
 <a href="/archive/" style="color:#9dff9d">⌂ Main</a>
 <a href="/archive/index" style="color:#9dff9d">≣ Index</a>
 <a href="/archive/gallery" style="color:#9dff9d">▦ Gallery</a>
 <form action="/archive/search" method="get" style="margin-left:auto;display:flex;gap:6px">
   <input name="q" placeholder="search characters / threads…"
    style="background:#021002;border:1px solid #2f6f2f;color:#7CFC00;
    padding:3px 8px;font-family:inherit">
   <button style="background:#143a14;border:1px solid #2f6f2f;color:#7CFC00;
    cursor:pointer">SEARCH</button>
 </form>
</div>
"""

PLACEHOLDER_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' width='320' height='180'>"
    "<rect width='100%' height='100%' fill='#11220f'/>"
    "<text x='50%' y='50%' fill='#3f7f3f' font-family='monospace' "
    "font-size='14' text-anchor='middle'>image not in archive</text></svg>"
).encode()


def _local_page(url):
    return "/archive/page?u=" + quote(url, safe="")


def _local_asset(url):
    return "/archive/asset?u=" + quote(url, safe="")


def rewrite_for_serving(html, base_url):
    soup = BeautifulSoup(html or "", "lxml")

    # Any leftover scripts removed (defence-in-depth; pages stored clean).
    for tag in soup(["script", "noscript"]):
        tag.decompose()

    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        u = normalize_url(src, base_url)
        if u and in_scope(u):
            img["src"] = _local_asset(u)
        else:
            img["src"] = _local_asset("__placeholder__")  # no external leakage

    for media in soup.find_all(["video", "audio", "source"]):
        src = media.get("src")
        if src:
            u = normalize_url(src, base_url)
            media["src"] = _local_asset(u) if (u and in_scope(u)) else ""

    for link in soup.find_all("link", rel=lambda r: r and "stylesheet" in r):
        href = link.get("href")
        if href:
            u = normalize_url(href, base_url)
            if u and in_scope(u):
                link["href"] = _local_asset(u)
            else:
                link.decompose()

    for a in soup.find_all("a", href=True):
        u = normalize_url(a["href"], base_url)
        if not u:
            continue
        if in_scope(u):
            a["href"] = _local_asset(u) if looks_like_asset(u) else _local_page(u)
        else:
            # keep external hyperlinks clickable, but never embed their content
            a["target"] = "_blank"
            a["rel"] = "noopener noreferrer"
            a["title"] = "external link (not archived)"

    body = soup.body or soup
    bar = BeautifulSoup(ARCHIVE_TOOLBAR, "lxml")
    if soup.body:
        soup.body.insert(0, bar)
    else:
        return ARCHIVE_TOOLBAR + str(soup)
    return str(soup)


# --------------------------------------------------------------------------- #
#  Flask application
# --------------------------------------------------------------------------- #

app = Flask(__name__)
NETWORK_ENABLED = {"on": False}     # live-toggleable LAN gate
LOCALHOST = {"127.0.0.1", "::1", "localhost"}


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.before_request
def _network_gate():
    # When LAN sharing is off, only localhost may reach the console/archive.
    if NETWORK_ENABLED["on"]:
        return
    remote = (request.remote_addr or "").split("%")[0]
    if remote not in LOCALHOST and remote != "127.0.0.1":
        abort(403, "Local-network access is disabled. Enable it in the console.")


# ---- control console -------------------------------------------------------#
@app.route("/")
def console():
    return Response(CONSOLE_HTML, mimetype="text/html")


@app.route("/api/config")
def api_config():
    return jsonify({
        "target": TARGET_HOST,
        "default_root": DEFAULT_ROOT,
        "engines_available": {
            "playwright": bool(_PLAYWRIGHT),
            "selenium": bool(_SELENIUM),
        },
        "settings": DB.get_meta("settings", {}),
        "network_enabled": NETWORK_ENABLED["on"],
        "lan_url": f"http://{local_ip()}:{APP_PORT}/",
        "resume_available": DB.stats()["pending"] > 0,
    })


@app.route("/api/status")
def api_status():
    st = CRAWLER.status()
    st["stats"] = DB.stats()
    st["network_enabled"] = NETWORK_ENABLED["on"]
    with _LOG_LOCK:
        st["log"] = list(LOG_BUFFER)[-60:]
    return jsonify(st)


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode", "resume")
    settings = {
        "root_url": data.get("root_url") or DEFAULT_ROOT,
        "max_depth": int(data.get("max_depth", 2)),
        "max_pages": int(data.get("max_pages", 500)),
        "sleep": float(data.get("sleep", 3.0)),
        "jitter": float(data.get("jitter", 1.5)),
        "headless": bool(data.get("headless", True)),
        "engine": data.get("engine", "auto"),
        "assets": bool(data.get("assets", True)),
        "max_attempts": int(data.get("max_attempts", 3)),
    }
    ok, msg = CRAWLER.start(settings, mode=mode)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/pause", methods=["POST"])
def api_pause():
    return jsonify({"ok": CRAWLER.pause()})


@app.route("/api/resume", methods=["POST"])
def api_resume():
    return jsonify({"ok": CRAWLER.resume()})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    return jsonify({"ok": CRAWLER.stop()})


@app.route("/api/network", methods=["POST"])
def api_network():
    data = request.get_json(force=True, silent=True) or {}
    NETWORK_ENABLED["on"] = bool(data.get("enabled"))
    log("INFO", f"Local-network sharing "
                f"{'ENABLED' if NETWORK_ENABLED['on'] else 'disabled'}.")
    return jsonify({"ok": True, "network_enabled": NETWORK_ENABLED["on"],
                    "lan_url": f"http://{local_ip()}:{APP_PORT}/"})


# ---- archive viewer --------------------------------------------------------#
@app.route("/archive/")
def archive_home():
    root = DB.get_meta("root_url", DEFAULT_ROOT)
    page = DB.get_page(normalize_url(root))
    if not page:
        any_page = DB.list_pages(limit=1)
        if any_page:
            page = DB.get_page(any_page[0]["url"])
    if not page:
        return Response(
            ARCHIVE_TOOLBAR +
            "<div style='font-family:monospace;color:#7CFC00;background:#021002;"
            "padding:40px'>The archive is empty. Run a backup from the "
            "<a href='/' style='color:#9dff9d'>console</a> first.</div>",
            mimetype="text/html")
    return Response(rewrite_for_serving(page["html"], page["url"]),
                    mimetype="text/html")


@app.route("/archive/page")
def archive_page():
    u = normalize_url(unquote(request.args.get("u", "")))
    page = DB.get_page(u) if u else None
    if not page:
        return Response(
            ARCHIVE_TOOLBAR +
            f"<div style='font-family:monospace;color:#7CFC00;"
            f"background:#021002;padding:40px'>This page is not in the archive "
            f"yet:<br><code>{u or ''}</code><br><br>"
            f"<a href='/archive/index' style='color:#9dff9d'>← back to index</a>"
            f"</div>", mimetype="text/html", status=404)
    return Response(rewrite_for_serving(page["html"], page["url"]),
                    mimetype="text/html")


@app.route("/archive/asset")
def archive_asset():
    u = unquote(request.args.get("u", ""))
    if u == "__placeholder__":
        return Response(PLACEHOLDER_SVG, mimetype="image/svg+xml")
    u = normalize_url(u)
    a = DB.get_asset(u) if u else None
    if not a:
        return Response(PLACEHOLDER_SVG, mimetype="image/svg+xml", status=404)
    resp = Response(a["data"], mimetype=a["content_type"] or "application/octet-stream")
    resp.headers["Cache-Control"] = "max-age=86400"
    return resp


@app.route("/archive/index")
def archive_index():
    pages = DB.list_pages()
    rows = "".join(
        f"<tr><td>{p['depth']}</td>"
        f"<td><a href='{_local_page(p['url'])}'>"
        f"{_html.escape(p['title'] or p['url'])}</a></td>"
        f"<td class='u'>{_html.escape(p['url'])}</td></tr>"
        for p in pages)
    body = f"""{ARCHIVE_TOOLBAR}
    <div class='wrap'><h1>Archive Index — {len(pages)} pages</h1>
    <table><thead><tr><th>depth</th><th>title</th><th>url</th></tr></thead>
    <tbody>{rows}</tbody></table></div>{ARCHIVE_CSS}"""
    return Response(body, mimetype="text/html")


@app.route("/archive/gallery")
def archive_gallery():
    imgs = DB.list_images()
    cells = "".join(
        f"<figure><a href='{_local_asset(i['url'])}' target='_blank'>"
        f"<img loading='lazy' src='{_local_asset(i['url'])}'></a>"
        f"<figcaption>{(i['size'] or 0)//1024} KB</figcaption></figure>"
        for i in imgs)
    body = f"""{ARCHIVE_TOOLBAR}
    <div class='wrap'><h1>Gallery — {len(imgs)} images</h1>
    <div class='grid'>{cells or "<p>No images archived yet.</p>"}</div>
    </div>{ARCHIVE_CSS}"""
    return Response(body, mimetype="text/html")


@app.route("/archive/search")
def archive_search():
    q = request.args.get("q", "")
    results = DB.search(q) if q else []
    rows = "".join(
        f"<li><a href='{_local_page(r['url'])}'>"
        f"{_html.escape(r['title'] or r['url'])}</a>"
        f"<div class='snip'>{_html.escape(r.get('snip','') or '')}</div></li>"
        for r in results)
    qsafe = _html.escape(q)
    body = f"""{ARCHIVE_TOOLBAR}
    <div class='wrap'><h1>Search</h1>
    <form method='get' action='/archive/search'>
      <input name='q' value="{qsafe}" autofocus
       placeholder='character name, thread, keyword…'>
      <button>SEARCH</button></form>
    <p>{len(results)} result(s) for <b>{qsafe}</b></p>
    <ul class='results'>{rows}</ul></div>{ARCHIVE_CSS}"""
    return Response(body, mimetype="text/html")


ARCHIVE_CSS = """<style>
 body{margin:0;background:#021002;color:#bfeabf;font-family:'Courier New',monospace}
 .wrap{max-width:1100px;margin:0 auto;padding:24px}
 h1{color:#7CFC00;border-bottom:1px solid #2f6f2f;padding-bottom:8px}
 a{color:#9dff9d}
 table{width:100%;border-collapse:collapse;font-size:13px}
 td,th{border:1px solid #1d3d1d;padding:6px;text-align:left;vertical-align:top}
 th{color:#7CFC00}
 td.u{color:#4f8f4f;font-size:11px;word-break:break-all}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}
 figure{margin:0;background:#0a1a08;border:1px solid #1d3d1d;padding:6px}
 figure img{width:100%;height:130px;object-fit:cover;display:block}
 figcaption{font-size:11px;color:#4f8f4f;text-align:center;margin-top:4px}
 input{background:#0a1a08;border:1px solid #2f6f2f;color:#7CFC00;padding:8px;
  font-family:inherit;width:60%}
 button{background:#143a14;border:1px solid #2f6f2f;color:#7CFC00;padding:8px 16px;
  cursor:pointer;font-family:inherit}
 ul.results{list-style:none;padding:0}
 ul.results li{border-bottom:1px solid #143014;padding:10px 0}
 .snip{color:#6fae6f;font-size:12px;margin-top:4px}
</style>"""


# --------------------------------------------------------------------------- #
#  The console UI  (1950s mainframe; in-universe; live + animated)
# --------------------------------------------------------------------------- #

CONSOLE_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KIWIEATER · ARCHIVAL MAINFRAME</title>
<style>
:root{
 --amber:#ffb000; --amber-dim:#a86b00; --green:#33ff66; --red:#ff4444;
 --panel:#2b2622; --panel2:#1c1916; --steel:#3a3531; --bezel:#0d0c0b;
 --label:#d9c7a3; --glass:#08110a;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{
 background:
   radial-gradient(circle at 30% 20%, #322c26 0, #1a1714 60%, #0c0a09 100%);
 font-family:'Courier New',monospace; color:var(--label);
 overflow-x:hidden; padding:18px;
}
/* ---- chassis ---- */
.chassis{
 max-width:1180px;margin:0 auto;background:linear-gradient(#37312b,#241f1b);
 border:3px solid #4a443d;border-radius:14px;
 box-shadow:0 18px 50px #000, inset 0 2px 0 #5a5249, inset 0 -6px 18px #000;
 padding:18px 20px 26px;
}
.nameplate{
 display:flex;align-items:center;gap:18px;border-bottom:2px solid #4a443d;
 padding-bottom:12px;margin-bottom:16px;
}
.badge{
 width:54px;height:54px;border-radius:8px;flex:0 0 auto;
 background:radial-gradient(circle at 35% 30%,#7CFC00,#0b3b00);
 border:2px solid #0a2a00;box-shadow:0 0 16px #2bff0066, inset 0 0 12px #001a00;
 position:relative;
}
.badge::after{content:"🥝";position:absolute;inset:0;display:flex;
 align-items:center;justify-content:center;font-size:26px}
.title h1{margin:0;font-size:26px;letter-spacing:6px;color:var(--amber);
 text-shadow:0 0 10px #ffb00066}
.title .sub{font-size:11px;letter-spacing:3px;color:var(--amber-dim)}
.serial{margin-left:auto;text-align:right;font-size:11px;color:#8a7d68}
.serial b{color:var(--label)}

.grid{display:grid;grid-template-columns:340px 1fr 300px;gap:16px}
@media(max-width:980px){.grid{grid-template-columns:1fr}}

.bay{background:linear-gradient(#211d19,#171410);border:2px solid #463f38;
 border-radius:10px;padding:14px;box-shadow:inset 0 0 22px #000}
.bay h2{margin:0 0 12px;font-size:12px;letter-spacing:3px;color:var(--amber);
 border-bottom:1px dashed #4a443d;padding-bottom:6px}

/* ---- CRT scope ---- */
.crt{position:relative;background:var(--glass);border-radius:50% / 8%;
 border:10px solid #0b0a09;box-shadow:inset 0 0 40px #000,0 0 0 2px #2a2622;
 overflow:hidden;height:230px}
.crt canvas{width:100%;height:100%;display:block}
.crt::after{content:"";position:absolute;inset:0;pointer-events:none;
 background:repeating-linear-gradient(transparent 0 2px,#00000055 2px 4px);
 animation:roll 8s linear infinite;opacity:.5}
@keyframes roll{from{background-position:0 0}to{background-position:0 200px}}
.scopelabel{position:absolute;left:10px;bottom:8px;color:#1f7a3a;font-size:10px;
 letter-spacing:2px;text-shadow:0 0 6px #00ff5577}

/* ---- reels ---- */
.reels{display:flex;justify-content:space-around;align-items:center;
 padding:10px 0 2px}
.reel{width:96px;height:96px;border-radius:50%;
 background:repeating-radial-gradient(#2a2521 0 6px,#1a1613 6px 12px);
 border:3px solid #524a41;position:relative;box-shadow:inset 0 0 18px #000}
.reel::before{content:"";position:absolute;inset:38px;border-radius:50%;
 background:#0d0b09;border:2px solid #6b6056}
.reel .spoke{position:absolute;inset:6px;border-radius:50%}
.reel .spoke span{position:absolute;left:50%;top:50%;width:6px;height:38px;
 margin:-38px 0 0 -3px;background:#6b6056;transform-origin:bottom center;border-radius:3px}
.spinning{animation:spin 1.6s linear infinite}
.spinning.fast{animation-duration:.5s}
@keyframes spin{to{transform:rotate(360deg)}}

/* ---- lamps ---- */
.lamps{display:flex;gap:14px;flex-wrap:wrap;justify-content:center;margin-top:6px}
.lamp{display:flex;flex-direction:column;align-items:center;gap:5px;font-size:9px;
 letter-spacing:1px;color:#8a7d68;width:58px;text-align:center}
.bulb{width:18px;height:18px;border-radius:50%;background:#3a201a;
 border:2px solid #120a08;box-shadow:inset 0 0 6px #000}
.bulb.on{background:radial-gradient(circle at 35% 30%,#fff,var(--c));
 box-shadow:0 0 14px var(--c),inset 0 0 6px #fff8}
.bulb.blink{animation:blink 1s steps(2) infinite}
@keyframes blink{50%{opacity:.25}}

/* ---- meters ---- */
.meter{margin:10px 0}
.meter .m-top{display:flex;justify-content:space-between;font-size:10px;
 letter-spacing:1px;color:#9c8e76;margin-bottom:3px}
.m-track{height:14px;background:#0c0a08;border:1px solid #463f38;border-radius:3px;
 overflow:hidden;box-shadow:inset 0 0 6px #000}
.m-fill{height:100%;width:0;background:linear-gradient(90deg,#2bff66,#bfff200,#ff9d2b);
 transition:width .5s ease}
.m-val{font-size:11px;color:var(--label)}

/* ---- controls ---- */
.field{margin-bottom:10px}
.field label{display:block;font-size:10px;letter-spacing:2px;color:#9c8e76;
 margin-bottom:4px}
.field input,.field select{width:100%;background:#0c0a08;border:1px solid #5a5249;
 color:var(--amber);padding:7px 8px;font-family:inherit;border-radius:4px}
.row{display:flex;gap:10px}.row>*{flex:1}
.switch{display:flex;align-items:center;gap:8px;cursor:pointer;font-size:11px;
 color:var(--label);margin:8px 0}
.switch .track{width:42px;height:20px;border-radius:10px;background:#1a1613;
 border:1px solid #5a5249;position:relative;transition:.2s}
.switch .knob{position:absolute;top:1px;left:1px;width:16px;height:16px;
 border-radius:50%;background:#8a7d68;transition:.2s}
.switch.on .track{background:#1d4d1d}.switch.on .knob{left:23px;background:#7CFC00}

.btns{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:8px}
button.key{font-family:inherit;letter-spacing:2px;font-weight:bold;cursor:pointer;
 padding:12px 8px;border-radius:6px;border:2px solid #0c0a08;color:#120a08;
 background:linear-gradient(#d9c7a3,#a8946c);box-shadow:0 3px 0 #5e5341,0 5px 8px #000;
 transition:.08s}
button.key:active{transform:translateY(3px);box-shadow:0 0 0 #5e5341}
button.key.go{background:linear-gradient(#7CFC00,#2f9e00);color:#012600}
button.key.stop{background:linear-gradient(#ff7b6b,#c0291a);color:#2a0500}
button.key.warn{background:linear-gradient(#ffd36b,#c9920f)}
button.key:disabled{filter:grayscale(.7) brightness(.7);cursor:not-allowed}
button.key.full{grid-column:1/3}

/* ---- teletype log ---- */
.teletype{background:#07110a;border:2px solid #103a18;border-radius:8px;
 height:230px;overflow:auto;padding:10px;font-size:12px;line-height:1.5;
 color:#33ff66;text-shadow:0 0 4px #00ff5544;box-shadow:inset 0 0 30px #000}
.teletype .l-WARNING{color:#ffd36b}
.teletype .l-ERROR{color:#ff6b5b}
.teletype .ts{color:#1f7a3a}
.statline{display:flex;gap:18px;flex-wrap:wrap;font-size:11px;margin-top:10px;
 color:#9c8e76}
.statline b{color:var(--amber)}
.now{font-size:11px;color:#33ff66;word-break:break-all;min-height:16px}
.foot{margin-top:14px;font-size:10px;color:#6f6353;text-align:center;letter-spacing:2px}
.lan{font-size:11px;color:#7CFC00;margin-top:6px;min-height:14px}
::-webkit-scrollbar{width:10px}::-webkit-scrollbar-thumb{background:#3a3531;border-radius:5px}
</style></head>
<body>
<div class="chassis">
 <div class="nameplate">
  <div class="badge"></div>
  <div class="title"><h1>KIWIEATER</h1>
   <div class="sub">OFFLINE ARCHIVAL MAINFRAME · MODEL KW-1958</div></div>
  <div class="serial">UNIT S/N <b>KE-0007</b><br><span id="clock"></span></div>
 </div>

 <div class="grid">
  <!-- LEFT: controls -->
  <div class="bay">
   <h2>◉ ARCHIVE DIRECTIVES</h2>
   <div class="field"><label>TARGET ROOT (kiwifarms.st only)</label>
    <input id="root" value="https://kiwifarms.st/"></div>
   <div class="row">
    <div class="field"><label>MAX DEPTH</label><input id="depth" type="number" value="2" min="0"></div>
    <div class="field"><label>PAGE LIMIT</label><input id="pages" type="number" value="500" min="1"></div>
   </div>
   <div class="row">
    <div class="field"><label>SLEEP (s)</label><input id="sleep" type="number" value="3" step="0.5" min="0"></div>
    <div class="field"><label>JITTER (±s)</label><input id="jitter" type="number" value="1.5" step="0.5" min="0"></div>
   </div>
   <div class="field"><label>BROWSER ENGINE</label>
    <select id="engine">
     <option value="auto">AUTO (Playwright → Selenium)</option>
     <option value="playwright">PLAYWRIGHT</option>
     <option value="selenium">SELENIUM</option>
    </select></div>
   <div class="switch on" id="sw-assets"><div class="track"><div class="knob"></div></div>
     <span>CAPTURE IMAGES / VIDEO AS BLOBS</span></div>
   <div class="switch" id="sw-headless"><div class="track"><div class="knob"></div></div>
     <span>HEADLESS BROWSER (off = better vs. challenge)</span></div>
   <div class="switch" id="sw-network"><div class="track"><div class="knob"></div></div>
     <span>SHARE OVER LOCAL NETWORK</span></div>
   <div class="lan" id="lan"></div>

   <div class="btns">
    <button class="key go" id="b-start">▶ RESUME / RUN</button>
    <button class="key warn" id="b-new">✦ NEW ARCHIVE</button>
    <button class="key" id="b-pause">❚❚ PAUSE</button>
    <button class="key stop" id="b-stop">■ STOP</button>
    <button class="key full" id="b-open">⧉ OPEN ARCHIVE ›</button>
   </div>
  </div>

  <!-- CENTER: scope + teletype -->
  <div class="bay">
   <h2>◉ SIGNAL MONITOR</h2>
   <div class="crt"><canvas id="scope"></canvas>
     <div class="scopelabel" id="scopelabel">STANDBY</div></div>
   <div class="reels">
     <div class="reel"><div class="spoke" id="reelA"><span style="transform:rotate(0)"></span><span style="transform:rotate(60deg)"></span><span style="transform:rotate(120deg)"></span></div></div>
     <div class="reel"><div class="spoke" id="reelB"><span style="transform:rotate(0)"></span><span style="transform:rotate(60deg)"></span><span style="transform:rotate(120deg)"></span></div></div>
   </div>
   <h2 style="margin-top:14px">◉ TELETYPE LOG</h2>
   <div class="teletype" id="log"></div>
   <div class="now" id="now"></div>
  </div>

  <!-- RIGHT: status -->
  <div class="bay">
   <h2>◉ STATUS PANEL</h2>
   <div class="lamps">
    <div class="lamp"><div class="bulb" id="L-power" style="--c:#33ff66"></div>POWER</div>
    <div class="lamp"><div class="bulb" id="L-run" style="--c:#ffb000"></div>RUN</div>
    <div class="lamp"><div class="bulb" id="L-pause" style="--c:#33b5ff"></div>HOLD</div>
    <div class="lamp"><div class="bulb" id="L-net" style="--c:#7CFC00"></div>NET</div>
    <div class="lamp"><div class="bulb" id="L-err" style="--c:#ff4444"></div>FAULT</div>
   </div>

   <div class="meter"><div class="m-top"><span>QUEUE PENDING</span><span class="m-val" id="v-pending">0</span></div>
     <div class="m-track"><div class="m-fill" id="m-pending"></div></div></div>
   <div class="meter"><div class="m-top"><span>PAGES ARCHIVED</span><span class="m-val" id="v-done">0</span></div>
     <div class="m-track"><div class="m-fill" id="m-done"></div></div></div>
   <div class="meter"><div class="m-top"><span>BLOB ASSETS</span><span class="m-val" id="v-assets">0</span></div>
     <div class="m-track"><div class="m-fill" id="m-assets"></div></div></div>
   <div class="meter"><div class="m-top"><span>FAILED</span><span class="m-val" id="v-failed">0</span></div>
     <div class="m-track"><div class="m-fill" id="m-failed" style="background:linear-gradient(90deg,#ff7b6b,#c0291a)"></div></div></div>

   <div class="statline">
    <div>STATE <b id="s-state">IDLE</b></div>
    <div>ENGINE <b id="s-engine">—</b></div>
    <div>DATA <b id="s-bytes">0 B</b></div>
   </div>
   <div class="foot">VACUUM-TUBE LOGIC · 1958 · FOR AUTHORISED ARCHIVAL USE</div>
  </div>
 </div>
</div>

<script>
const $=id=>document.getElementById(id);
const fmtBytes=n=>{if(!n)return '0 B';const u=['B','KB','MB','GB','TB'];
 let i=Math.floor(Math.log(n)/Math.log(1024));return (n/Math.pow(1024,i)).toFixed(1)+' '+u[i];};

// ---- switches ----
function bindSwitch(id){const el=$(id);el.addEventListener('click',()=>el.classList.toggle('on'));return el;}
const swAssets=bindSwitch('sw-assets'), swHeadless=bindSwitch('sw-headless');
const swNetwork=$('sw-network');
swNetwork.addEventListener('click',async()=>{
  const enabled=!swNetwork.classList.contains('on');
  swNetwork.classList.toggle('on',enabled);
  const r=await fetch('/api/network',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled})}).then(r=>r.json());
  $('lan').textContent=enabled?('LAN ▸ '+r.lan_url):'';
});

function gather(){return{
  root_url:$('root').value, max_depth:+$('depth').value, max_pages:+$('pages').value,
  sleep:+$('sleep').value, jitter:+$('jitter').value, engine:$('engine').value,
  assets:swAssets.classList.contains('on'), headless:swHeadless.classList.contains('on'),
};}
async function post(url,body){return fetch(url,{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})}).then(r=>r.json());}

$('b-start').onclick =()=>post('/api/start',{...gather(),mode:'resume'});
$('b-new').onclick   =()=>{ if(confirm('Erase the current archive and start a NEW backup?'))
                            post('/api/start',{...gather(),mode:'new'});};
$('b-pause').onclick =()=>post('/api/pause');
$('b-stop').onclick  =()=>post('/api/stop');
$('b-open').onclick  =()=>window.open('/archive/','kiwieater_archive');

// ---- clock ----
setInterval(()=>{$('clock').textContent=new Date().toLocaleTimeString();},1000);

// ---- oscilloscope (functional: amplitude ⇄ crawl activity) ----
const cv=$('scope'),cx=cv.getContext('2d');
function sizeScope(){cv.width=cv.clientWidth;cv.height=cv.clientHeight;}
window.addEventListener('resize',sizeScope);sizeScope();
let activity=0, phase=0;
function drawScope(){
 const w=cv.width,h=cv.height;cx.clearRect(0,0,w,h);
 cx.strokeStyle='#0c3a1c';cx.lineWidth=1;
 for(let x=0;x<w;x+=w/12){cx.beginPath();cx.moveTo(x,0);cx.lineTo(x,h);cx.stroke();}
 for(let y=0;y<h;y+=h/6){cx.beginPath();cx.moveTo(0,y);cx.lineTo(w,y);cx.stroke();}
 cx.beginPath();cx.strokeStyle='#33ff66';cx.lineWidth=2;cx.shadowColor='#33ff66';cx.shadowBlur=8;
 const amp=(h/2-10)*(0.12+activity*0.85);
 for(let x=0;x<=w;x++){
   const t=x/w*Math.PI*6+phase;
   const y=h/2+Math.sin(t)*amp*(0.6+0.4*Math.sin(t*0.5+phase*0.7))
          +(activity>0?(Math.random()-0.5)*activity*6:0);
   x===0?cx.moveTo(x,y):cx.lineTo(x,y);
 }
 cx.stroke();cx.shadowBlur=0;
 phase+=0.04+activity*0.22;
 activity*=0.96;                       // decay; refreshed by new log lines
 requestAnimationFrame(drawScope);
}
drawScope();

// ---- reels ----
function setReels(state){
 [reelA,reelB].forEach(r=>{r.classList.remove('spinning','fast');
   if(state==='running'){r.classList.add('spinning','fast');}
   else if(state==='paused'){r.classList.add('spinning');}});
}
const reelA=$('reelA'),reelB=$('reelB');

// ---- poll ----
let lastLogLen=0, maxSeen={pending:1,done:1,assets:1,failed:1};
function setMeter(id,val,key){
 maxSeen[key]=Math.max(maxSeen[key],val,1);
 $('m-'+id).style.width=Math.min(100,val/maxSeen[key]*100)+'%';
 $('v-'+id).textContent=val;
}
async function poll(){
 try{
  const s=await fetch('/api/status').then(r=>r.json());
  const st=s.state||'idle', stats=s.stats||{};
  $('s-state').textContent=st.toUpperCase();
  $('s-engine').textContent=(s.engine||'—');
  $('s-bytes').textContent=fmtBytes(stats.bytes||0);
  setMeter('pending',stats.pending||0,'pending');
  setMeter('done',stats.done||0,'done');
  setMeter('assets',stats.assets||0,'assets');
  setMeter('failed',stats.failed||0,'failed');
  $('now').textContent=s.current_url?('▸ '+s.current_url):'';

  // lamps
  $('L-power').classList.add('on');
  $('L-run').classList.toggle('on',st==='running');
  $('L-run').classList.toggle('blink',st==='running');
  $('L-pause').classList.toggle('on',st==='paused');
  $('L-net').classList.toggle('on',!!s.network_enabled);
  $('L-err').classList.toggle('on',st==='error');
  $('L-err').classList.toggle('blink',st==='error');
  swNetwork.classList.toggle('on',!!s.network_enabled);

  setReels(st);
  $('scopelabel').textContent =
    st==='running'?'ARCHIVING…':st==='paused'?'HOLD':st==='error'?'FAULT':'STANDBY';

  // button enable/disable (every key reflects real state)
  $('b-pause').disabled = st!=='running';
  $('b-stop').disabled  = !(st==='running'||st==='paused');

  // teletype
  const lg=s.log||[];
  if(lg.length!==lastLogLen){
   activity=1;                          // kick the scope on new output
   const box=$('log');
   box.innerHTML=lg.map(e=>`<div class="l-${e.level}"><span class="ts">${e.ts}</span> `
     +`[${e.level}] ${(e.msg||'').replace(/</g,'&lt;')}</div>`).join('');
   box.scrollTop=box.scrollHeight;lastLogLen=lg.length;
  }
 }catch(e){/* server momentarily busy */}
}
setInterval(poll,1000);poll();

// ---- initial config ----
fetch('/api/config').then(r=>r.json()).then(c=>{
 if(c.default_root)$('root').value=c.default_root;
 if(!c.engines_available.playwright){
   [...$('engine').options].find(o=>o.value==='playwright').disabled=true;}
 if(!c.engines_available.selenium){
   [...$('engine').options].find(o=>o.value==='selenium').disabled=true;}
 swNetwork.classList.toggle('on',!!c.network_enabled);
 if(c.network_enabled)$('lan').textContent='LAN ▸ '+c.lan_url;
 if(c.resume_available)$('b-start').textContent='▶ RESUME ('+'pending'+')';
});
</script>
</body></html>"""


# --------------------------------------------------------------------------- #
#  Launch
# --------------------------------------------------------------------------- #

def _open_browser_when_ready():
    url = f"http://127.0.0.1:{APP_PORT}/"
    for _ in range(40):
        try:
            requests.get(url, timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main():
    # quiet Flask's default request logging noise
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    banner = r"""
   __ __ _          _ ______      _
  |  /  (_)        (_)  ____|    | |
  | |  | |_ __      ___ |__   __ _| |_ ___ _ __
  | |\/| | '_ \ /\ / / |  __| / _` | __/ _ \ '__|
  | |  | | | | V  V /| | |___| (_| | ||  __/ |
  |_|  |_|_| |_|\_/\_/ |_|______\__,_|\__\___|_|
        OFFLINE ARCHIVAL MAINFRAME · KW-1958
"""
    print(banner)
    log("INFO", f"KiwiEater console at http://127.0.0.1:{APP_PORT}/")
    log("INFO", f"Playwright available: {bool(_PLAYWRIGHT)} | "
                f"Selenium available: {bool(_SELENIUM)}")
    if DB.stats()["pending"]:
        log("INFO", f"Resumable session detected: "
                    f"{DB.stats()['pending']} URL(s) pending.")

    threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    # Bind to 0.0.0.0 so the optional LAN gate can serve other devices when the
    # operator enables it; until then the before_request gate blocks non-local.
    app.run(host="0.0.0.0", port=APP_PORT, threaded=True, debug=False,
            use_reloader=False)


if __name__ == "__main__":
    main()
