"""Central configuration: paths, constants and default crawl settings.

Everything that another module might need to agree on lives here so there is a
single source of truth.  Runtime/operational data goes under ``kiwieater_data``
(git-ignored); the portable deliverable backup goes under ``Archive``.
"""

import os
import glob

# --------------------------------------------------------------------------- #
#  Target — this tool is purpose-built for one site and one site only.
# --------------------------------------------------------------------------- #

TARGET_HOST = "kiwifarms.st"
DEFAULT_ROOT = "https://kiwifarms.st/"

APP_PORT = int(os.environ.get("KIWIEATER_PORT", "8777"))

# --------------------------------------------------------------------------- #
#  Filesystem layout
# --------------------------------------------------------------------------- #

# Repo root = parent of this package directory.
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(PKG_DIR)

WEBUI_DIR = os.path.join(PKG_DIR, "webui")
VIEWER_TEMPLATE_DIR = os.path.join(WEBUI_DIR, "viewer")

# Operational (non-portable) data: queue DB, browser profile, session logs.
DATA_DIR = os.path.join(BASE_DIR, "kiwieater_data")
PROFILE_DIR = os.path.join(DATA_DIR, "browser_profile")
LOG_DIR = os.path.join(DATA_DIR, "logs")
STATE_DB = os.path.join(DATA_DIR, "state.db")

# The deliverable: portable JSON + BLOB backup the user can navigate or parse
# with any tooling, with no dependency on this program.
ARCHIVE_DIR = os.path.join(BASE_DIR, "Archive")
PAGES_DIR = os.path.join(ARCHIVE_DIR, "pages")
BLOBS_DIR = os.path.join(ARCHIVE_DIR, "blobs")
VIEWER_DIR = os.path.join(ARCHIVE_DIR, "viewer")
MANIFEST_PATH = os.path.join(ARCHIVE_DIR, "manifest.json")
SEARCH_INDEX_PATH = os.path.join(ARCHIVE_DIR, "search_index.json")
GALLERY_PATH = os.path.join(ARCHIVE_DIR, "gallery.json")
BLOB_INDEX_PATH = os.path.join(BLOBS_DIR, "blob_index.json")


def ensure_dirs():
    """Create every runtime directory we rely on (idempotent)."""
    for d in (DATA_DIR, PROFILE_DIR, LOG_DIR,
              ARCHIVE_DIR, PAGES_DIR, BLOBS_DIR, VIEWER_DIR):
        os.makedirs(d, exist_ok=True)


# --------------------------------------------------------------------------- #
#  Browser identity / stealth
# --------------------------------------------------------------------------- #

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "en-US,en;q=0.9"
VIEWPORT = {"width": 1366, "height": 900}


def detect_chromium():
    """Locate a Chromium binary: env override first, then common install paths
    (including the pre-provisioned ``/opt/pw-browsers`` used in some
    environments).  Returns ``None`` to let Playwright use its own download."""
    env = os.environ.get("KIWIEATER_CHROMIUM")
    if env and os.path.exists(env):
        return env
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""), "/opt/pw-browsers"]
    for root in roots:
        if not root:
            continue
        for pat in ("chromium-*/chrome-linux/chrome",
                    "chromium*/chrome-linux/chrome",
                    "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
                    "chromium-*/chrome-win/chrome.exe"):
            hits = sorted(glob.glob(os.path.join(root, pat)))
            if hits:
                return hits[-1]
    return None


CHROMIUM_PATH = detect_chromium()

# --------------------------------------------------------------------------- #
#  Challenge / cleaning constants
# --------------------------------------------------------------------------- #

# Strings that betray an anti-bot / proof-of-work interstitial (Kiwiflare,
# Cloudflare, generic "checking your browser" pages).
CHALLENGE_MARKERS = (
    "kiwiflare", "checking your browser", "just a moment",
    "verifying you are human", "verify you are human", "ddos protection",
    "challenge-platform", "cf-challenge", "cf_chl", "proof of work",
    "enable javascript and cookies", "attention required",
    "please stand by, while we are checking", "_cf_chl_opt",
)

# Cookies whose presence means a challenge has been cleared.
CLEARANCE_COOKIES = ("cf_clearance", "kiwiflare", "kiwiflare_clearance",
                     "__ddg1_", "sssg_clearance")

# Tags removed during structural cleaning (extraneous / unsafe / external).
STRIP_TAGS = ("script", "noscript", "iframe", "embed", "object", "svg",
              "ins", "template")

ASSET_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".svg",
             ".mp4", ".webm", ".mov", ".m4v", ".ogg", ".mp3", ".wav",
             ".css", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".pdf")

CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
    "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico",
    "image/svg+xml": ".svg", "video/mp4": ".mp4", "video/webm": ".webm",
    "video/quicktime": ".mov", "audio/mpeg": ".mp3", "audio/ogg": ".ogg",
    "audio/wav": ".wav", "text/css": ".css", "font/woff": ".woff",
    "font/woff2": ".woff2", "font/ttf": ".ttf", "application/pdf": ".pdf",
    "application/font-woff": ".woff", "application/x-font-ttf": ".ttf",
}

# --------------------------------------------------------------------------- #
#  Default crawl settings (mirrored by the console UI)
# --------------------------------------------------------------------------- #

DEFAULT_SETTINGS = {
    "root_url": DEFAULT_ROOT,
    "max_depth": 2,
    "max_pages": 500,
    "sleep": 3.0,            # base inter-page delay, seconds
    "jitter": 1.5,          # +/- random jitter on the delay
    "engine": "auto",       # auto | playwright | selenium
    "headless": False,      # headed is markedly better against the challenge
    "assets": True,         # capture images/video as BLOB files
    "max_attempts": 4,      # per-URL retry budget
    "challenge_timeout": 90,  # seconds to wait for a PoW gate to clear
    "manual_solve": True,   # allow a human to solve a stuck challenge (headed)
}
