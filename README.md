# KiwiEater — Offline Archival Mainframe

A themed Web-UI utility that creates a fully **offline, navigable backup** of
[`kiwifarms.st`](https://kiwifarms.st). It renders the site with a *real*
browser engine so the **Kiwiflare** proof-of-work / "checking your browser"
gate is cleared the same way an ordinary visitor's browser clears it, then
writes a **portable JSON + BLOB backup** you can navigate in-app or parse with
any tool.

This tool is purpose-built for one site. Scope is locked to `kiwifarms.st` and
its sub-domains by design.

## How it crawls — a section-aware spiderweb

The backup is built **programmatically**, starting from the site's main page and
navigating with the site's own buttons, so coverage is complete and a stopped
crawl resumes exactly where it left off.

- **Spiderwebbing, depth-first, with complete pagination.** From the main page
  it dives into a forum, then a thread, follows that thread **page by page to its
  end**, then **backtracks** to the listing and takes the next thread, then the
  next forum — covering every page along every trail. A thread opens on its
  *most recent* page (e.g. `…/community-feature-submissions.114933/page-700`), so
  the crawler steps systematically **backwards** through it
  (`…/page-700 → …/page-699 → … → page 1`); it also follows the next page when
  one is advertised, so a thread is fully captured no matter which page it was
  entered on. The page count is re-read from each page as it goes, so a thread
  that **gains a post (and a page) mid-backup** is still completed rather than
  cut short. Per-post permalinks (`…/page-700#post-24855389`) collapse onto their
  page — the `#post-…` anchor is dropped — so a post is never archived as a
  separate, duplicate page.

- **Section-relative depth that never truncates a section.** Depth is the length
  of the navigation *trail*. `MAX DEPTH` bounds how far the crawl **fans out into
  new sections** (main → forum → sub-forum → thread); a section's *own* pages are
  **exempt from the cap**, so even a thread hundreds of pages long is archived in
  full. A thread reached via main → forum → sub-forum sits at depth 4, and its
  pages continue from there — `MAX DEPTH` will not stop a thread part-way.

- **A stored trail.** Every queued URL carries a materialised-path `trail`;
  ordering the queue by it both produces the depth-first walk and — because the
  trail is persisted in SQLite — reproduces the *identical* order after a
  Stop/crash. That one fact gives both full coverage and an exact resume. The
  trail, parent, and section of every page are written into the portable backup
  (`manifest.json`) too, so the navigation structure travels with the data.

Set `PAGE LIMIT` to `0` to pull **all** pages (it is only a safety cap);
otherwise the crawl stops after that many pages, still fully resumable.

## Focusing on one section

Leave **FOCUS SECTION** blank for a whole-site crawl. Put a sub-path there —
e.g. `https://kiwifarms.st/forums/lolcows.16/` — to point the crawler at just
that section. The focus is honoured without ever splitting the data: there is
always **one** archive, and a focused crawl simply fills in part of it.

- **The path leading to the section is archived first, so it can be navigated
  to.** Before spiderwebbing the section, KiwiEater walks the breadcrumb chain
  from the main page down to it and archives each step. Focusing on
  `…/forums/lolcows.16/` on a fresh archive therefore saves
  `https://kiwifarms.st/` first, then `https://kiwifarms.st/forums/`, then
  begins crawling within `…/forums/lolcows.16/` — so in the saved copy you can
  click from the main page straight down to the section, exactly like the live
  site. (These ancestor pages are stored as **breadcrumbs**: archived for
  navigation, but not themselves spiderwebbed.)

- **The spiderweb stays inside the section.** From the focused section the crawl
  follows its pages and the threads it contains, but does not climb back to the
  main page or wander into sibling sections. Focusing on a single forum captures
  that forum and its threads; focusing on `…/forums/` captures every forum and
  their threads.

- **Already-saved locations are detected, never re-copied.** Because everything
  lives in one archive and the queue is persisted, changing the focus picks up
  where the last crawl left off. Archive `…/forums/lolcows.16/` first, then set
  the focus to `…/forums/` and the rest of the forums are crawled while
  `lolcows.16` is recognised as already captured and skipped. Clear the focus
  (back to `https://kiwifarms.st/`) and the rest of the whole site is crawled,
  again aware of everything already saved. This is resume-safe across
  pause/stop/restart and never produces a second backup or duplicate pages.

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
  urls.py                  URL normalisation / scope / focus + breadcrumb rules
  logbook.py               ring-buffer + per-session file + DB logging
  storage.py               SQLite resume state + JSON/BLOB archive store
  cleaner.py               structural HTML cleaning
  browser.py               real-browser engine + Kiwiflare solver
  crawler.py               section-aware spiderweb (depth-first, focusable, resumable) worker
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

### A fully themed, self-contained backup

A captured page renders as a fully themed page rather than skeletal white HTML
because **the archive wears the KiwiEater 1950s theme itself** — it does not
depend on re-capturing the live site's own stylesheets (those are served from
behind the Kiwiflare gate and often can't be retrieved, which is exactly what
left earlier backups unstyled):

- **The theme travels with the data.** `viewer/archive-theme.css` — a
  green-phosphor / amber mainframe stylesheet — is bundled into every backup
  and injected into each page when it is viewed. It styles plain HTML *and* the
  XenForo structures KiwiFarms is built from (page nav, blocks, node/thread/
  forum listings, posts, pagination, bb-code), so a page looks fleshed-out and
  deliberate, matching the console's own look.
- **The site's own CSS is dropped, deterministically.** When a page is shown,
  its `<link>`/`<style>` site stylesheets are removed and the archive theme is
  injected last so it always wins — no white page is possible whether or not
  any site CSS was captured.
- **Assets and media are localised.** `<img>`/`<video>`/`<audio>` and
  `srcset`/`<picture>` references (and `url(...)` in inline `style=""`) are
  rewritten to the on-disk BLOB files, so images and media display from the
  archive and the page never reaches out to the live site. Inline vector icons
  (`<svg>` logo/UI sprites) are preserved.

The page bodies themselves stay as `pages/<hash>.json` — the BLOBs hold the
binary assets, and the JSON holds the cleaned structural HTML that the bundled
theme then renders.

## Console features (all wired to real actions)

- **In-universe 1950s computer:** oscilloscope tied to live crawl activity,
  spinning tape reels, status lamps, VU meters, and a teletype log.
- **Directives:** target root, focus section (a sub-path to spiderweb, blank =
  whole site), max depth (section fan-out; a section's own pages are never
  capped), page limit (`0 = ALL`), inter-page sleep + jitter, challenge wait,
  per-URL retries, browser engine, headless toggle, BLOB capture, manual-solve
  toggle.
- **RESUME / RUN · NEW ARCHIVE · PAUSE · STOP · REBUILD INDEX · OPEN ARCHIVE.**
- **Resume:** the work queue is persisted in SQLite and every page/BLOB is
  written atomically, so a stopped or crashed crawl resumes exactly where it
  left off. Each session writes a log under `kiwieater_data/logs/`.
- **Local-network sharing:** off by default (localhost only); toggle it on to
  serve the console/archive to other devices on your LAN.
