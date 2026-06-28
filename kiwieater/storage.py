"""Storage layer.

Two concerns live here, deliberately kept side by side so a page is written
once and stays consistent:

1. **Operational state** (``kiwieater_data/state.db``, SQLite/WAL): the resume
   queue, link graph, per-session meta, a log table and a BLOB de-dup index.
   This is fast, transactional and crash-safe — it is what makes *resume* work.

2. **The portable backup** (``Archive/``): every page as a standalone JSON file
   and every image/video as a de-duplicated BLOB file on disk.  This is the
   deliverable — it can be parsed or navigated by anything, with no dependency
   on this program.

The two are linked by content hashes, never duplicated content.
"""

import os
import json
import shutil
import sqlite3
import hashlib
import threading
from datetime import datetime

from . import config
from . import logbook
from .urls import url_key


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _sniff_ext(data):
    """Best-effort file extension from a BLOB's leading magic bytes.

    Used only when the declared content-type is missing or unrecognised, so a
    real image/video/font served with a vague type (``application/octet-stream``
    and friends — common from behind a CDN/anti-bot edge) is still saved with a
    renderable extension instead of an inert ``.bin``.
    """
    if not data:
        return None
    head = data[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if head.startswith(b"BM"):
        return ".bmp"
    if head.startswith(b"\x00\x00\x01\x00"):
        return ".ico"
    if head.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if head.startswith(b"wOF2"):
        return ".woff2"
    if head.startswith(b"wOFF"):
        return ".woff"
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"OggS"):
        return ".ogg"
    if head.startswith(b"\x1aE\xdf\xa3"):       # Matroska / WebM
        return ".webm"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    if data[4:8] == b"ftyp":                    # ISO base media (mp4/avif/heic)
        brand = data[8:12]
        if brand in (b"avif", b"avis"):
            return ".avif"
        if brand in (b"heic", b"heix", b"mif1", b"msf1"):
            return ".heic"
        return ".mp4"
    stripped = data[:64].lstrip()
    if stripped[:5].lower() == b"<?xml" or stripped[:4].lower() == b"<svg":
        return ".svg"
    return None


class ArchiveStore:
    def __init__(self):
        config.ensure_dirs()
        self.path = config.STATE_DB
        self._lock = threading.Lock()
        self.fts = False
        self._init_db()
        # Persist UI/console logs into SQLite too.
        logbook.register_sink(self._log_sink)

    # ------------------------------------------------------------------ #
    #  Connection / schema
    # ------------------------------------------------------------------ #
    def _conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self):
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS pages(
                    url TEXT PRIMARY KEY, title TEXT, text TEXT,
                    depth INTEGER, file TEXT, fetched_at TEXT,
                    trail TEXT, parent TEXT, section TEXT, page_no INTEGER DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS blobs(
                    url TEXT PRIMARY KEY, sha256 TEXT, content_type TEXT,
                    size INTEGER, file TEXT, source_page TEXT, fetched_at TEXT
                );
                CREATE TABLE IF NOT EXISTS queue(
                    url TEXT PRIMARY KEY, depth INTEGER, status TEXT,
                    attempts INTEGER DEFAULT 0, updated_at TEXT,
                    trail TEXT, parent TEXT, section TEXT, page_no INTEGER DEFAULT 1,
                    breadcrumb INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS links(
                    src TEXT, dst TEXT, PRIMARY KEY(src, dst)
                );
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT, level TEXT, msg TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);
                CREATE INDEX IF NOT EXISTS idx_queue_trail ON queue(trail);
                CREATE INDEX IF NOT EXISTS idx_blobs_ct ON blobs(content_type);
                """
            )
            # Upgrade older databases in place so the trail/section columns the
            # spiderweb crawl relies on exist even on a resumed legacy archive.
            self._migrate(c, "queue", {"trail": "TEXT", "parent": "TEXT",
                                       "section": "TEXT",
                                       "page_no": "INTEGER DEFAULT 1",
                                       "breadcrumb": "INTEGER DEFAULT 0"})
            self._migrate(c, "pages", {"trail": "TEXT", "parent": "TEXT",
                                       "section": "TEXT",
                                       "page_no": "INTEGER DEFAULT 1"})
            try:
                c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts "
                          "USING fts5(url, title, text)")
                self.fts = True
            except Exception:
                self.fts = False

    @staticmethod
    def _migrate(c, table, columns):
        """Add any missing columns to ``table`` (SQLite has no ADD COLUMN IF
        NOT EXISTS, so we diff against ``PRAGMA table_info``)."""
        have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # ------------------------------------------------------------------ #
    #  meta / settings
    # ------------------------------------------------------------------ #
    def set_meta(self, key, value):
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value)))

    def get_meta(self, key, default=None):
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default

    # ------------------------------------------------------------------ #
    #  log table sink
    # ------------------------------------------------------------------ #
    def _log_sink(self, level, msg):
        try:
            with self._lock, self._conn() as c:
                c.execute("INSERT INTO log(ts,level,msg) VALUES(?,?,?)",
                          (_now(), level, msg))
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  Queue (resume engine)
    # ------------------------------------------------------------------ #
    def enqueue(self, url, depth, trail="", parent=None, section="", page_no=1,
                breadcrumb=0):
        """Queue ``url`` if it is not already known.  ``breadcrumb=1`` marks a
        page that should be archived for navigation but *not* spiderwebbed (an
        ancestor on the path down to a focused section).  ``INSERT OR IGNORE``
        keeps re-seeding idempotent — an already-known URL is left exactly as it
        is, so changing focus accumulates into the one archive."""
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO queue"
                "(url,depth,status,updated_at,trail,parent,section,page_no,"
                "breadcrumb) VALUES(?,?, 'pending', ?,?,?,?,?,?)",
                (url, depth, _now(), trail, parent, section, page_no,
                 1 if breadcrumb else 0))

    def enqueue_or_reopen(self, url, depth, trail="", parent=None, section="",
                          page_no=1):
        """Queue a spiderweb child (always a fully-expandable, non-breadcrumb
        node).  If the URL was previously archived only as a *breadcrumb* — saved
        as a stepping-stone for an earlier focused crawl but never expanded — it
        is re-opened so the broader crawl now reaching it explores it, reusing
        the existing saved page rather than making a duplicate."""
        with self._lock, self._conn() as c:
            row = c.execute("SELECT breadcrumb FROM queue WHERE url=?",
                            (url,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO queue(url,depth,status,updated_at,trail,parent,"
                    "section,page_no,breadcrumb) VALUES(?,?, 'pending', ?,?,?,?,?,0)",
                    (url, depth, _now(), trail, parent, section, page_no))
            elif row["breadcrumb"]:
                c.execute("UPDATE queue SET breadcrumb=0, status='pending', "
                          "updated_at=? WHERE url=?", (_now(), url))
            # else: already a full (expandable) node — leave it untouched.

    def reopen_active(self, url):
        """Make ``url`` the live crawl front again if it had been archived merely
        as a breadcrumb, so focusing on a section that was earlier only passed
        through (e.g. focusing ``/forums`` after ``/forums/lolcows.16``, or
        switching back to the whole site) actually expands it.  A node that is
        already expandable (or in flight) is left as-is, so a plain resume never
        needlessly re-fetches work that is already done."""
        with self._lock, self._conn() as c:
            row = c.execute("SELECT breadcrumb FROM queue WHERE url=?",
                            (url,)).fetchone()
            if row is not None and row["breadcrumb"]:
                c.execute("UPDATE queue SET breadcrumb=0, status='pending', "
                          "attempts=0, updated_at=? WHERE url=?", (_now(), url))

    def next_pending(self):
        # ``trail`` is a materialised path, so ordering by it lexically yields a
        # depth-first (spiderweb) pre-order — and, because it is persisted, the
        # *same* order after a Stop/crash, which is what makes resume exact.
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT url,depth,attempts,trail,parent,section,page_no,"
                "breadcrumb FROM queue WHERE status='pending' "
                "ORDER BY (trail IS NULL), trail ASC, depth ASC, rowid ASC "
                "LIMIT 1").fetchone()
            if row:
                c.execute("UPDATE queue SET status='processing', updated_at=? "
                          "WHERE url=?", (_now(), row["url"]))
                return dict(row)
        return None

    def mark(self, url, status, bump_attempt=False):
        with self._lock, self._conn() as c:
            if bump_attempt:
                c.execute("UPDATE queue SET status=?, attempts=attempts+1, "
                          "updated_at=? WHERE url=?", (status, _now(), url))
            else:
                c.execute("UPDATE queue SET status=?, updated_at=? WHERE url=?",
                          (status, _now(), url))

    def requeue_processing(self):
        """Recover any 'processing' rows left behind by a crash -> pending."""
        with self._lock, self._conn() as c:
            return c.execute("UPDATE queue SET status='pending' "
                             "WHERE status='processing'").rowcount

    def is_known(self, url):
        with self._conn() as c:
            return c.execute("SELECT 1 FROM queue WHERE url=?",
                             (url,)).fetchone() is not None

    def is_settled(self, url):
        """True if ``url`` is already a full (expandable) queue node — queued,
        in flight or done as part of a spiderweb.  Used to skip re-planning work
        that is already covered.  A page saved only as a *breadcrumb* is **not**
        settled: it still needs expanding if a crawl descends into it, which is
        what lets a later, broader focus pick up where a narrower one left off
        without ever duplicating the archive."""
        with self._conn() as c:
            return c.execute("SELECT 1 FROM queue WHERE url=? AND breadcrumb=0",
                             (url,)).fetchone() is not None

    # ------------------------------------------------------------------ #
    #  Pages  (SQLite index + JSON file body)
    # ------------------------------------------------------------------ #
    def save_page(self, url, title, html, text, depth, links, assets,
                  trail="", parent=None, section="", page_no=1):
        key = url_key(url)
        rel = os.path.join("pages", f"{key}.json")
        abspath = os.path.join(config.ARCHIVE_DIR, rel)
        record = {
            "url": url, "title": title, "depth": depth,
            "trail": trail, "parent": parent, "section": section,
            "page_no": page_no,
            "fetched_at": _now(), "html": html, "text": text,
            "links": links, "assets": sorted(assets),
        }
        tmp = abspath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, abspath)        # atomic write (resume-safe)

        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO pages(url,title,text,depth,file,fetched_at,"
                "trail,parent,section,page_no) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
                "title=excluded.title, text=excluded.text, depth=excluded.depth,"
                "file=excluded.file, fetched_at=excluded.fetched_at, "
                "trail=excluded.trail, parent=excluded.parent, "
                "section=excluded.section, page_no=excluded.page_no",
                (url, title, text, depth, rel, record["fetched_at"],
                 trail, parent, section, page_no))
            if self.fts:
                try:
                    c.execute("DELETE FROM pages_fts WHERE url=?", (url,))
                    c.execute("INSERT INTO pages_fts(url,title,text) "
                              "VALUES(?,?,?)", (url, title, text))
                except Exception:
                    pass

    def get_page(self, url):
        """Return the full page record (JSON body merged with index row)."""
        with self._conn() as c:
            row = c.execute("SELECT * FROM pages WHERE url=?", (url,)).fetchone()
        if not row:
            return None
        abspath = os.path.join(config.ARCHIVE_DIR, row["file"])
        try:
            with open(abspath, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return dict(row)

    def page_exists(self, url):
        with self._conn() as c:
            return c.execute("SELECT 1 FROM pages WHERE url=?",
                             (url,)).fetchone() is not None

    def list_pages(self, limit=200000):
        # Order by trail so the manifest/index reflect the real navigation trail
        # (home first, then each section dived through in spiderweb order).
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT url,title,depth,file,fetched_at,trail,parent,section,"
                "page_no FROM pages "
                "ORDER BY (trail IS NULL), trail ASC, depth ASC, url ASC "
                "LIMIT ?", (limit,))]

    # ------------------------------------------------------------------ #
    #  BLOB assets  (de-duplicated files on disk)
    # ------------------------------------------------------------------ #
    def has_asset(self, url):
        with self._conn() as c:
            return c.execute("SELECT 1 FROM blobs WHERE url=?",
                             (url,)).fetchone() is not None

    def _asset_ext(self, content_type, url, data=b""):
        """Decide a BLOB's on-disk extension.

        The extension matters: the archive is served as static files and a
        browser will only render an image/video that arrives with a sensible
        type, which static servers infer from the file extension.  So we resolve
        it from the most reliable signal down to the least:

        1. the declared ``content_type`` (XenForo serves correct types),
        2. the bytes themselves (a magic-number sniff — this is what rescues a
           real image delivered with a missing/odd content-type from becoming an
           unrenderable ``.bin``),
        3. the URL's own extension, and finally ``.bin``.
        """
        ext = config.CONTENT_TYPE_EXT.get((content_type or "").lower())
        if ext:
            return ext
        ext = _sniff_ext(data)
        if ext:
            return ext
        base = os.path.splitext(url.split("?")[0])[1].lower()
        return base if base and len(base) <= 6 and base.isascii() else ".bin"

    def _blob_paths(self, sha, ext):
        rel = os.path.join("blobs", sha[:2], f"{sha}{ext}")
        return rel, os.path.join(config.ARCHIVE_DIR, rel)

    def save_asset(self, url, content_type, data, source_page=""):
        """Persist a media/asset BLOB to disk, de-duplicated by SHA-256."""
        sha = hashlib.sha256(data).hexdigest()
        rel, abspath = self._blob_paths(sha, self._asset_ext(content_type, url, data))
        if not os.path.exists(abspath):
            os.makedirs(os.path.dirname(abspath), exist_ok=True)
            tmp = abspath + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, abspath)
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO blobs"
                      "(url,sha256,content_type,size,file,source_page,fetched_at)"
                      " VALUES(?,?,?,?,?,?,?)",
                      (url, sha, content_type, len(data), rel,
                       source_page, _now()))
        return rel

    def asset_content_type(self, url):
        """Return just the stored content-type for an asset (no body read), or
        ``None``.  Lets the crawler decide whether a resumed asset is a
        stylesheet worth re-parsing without loading large media off disk."""
        with self._conn() as c:
            row = c.execute("SELECT content_type FROM blobs WHERE url=?",
                            (url,)).fetchone()
        return row["content_type"] if row else None

    def get_asset(self, url):
        """Return ``(content_type, bytes)`` for a stored asset, or ``None``."""
        with self._conn() as c:
            row = c.execute("SELECT content_type,file FROM blobs WHERE url=?",
                            (url,)).fetchone()
        if not row:
            return None
        abspath = os.path.join(config.ARCHIVE_DIR, row["file"])
        try:
            with open(abspath, "rb") as fh:
                return row["content_type"], fh.read()
        except Exception:
            return None

    def list_images(self, limit=20000):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT url,content_type,size,file,source_page FROM blobs "
                "WHERE content_type LIKE 'image/%' "
                "ORDER BY rowid DESC LIMIT ?", (limit,))]

    def blob_index(self):
        with self._conn() as c:
            return {r["url"]: {"file": r["file"], "content_type": r["content_type"],
                               "size": r["size"], "sha256": r["sha256"]}
                    for r in c.execute(
                        "SELECT url,file,content_type,size,sha256 FROM blobs")}

    # ------------------------------------------------------------------ #
    #  Links
    # ------------------------------------------------------------------ #
    def add_link(self, src, dst):
        with self._lock, self._conn() as c:
            c.execute("INSERT OR IGNORE INTO links(src,dst) VALUES(?,?)",
                      (src, dst))

    # ------------------------------------------------------------------ #
    #  Search
    # ------------------------------------------------------------------ #
    def search(self, q, limit=300):
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
                        (f'"{safe}"', limit)).fetchall()
                    return [dict(r) for r in rows]
                except Exception:
                    pass
            like = f"%{q}%"
            rows = c.execute(
                "SELECT url, title, substr(text,1,180) AS snip FROM pages "
                "WHERE title LIKE ? OR text LIKE ? LIMIT ?",
                (like, like, limit)).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    #  Stats / lifecycle
    # ------------------------------------------------------------------ #
    def stats(self):
        with self._conn() as c:
            q = {r[0]: r[1] for r in c.execute(
                "SELECT status, COUNT(*) FROM queue GROUP BY status")}
            pages = c.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            assets = c.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
            nbytes = c.execute(
                "SELECT COALESCE(SUM(size),0) FROM blobs").fetchone()[0]
        return {"pending": q.get("pending", 0),
                "processing": q.get("processing", 0),
                "done": q.get("done", 0), "failed": q.get("failed", 0),
                "pages": pages, "assets": assets, "bytes": nbytes}

    def wipe_archive(self):
        """Erase everything — operational tables and the portable backup —
        so a NEW archive truly starts from a clean slate."""
        with self._lock, self._conn() as c:
            for t in ("pages", "blobs", "queue", "links", "log"):
                c.execute(f"DELETE FROM {t}")
            if self.fts:
                try:
                    c.execute("DELETE FROM pages_fts")
                except Exception:
                    pass
        for d in (config.PAGES_DIR, config.BLOBS_DIR):
            shutil.rmtree(d, ignore_errors=True)
        for f in (config.MANIFEST_PATH, config.SEARCH_INDEX_PATH,
                  config.GALLERY_PATH):
            try:
                os.remove(f)
            except OSError:
                pass
        config.ensure_dirs()
