# KiwiEater — Offline Archival Mainframe

A single-file, themed Web-UI utility that creates a fully **offline, navigable
backup** of `kiwifarms.st`. It renders the site with a real browser engine so
the **Kiwiflare** proof-of-work / "checking your browser" gate is cleared the
same way an ordinary visitor's browser clears it, then stores cleaned page
structure in SQLite and every image/video as a compressed BLOB.

## Run

```bash
pip install flask beautifulsoup4 requests playwright
playwright install chromium        # or use Selenium + Chrome instead
python app.py
```

A 1950s-mainframe control console opens automatically at
`http://127.0.0.1:8777/`. (Set `KIWIEATER_PORT` to change the port. If you have
a custom Chromium binary, point `KIWIEATER_CHROMIUM` at it.)

## What it does

- **Console (in-universe 1950s computer):** oscilloscope tied to live crawl
  activity, spinning tape reels, status lamps, VU meters, and a teletype log —
  every control is wired to a real action.
- **Crawl directives:** target root, max depth, page limit, inter-page
  sleep + jitter, browser engine (Playwright/Selenium/auto), headless toggle,
  BLOB capture toggle.
- **Kiwiflare solver:** detects the challenge interstitial and *waits for the
  JavaScript proof-of-work to finish* (with a reload nudge + exponential
  backoff) instead of backing off. A persistent browser profile reuses the
  clearance cookie across pages and runs.
- **Resume:** the work queue is persisted in SQLite; a stopped or crashed crawl
  resumes exactly where it left off (interrupted items are recovered). Each
  session writes a log file under `kiwieater_data/logs/`.
- **Offline archive:** click **OPEN ARCHIVE** to browse the backup in a new
  window. The site's own navigation works because internal links are rewritten
  to local routes; external/extraneous content is stripped. Includes generated
  **Index**, **Gallery**, and database-backed **Search**.
- **Local-network sharing:** off by default (localhost only); toggle it on to
  serve the console/archive to other devices on your LAN.

Scope is locked to `kiwifarms.st` and its sub-domains by design.

All runtime data lives in `kiwieater_data/` (git-ignored).
