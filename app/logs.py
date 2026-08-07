"""
In-memory log ring buffer for the dashboard's live log viewer.

ZenLite's own loggers (zenlite, zenlite.router, zenlite.proxy, ...) write
into a shared ring buffer via a custom logging.Handler. The dashboard polls
`/dashboard/logs` and renders the last N entries live.
"""

import logging
import threading
import time
from collections import deque

# ── Ring Buffer ─────────────────────────────────────────────────────────────

MAX_ENTRIES = 500


class LogBuffer:
    """Thread-safe, bounded log buffer with a simple monotonic sequence."""

    def __init__(self, max_entries: int = MAX_ENTRIES):
        self._entries: deque[dict] = deque(maxlen=max_entries)
        self._lock = threading.Lock()
        self._seq = 0

    def append(self, level: str, logger: str, message: str, ts: float) -> dict:
        with self._lock:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "time": time.strftime("%H:%M:%S", time.localtime(ts)),
                "level": level,
                "logger": logger,
                "message": message,
            }
            self._entries.append(entry)
            return entry

    def snapshot(self, limit: int = 200) -> list[dict]:
        """Return the most recent `limit` entries, oldest first."""
        with self._lock:
            items = list(self._entries)
        return items[-limit:]

    @property
    def max_seq(self) -> int:
        """Highest sequence number ever issued (detects server restarts)."""
        with self._lock:
            return self._seq


log_buffer = LogBuffer()


# ── Logging Handler ──────────────────────────────────────────────────────────

class DashboardLogHandler(logging.Handler):
    """Bridges Python logging into the shared ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                # Include traceback text so errors are actually diagnosable.
                import traceback

                message += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
            log_buffer.append(record.levelname, record.name, message, record.created)
        except Exception:
            # Never let logging itself break the app.
            pass


def install_log_bridge() -> None:
    """Attach the dashboard log handler to the root logger, once."""
    logger = logging.getLogger("zenlite")
    if any(isinstance(h, DashboardLogHandler) for h in logger.handlers):
        return
    handler = DashboardLogHandler()
    handler.setLevel(logging.INFO)
    # "zenlite" is the app's root namespace; attach to it so all child
    # loggers (zenlite.router, zenlite.proxy, ...) flow into the buffer.
    logger.addHandler(handler)
