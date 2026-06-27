"""Logging: a ring buffer for the live UI, a per-session file, and pluggable
sinks (the storage layer registers one to persist logs in SQLite).

Decoupling the sink registration avoids a circular import between this module
and ``storage``.
"""

import os
import threading
import collections
from datetime import datetime

from . import config

LOG_BUFFER = collections.deque(maxlen=800)
_LOCK = threading.Lock()
_SESSION_FH = None
_SINKS = []          # list of callables(level, msg) -> None


def register_sink(fn):
    """Register an extra log destination (e.g. the SQLite log table)."""
    if fn not in _SINKS:
        _SINKS.append(fn)


def open_session_log(session_id):
    """Open (append) a per-session log file and return its path."""
    global _SESSION_FH
    config.ensure_dirs()
    try:
        if _SESSION_FH:
            _SESSION_FH.close()
    except Exception:
        pass
    path = os.path.join(config.LOG_DIR, f"session_{session_id}.log")
    _SESSION_FH = open(path, "a", encoding="utf-8")
    return path


def log(level, msg):
    """Emit a log line everywhere: stdout, ring buffer, session file, sinks."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [{level}] {msg}"
    with _LOCK:
        LOG_BUFFER.append({"ts": ts, "level": level, "msg": str(msg)})
        print(line, flush=True)
        if _SESSION_FH:
            try:
                _SESSION_FH.write(line + "\n")
                _SESSION_FH.flush()
            except Exception:
                pass
    for sink in list(_SINKS):
        try:
            sink(level, str(msg))
        except Exception:
            pass


def recent(n=80):
    """Return the most recent ``n`` log records for the UI."""
    with _LOCK:
        return list(LOG_BUFFER)[-n:]
