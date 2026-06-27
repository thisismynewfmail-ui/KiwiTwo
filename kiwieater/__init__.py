"""KiwiEater — Offline Archival Mainframe for kiwifarms.st.

A themed Web-UI utility that creates a fully offline, navigable backup of
https://kiwifarms.st .  The crawl is driven by a real browser engine so the
Kiwiflare proof-of-work / "checking your browser" gate is cleared exactly the
way an ordinary visitor's browser clears it.  Pages are stored as portable
JSON, every image/video as a deduplicated BLOB file, and the whole thing is
served back as a navigable, in-universe archive.

This package is intentionally split into focused modules:

    config           paths, constants, default settings
    urls             URL normalisation / scope rules
    logbook          ring-buffer + per-session file + DB logging
    storage          SQLite operational state + JSON/BLOB archive store
    cleaner          structural HTML cleaning (strip extraneous/external)
    browser          real-browser engine + Kiwiflare challenge solver
    crawler          background, pausable, resumable crawl worker
    archive_builder  manifest / gallery / search index + static viewer
    server           Flask console + archive routes
"""

__version__ = "2.0.0"
__all__ = ["__version__"]
