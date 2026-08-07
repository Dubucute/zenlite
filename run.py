"""
ZenLite — Entry Point
Run this to start the AI gateway server.

Dev (default, same as always):
    python run.py

Production (multiple workers, reload off):
    ZENLITE_WORKERS=4 python run.py

Environment variables:
    ZENLITE_HOST       host to bind (default 127.0.0.1; use 0.0.0.0 to expose)
    ZENLITE_PORT       port (default 8100)
    ZENLITE_WORKERS    number of uvicorn workers (default 1)
    ZENLITE_RELOAD     "1"/"true" to force dev auto-reload (single worker only)
"""

import os

import uvicorn

from app.config import HOST, PORT


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


if __name__ == "__main__":
    workers = int(os.getenv("ZENLITE_WORKERS", "1"))
    if workers < 1:
        workers = 1

    # Reload is a dev feature and uvicorn forbids it with multiple workers.
    reload_enabled = _env_flag("ZENLITE_RELOAD") or (workers == 1 and not os.getenv("ZENLITE_WORKERS"))
    if workers > 1:
        reload_enabled = False

    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=PORT,
        workers=workers,
        reload=reload_enabled,
        log_level="info",
    )
