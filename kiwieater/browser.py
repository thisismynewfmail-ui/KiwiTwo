"""Real-browser engine and Kiwiflare challenge solver.

Why the original failed
-----------------------
The error in the bug report —

    Challenge page detected on https://kiwifarms.st; backing off
    Queue empty, finished.

— happened because the old crawler *detected* the Kiwiflare interstitial and
immediately gave up.  Kiwiflare is a JavaScript proof-of-work gate: a real
browser clears it on its own **if you let the script run and wait**.  Backing
off guarantees you never get past the front door, so the queue drains to empty
having archived nothing.

The fix, implemented here, is a layered strategy:

1.  Drive a **real browser** (Playwright → Selenium) that actually executes the
    challenge JavaScript.
2.  **Apply stealth**: hide the `navigator.webdriver` flag and patch the most
    common automation tell-tales so the gate treats us like a normal visitor.
3.  **Wait the proof-of-work out** — poll until the interstitial markers vanish
    *and* a clearance cookie appears, with a mid-way reload nudge for gates that
    re-arm.
4.  **Reuse a persistent profile** so the clearance cookie survives across every
    page and across runs (solve once, archive thousands).
5.  **Manual-solve fallback** (headed mode): if automation stalls, surface the
    visible window so a human can click through once; we detect the moment it
    clears and carry on.
6.  Hand the clearance cookies to a ``requests`` session so BLOB downloads pass
    the gate too, without re-rendering every file.
"""

import time
import random

from . import config
from .logbook import log

# Optional engines — imported lazily so the console still runs without them.
_PLAYWRIGHT = None
_SELENIUM = None
try:
    from playwright.sync_api import sync_playwright
    _PLAYWRIGHT = sync_playwright
except Exception:
    _PLAYWRIGHT = None
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    _SELENIUM = webdriver
except Exception:
    _SELENIUM = None


def engines_available():
    return {"playwright": bool(_PLAYWRIGHT), "selenium": bool(_SELENIUM)}


# Stealth patches applied before any page script runs.
STEALTH_JS = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
const _q = navigator.permissions && navigator.permissions.query;
if (_q) { navigator.permissions.query = (p) =>
    p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : _q(p); }
try {
  const gp = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(p){
    if (p === 37445) return 'Intel Inc.';
    if (p === 37446) return 'Intel Iris OpenGL Engine';
    return gp.call(this, p);
  };
} catch(e){}
"""


def is_challenge_html(html, title=""):
    """True when ``html``/``title`` look like an anti-bot interstitial rather
    than real archived content."""
    blob = (title + " " + (html or "")[:6000]).lower()
    if any(m in blob for m in config.CHALLENGE_MARKERS):
        # Real threads are large and content-rich; challenge shells are tiny.
        return len(html or "") < 18000 or "challenge" in blob or "_cf_chl" in blob
    return False


def _clear_profile_locks():
    """Remove stale single-instance lock files a crashed Chrome may leave, so a
    fresh launch can reclaim the persistent profile directory."""
    import glob
    import os
    for pat in ("SingletonLock", "SingletonCookie", "SingletonSocket",
                "lockfile", "*.lock"):
        for p in glob.glob(os.path.join(config.PROFILE_DIR, pat)):
            try:
                os.remove(p)
            except OSError:
                pass


class BrowserEngine:
    """Pluggable real-browser driver: Playwright preferred, Selenium fallback."""

    def __init__(self, headless=True, engine="auto",
                 challenge_timeout=90, manual_solve=True):
        self.headless = headless
        self.engine = engine
        self.challenge_timeout = challenge_timeout
        self.manual_solve = manual_solve
        self.kind = None
        self._pw = None
        self._ctx = None
        self._page = None
        self._driver = None

    # ------------------------------------------------------------------ #
    #  Lifecycle
    # ------------------------------------------------------------------ #
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
            user_agent=config.USER_AGENT,
            viewport=config.VIEWPORT,
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-features=IsolateOrigins,site-per-process"],
        )
        if config.CHROMIUM_PATH:
            launch_kw["executable_path"] = config.CHROMIUM_PATH
        last_exc = None
        for attempt in range(3):
            _clear_profile_locks()
            try:
                self._ctx = self._pw.chromium.launch_persistent_context(
                    config.PROFILE_DIR, **launch_kw)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                log("WARNING", f"Browser launch {attempt+1}/3 failed "
                               f"(profile busy?); retrying…")
                time.sleep(2.5)
        if last_exc:
            raise last_exc
        try:
            self._ctx.add_init_script(STEALTH_JS)
        except Exception:
            pass
        self._ctx.set_default_timeout(35000)
        self._page = (self._ctx.pages[0] if self._ctx.pages
                      else self._ctx.new_page())

    def _start_selenium(self):
        opts = ChromeOptions()
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_argument(f"--user-data-dir={config.PROFILE_DIR}")
        opts.add_argument(f"--user-agent={config.USER_AGENT}")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument(f"--window-size={config.VIEWPORT['width']},"
                          f"{config.VIEWPORT['height']}")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        if config.CHROMIUM_PATH:
            opts.binary_location = config.CHROMIUM_PATH
        self._driver = _SELENIUM.Chrome(options=opts)
        try:
            self._driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  Navigation + challenge solving
    # ------------------------------------------------------------------ #
    def fetch(self, url, settle=1.4, should_stop=None):
        """Navigate to ``url`` and return ``(html, title)`` once any challenge
        gate is cleared.  ``should_stop`` lets the Stop button interrupt an
        in-flight fetch promptly."""
        stop = should_stop or (lambda: False)
        self._goto(url)
        self._human_jiggle()
        time.sleep(settle)
        html, title = self._content()

        if not is_challenge_html(html, title):
            return html, title

        log("INFO", f"Kiwiflare gate on {url}; solving proof-of-work…")
        return self._solve_challenge(url, stop)

    def _solve_challenge(self, url, stop):
        deadline = time.time() + self.challenge_timeout
        nudged = False
        announced_manual = False
        while time.time() < deadline:
            if stop():
                raise RuntimeError("stopped")
            time.sleep(2.0)
            self._human_jiggle()
            html, title = self._content()
            if not is_challenge_html(html, title):
                log("INFO", "Challenge cleared.")
                return html, title
            if self._has_clearance_cookie():
                # Cookie present but page not yet reloaded — give it a beat.
                self._goto(url)
                time.sleep(2.0)
                html, title = self._content()
                if not is_challenge_html(html, title):
                    log("INFO", "Challenge cleared (clearance cookie).")
                    return html, title
            # One reload nudge at the half-way mark (some gates re-arm).
            if not nudged and time.time() > deadline - self.challenge_timeout / 2:
                nudged = True
                log("INFO", "Nudging the challenge with a reload…")
                self._reload(url)
            # Headed manual-solve fallback when automation is clearly stuck.
            if (self.manual_solve and not self.headless
                    and not announced_manual
                    and time.time() > deadline - self.challenge_timeout / 4):
                announced_manual = True
                log("WARNING", "Challenge persisting — solve it manually in the "
                               "browser window; archiving resumes automatically.")
                deadline += 120        # grant extra time for a human click

        raise RuntimeError("challenge_not_cleared")

    # ------------------------------------------------------------------ #
    #  Low-level driver helpers
    # ------------------------------------------------------------------ #
    def _goto(self, url):
        if self.kind == "playwright":
            self._page.goto(url, wait_until="domcontentloaded", timeout=35000)
            try:
                self._page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
        else:
            self._driver.set_page_load_timeout(35)
            self._driver.get(url)

    def _reload(self, url):
        try:
            if self.kind == "playwright":
                self._page.reload(wait_until="domcontentloaded", timeout=35000)
            else:
                self._driver.refresh()
        except Exception:
            try:
                self._goto(url)
            except Exception:
                pass

    def _human_jiggle(self):
        """Tiny, human-ish mouse movement — cheap insurance against trivially
        behavioural bot checks."""
        try:
            x, y = random.randint(120, 900), random.randint(120, 600)
            if self.kind == "playwright":
                self._page.mouse.move(x, y, steps=4)
            else:
                from selenium.webdriver.common.action_chains import ActionChains
                ActionChains(self._driver).move_by_offset(
                    random.randint(1, 5), random.randint(1, 5)).perform()
        except Exception:
            pass

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

    # ------------------------------------------------------------------ #
    #  Asset capture (through the *cleared* browser session)
    # ------------------------------------------------------------------ #
    def fetch_binary(self, url, referer=None, timeout=30):
        """Download a media/asset BLOB through the browser that already cleared
        the gate, returning ``(content_type, bytes)`` or ``None``.

        This is the reliable path for a gated site.  Kiwiflare/Cloudflare bind a
        clearance cookie to the browser that earned it — its TLS/JA3 fingerprint,
        User-Agent and IP — so a plain ``requests`` call carrying only the copied
        cookie is frequently rejected (an HTML challenge or a 403), which is what
        left attachment images "not in the archive" even though the page itself
        was captured.  Fetching with the browser's own networking reuses that
        exact, accepted session, so the image/video bytes actually come back.

        Returns ``None`` (so the caller can fall back to ``requests``) on any
        error, a non-OK status, an empty body, or an HTML anti-bot shell handed
        back in place of binary data.
        """
        try:
            if self.kind == "playwright":
                return self._fetch_binary_playwright(url, referer, timeout)
            if self.kind == "selenium":
                return self._fetch_binary_selenium(url, referer, timeout)
        except Exception:
            return None
        return None

    def _fetch_binary_playwright(self, url, referer, timeout):
        headers = {"Referer": referer} if referer else {}
        resp = self._ctx.request.get(url, headers=headers,
                                     timeout=timeout * 1000)
        if not resp.ok:
            return None
        ct = (resp.headers.get("content-type", "") or "").split(";")[0].strip()
        if ct.startswith("text/html"):          # a challenge shell, not an asset
            return None
        body = resp.body()
        return (ct, body) if body else None

    def _fetch_binary_selenium(self, url, referer, timeout):
        """Fetch the asset from inside the page (same origin as the asset, so the
        browser's cookies and fingerprint apply) and hand the bytes back as a
        data: URL we decode here."""
        import base64
        script = (
            "const cb = arguments[arguments.length - 1];"
            "fetch(arguments[0], {credentials:'include'})"
            ".then(r => r.ok ? r.blob() : null).then(b => {"
            "  if(!b){cb(null);return;}"
            "  const fr = new FileReader();"
            "  fr.onload = () => cb(fr.result);"
            "  fr.onerror = () => cb(null);"
            "  fr.readAsDataURL(b);"
            "}).catch(() => cb(null));")
        self._driver.set_script_timeout(timeout)
        data_url = self._driver.execute_async_script(script, url)
        if not data_url or not data_url.startswith("data:"):
            return None
        meta, _, b64 = data_url.partition(",")
        ct = meta[5:].split(";")[0].strip()      # strip "data:" and ";base64"
        if ct.startswith("text/html"):
            return None
        try:
            body = base64.b64decode(b64)
        except Exception:
            return None
        return (ct, body) if body else None

    def _has_clearance_cookie(self):
        try:
            names = set(self.cookies().keys())
        except Exception:
            return False
        return any(any(c in n for c in config.CLEARANCE_COOKIES) for n in names)

    def cookies(self):
        try:
            if self.kind == "playwright":
                return {c["name"]: c["value"] for c in self._ctx.cookies()}
            return {c["name"]: c["value"] for c in self._driver.get_cookies()}
        except Exception:
            return {}

    def warm_up(self, root, should_stop=None):
        """Establish clearance against the site root before the main crawl so
        the very first real page already has a valid clearance cookie."""
        try:
            self.fetch(root, settle=2.0, should_stop=should_stop)
            return True
        except Exception as exc:
            log("WARNING", f"Warm-up did not clear immediately: {exc}")
            return False

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
