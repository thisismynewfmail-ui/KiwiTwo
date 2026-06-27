"""Flask application: the in-universe console, the control API, and static
serving of the portable ``Archive/`` (manifest, page JSON, BLOBs and the
standalone viewer).

The archive is served as static files so the same bytes that sit on disk — the
universal, software-independent backup — are exactly what the viewer renders.
A live-toggleable gate keeps everything localhost-only until the operator opts
into local-network sharing.
"""

import os
import socket

from flask import (Flask, request, jsonify, Response, abort, redirect,
                   send_from_directory)

from . import config
from .logbook import log, recent
from .storage import ArchiveStore
from .archive_builder import ArchiveBuilder
from .crawler import Crawler
from .browser import engines_available
from .urls import normalize_url, in_scope

STORE = ArchiveStore()
BUILDER = ArchiveBuilder(STORE)
CRAWLER = Crawler(STORE, BUILDER)

NETWORK_ENABLED = {"on": False}
LOCALHOST = {"127.0.0.1", "::1", "localhost"}

app = Flask(__name__)

with open(os.path.join(config.WEBUI_DIR, "console.html"), encoding="utf-8") as _fh:
    CONSOLE_HTML = _fh.read()


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
    if NETWORK_ENABLED["on"]:
        return
    remote = (request.remote_addr or "").split("%")[0]
    if remote not in LOCALHOST and remote != "127.0.0.1":
        abort(403, "Local-network access is disabled. Enable it in the console.")


# --------------------------------------------------------------------------- #
#  Console + control API
# --------------------------------------------------------------------------- #
@app.route("/")
def console():
    return Response(CONSOLE_HTML, mimetype="text/html")


@app.route("/api/config")
def api_config():
    return jsonify({
        "target": config.TARGET_HOST,
        "default_root": config.DEFAULT_ROOT,
        "defaults": config.DEFAULT_SETTINGS,
        "engines_available": engines_available(),
        "settings": STORE.get_meta("settings", {}),
        "network_enabled": NETWORK_ENABLED["on"],
        "lan_url": f"http://{local_ip()}:{config.APP_PORT}/",
        "resume_available": STORE.stats()["pending"] > 0,
    })


@app.route("/api/status")
def api_status():
    st = CRAWLER.status()
    st["stats"] = STORE.stats()
    st["network_enabled"] = NETWORK_ENABLED["on"]
    st["log"] = recent(70)
    return jsonify(st)


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode", "resume")
    settings = dict(config.DEFAULT_SETTINGS)
    for k in settings:
        if k in data:
            settings[k] = data[k]
    # Coerce numeric/bool fields defensively.
    for k in ("max_depth", "max_pages", "max_attempts", "challenge_timeout"):
        settings[k] = int(settings[k])
    for k in ("sleep", "jitter"):
        settings[k] = float(settings[k])
    for k in ("headless", "assets", "manual_solve"):
        settings[k] = bool(settings[k])
    ok, msg = CRAWLER.start(settings, mode=mode)
    if not ok:
        log("WARNING", msg)
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


@app.route("/api/rebuild", methods=["POST"])
def api_rebuild():
    try:
        BUILDER.build()
        return jsonify({"ok": True, "message": "Archive indexes rebuilt."})
    except Exception as exc:
        log("ERROR", f"Rebuild failed: {exc}")
        return jsonify({"ok": False, "message": str(exc)})


@app.route("/api/network", methods=["POST"])
def api_network():
    data = request.get_json(force=True, silent=True) or {}
    NETWORK_ENABLED["on"] = bool(data.get("enabled"))
    log("INFO", "Local-network sharing "
                f"{'ENABLED' if NETWORK_ENABLED['on'] else 'disabled'}.")
    return jsonify({"ok": True, "network_enabled": NETWORK_ENABLED["on"],
                    "lan_url": f"http://{local_ip()}:{config.APP_PORT}/"})


# --------------------------------------------------------------------------- #
#  Archive serving  (static JSON + BLOBs + standalone viewer)
# --------------------------------------------------------------------------- #
@app.route("/archive/")
def archive_home():
    if not os.path.exists(config.MANIFEST_PATH):
        # Build whatever exists so first-open is never a dead end.
        try:
            BUILDER.build()
        except Exception:
            pass
    return redirect("/archive/viewer/index.html")


@app.route("/archive/<path:relpath>")
def archive_file(relpath):
    """Serve any file inside the portable Archive directory."""
    full = os.path.normpath(os.path.join(config.ARCHIVE_DIR, relpath))
    if not full.startswith(config.ARCHIVE_DIR):
        abort(403)
    directory, name = os.path.split(full)
    if not os.path.isfile(full):
        abort(404)
    return send_from_directory(directory, name)


# --------------------------------------------------------------------------- #
#  Programmatic helpers
# --------------------------------------------------------------------------- #
@app.route("/api/page")
def api_page():
    u = normalize_url(request.args.get("u", ""))
    if not u or not in_scope(u):
        abort(400)
    rec = STORE.get_page(u)
    if not rec:
        abort(404)
    return jsonify(rec)
