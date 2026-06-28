"""Build the portable, self-describing parts of the backup.

After (or during) a crawl this produces, under ``Archive/``:

    manifest.json        archive metadata + the full page list (navigation)
    search_index.json    {url,title,excerpt} records for character/keyword search
    gallery.json         every image BLOB with its source page
    blobs/blob_index.json  url -> {file, content_type, size, sha256}
    viewer/              a standalone, themed HTML/JS viewer (no Python needed)

Everything here is plain JSON or static files, so the backup can be parsed or
navigated by any tool — the viewer is a convenience, not a requirement.
"""

import os
import json
import shutil
from datetime import datetime

from . import config
from .logbook import log
from .urls import normalize_url


class ArchiveBuilder:
    def __init__(self, store):
        self.store = store

    def _write_json(self, path, obj):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    def build(self):
        config.ensure_dirs()
        pages = self.store.list_pages()
        images = self.store.list_images()
        blob_idx = self.store.blob_index()
        stats = self.store.stats()
        # Normalise the root so it matches the key every page is stored under
        # (the crawler normalises page URLs, and ``DEFAULT_ROOT`` carries a
        # trailing slash); otherwise the viewer's "main page" lookup could miss.
        root = (normalize_url(self.store.get_meta("root_url", config.DEFAULT_ROOT))
                or config.DEFAULT_ROOT)
        session = self.store.get_meta("session_id", "")

        manifest = {
            "archive": "KiwiEater",
            "format_version": 3,
            "target": config.TARGET_HOST,
            "root_url": root,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "session_id": session,
            "counts": {"pages": stats["pages"], "assets": stats["assets"],
                       "bytes": stats["bytes"]},
            # Pages are listed in trail order (main page first, then each
            # section as it was dived through), and each carries its trail /
            # parent / section / page so the navigation structure is part of the
            # portable backup — enough to audit coverage or resume from the
            # files alone.
            "pages": [
                {"url": p["url"], "title": p["title"] or p["url"],
                 "depth": p["depth"],
                 "trail": p.get("trail"), "parent": p.get("parent"),
                 "section": p.get("section"), "page_no": p.get("page_no"),
                 "file": p["file"].replace(os.sep, "/"),
                 "fetched_at": p["fetched_at"]}
                for p in pages],
        }
        self._write_json(config.MANIFEST_PATH, manifest)

        search = [{"url": p["url"], "title": p["title"] or p["url"]}
                  for p in pages]
        # Pull a short excerpt from each page's stored text for offline search.
        for entry in search:
            rec = self.store.get_page(entry["url"]) or {}
            txt = (rec.get("text") or "")[:600]
            entry["excerpt"] = txt
        self._write_json(config.SEARCH_INDEX_PATH,
                         {"generated_at": manifest["generated_at"],
                          "entries": search})

        gallery = [{"url": i["url"],
                    "file": i["file"].replace(os.sep, "/"),
                    "content_type": i["content_type"],
                    "size": i["size"],
                    "source_page": i["source_page"]}
                   for i in images]
        self._write_json(config.GALLERY_PATH,
                         {"generated_at": manifest["generated_at"],
                          "images": gallery})

        self._write_json(config.BLOB_INDEX_PATH,
                         {url: {**rec, "file": rec["file"].replace(os.sep, "/")}
                          for url, rec in blob_idx.items()})

        self._sync_viewer()
        log("INFO", f"Archive built: {stats['pages']} pages, "
                    f"{stats['assets']} assets, {len(gallery)} images.")
        return manifest

    def _sync_viewer(self):
        """Copy the standalone viewer template into the Archive so the backup is
        self-contained and openable on its own."""
        src = config.VIEWER_TEMPLATE_DIR
        if not os.path.isdir(src):
            return
        os.makedirs(config.VIEWER_DIR, exist_ok=True)
        for name in os.listdir(src):
            s = os.path.join(src, name)
            if os.path.isfile(s):
                shutil.copy2(s, os.path.join(config.VIEWER_DIR, name))
