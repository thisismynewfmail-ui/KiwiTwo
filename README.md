# KiwiEater — Offline Archival Mainframe

A themed Web-UI utility that creates a fully **offline, navigable backup** of
[`kiwifarms.st`](https://kiwifarms.st). It renders the site with a *real*
browser engine so the **Kiwiflare** proof-of-work / "checking your browser"
gate is cleared the same way an ordinary visitor's browser clears it, then
writes a **portable JSON + BLOB backup** you can navigate in-app or parse with
any tool.

This tool is purpose-built for one site. Scope is locked to `kiwifarms.st` and
its sub-domains by design.

## Run

```bash
pip install -r requirements.txt
playwright install chromium        # or use Selenium + Chrome instead
python run.py                      # (python app.py still works too)
```

A 1950s-mainframe control console opens automatically at
`http://127.0.0.1:8777/`. Set `KIWIEATER_PORT` to change the port; point
`KIWIEATER_CHROMIUM` at a custom Chromium binary if needed.

## The Kiwiflare fix

The original crawler *detected* the challenge and **backed off**, so it never
got past the front door (`Queue empty, finished` with nothing archived). The
rework solves the gate instead, layering several real strategies
(`kiwieater/browser.py`):

1. Drives a real browser that executes the challenge JavaScript.
2. Applies **stealth** patches (`navigator.webdriver`, plugins, WebGL, etc.).
3. **Waits the proof-of-work out** — polls until the interstitial clears and a
   clearance cookie appears, with a mid-way reload nudge.
4. Reuses a **persistent browser profile** so the clearance cookie survives
   across pages and runs (solve once, archive thousands).
5. **Manual-solve fallback** (headed mode): if automation stalls, you can click
   through once in the visible window and the crawl resumes automatically.
6. Shares clearance cookies with a `requests` session so BLOB downloads pass
   the gate too. Inter-page sleep + jitter and exponential backoff reduce
   blocks and timeouts.

## Project layout

```
run.py / app.py            launcher (auto-installs deps, opens the console)
kiwieater/
  config.py                paths, constants, default settings
  urls.py                  URL normalisation / scope rules
  logbook.py               ring-buffer + per-session file + DB logging
  storage.py               SQLite resume state + JSON/BLOB archive store
  cleaner.py               structural HTML cleaning
  browser.py               real-browser engine + Kiwiflare solver
  crawler.py               background, pausable, resumable crawl worker
  archive_builder.py       manifest / gallery / search + standalone viewer
  server.py                Flask console + archive routes
  webui/console.html       the in-universe 1950s console
  webui/viewer/            standalone themed archive viewer (HTML/CSS/JS)
Archive/                   ← the deliverable backup (generated)
kiwieater_data/            ← operational state (DB, profile, logs; git-ignored)
```

## The backup (`Archive/`)

The portable, software-independent deliverable:

```
Archive/
  manifest.json            archive metadata + full page list (navigation)
  search_index.json        {url,title,excerpt} for character/keyword search
  gallery.json             every image BLOB + its source page
  pages/<hash>.json        one cleaned, structural page per file
  blobs/<ab>/<sha>.<ext>   de-duplicated image/video/CSS BLOB files
  blobs/blob_index.json    url → {file, content_type, size, sha256}
  viewer/                  standalone HTML/JS viewer (no Python required)
```

Everything is plain JSON or static files, so the backup can be parsed or
navigated by anything. Click **OPEN ARCHIVE** in the console (or open
`Archive/viewer/index.html` through any static web server) to browse it: the
first page is the site's main page and the site's own navigation buttons work
because internal links are rewritten to in-archive routes. Images/media/CSS
load from the saved BLOB files in their original places; external content is
never loaded.

## Console features (all wired to real actions)

- **In-universe 1950s computer:** oscilloscope tied to live crawl activity,
  spinning tape reels, status lamps, VU meters, and a teletype log.
- **Directives:** target root, max depth, page limit, inter-page sleep + jitter,
  challenge wait, per-URL retries, browser engine, headless toggle, BLOB
  capture, manual-solve toggle.
- **RESUME / RUN · NEW ARCHIVE · PAUSE · STOP · REBUILD INDEX · OPEN ARCHIVE.**
- **Resume:** the work queue is persisted in SQLite and every page/BLOB is
  written atomically, so a stopped or crashed crawl resumes exactly where it
  left off. Each session writes a log under `kiwieater_data/logs/`.
- **Local-network sharing:** off by default (localhost only); toggle it on to
  serve the console/archive to other devices on your LAN.
