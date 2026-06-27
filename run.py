#!/usr/bin/env python3
"""KiwiEater launcher.

Boots the themed control console and the archive server, opening the console in
your browser automatically.  Run it with::

    python run.py            # or:  python app.py

Optional environment variables:
    KIWIEATER_PORT       console/archive port (default 8777)
    KIWIEATER_CHROMIUM   explicit Chromium binary path
"""

import os
import sys
import time
import logging
import threading
import subprocess
import webbrowser


# --------------------------------------------------------------------------- #
#  Best-effort dependency bootstrap (real libraries only).
# --------------------------------------------------------------------------- #
def _ensure(import_name, pip_name=None):
    try:
        __import__(import_name)
        return True
    except Exception:
        try:
            print(f"[setup] Installing {pip_name or import_name} …")
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                            pip_name or import_name], check=False)
            __import__(import_name)
            return True
        except Exception as exc:
            print(f"[setup] Could not install {pip_name or import_name}: {exc}")
            return False


def _bootstrap():
    _ensure("flask", "flask")
    _ensure("bs4", "beautifulsoup4")
    _ensure("requests", "requests")
    _ensure("lxml", "lxml")
    # Playwright is the preferred browser engine; install the package, but the
    # Chromium binary may need `playwright install chromium` separately.
    if not (_ensure("playwright", "playwright")):
        print("[setup] Playwright unavailable — Selenium + Chrome can be used "
              "instead (see README).")


def main():
    _bootstrap()

    # Imported after bootstrap so freshly installed packages are importable.
    from kiwieater import config
    from kiwieater.logbook import log
    from kiwieater.browser import engines_available
    from kiwieater.server import app, STORE

    config.ensure_dirs()
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
    log("INFO", f"KiwiEater console at http://127.0.0.1:{config.APP_PORT}/")
    eng = engines_available()
    log("INFO", f"Playwright available: {eng['playwright']} | "
                f"Selenium available: {eng['selenium']}")
    pending = STORE.stats()["pending"]
    if pending:
        log("INFO", f"Resumable session detected: {pending} URL(s) pending.")

    def _open_when_ready():
        import requests
        url = f"http://127.0.0.1:{config.APP_PORT}/"
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

    threading.Thread(target=_open_when_ready, daemon=True).start()
    # Bind 0.0.0.0 so the LAN gate can serve other devices when enabled; until
    # then the before_request gate blocks every non-local address.
    app.run(host="0.0.0.0", port=config.APP_PORT, threaded=True,
            debug=False, use_reloader=False)


if __name__ == "__main__":
    # Make sure the repo root is importable when launched from elsewhere.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
