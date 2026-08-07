"""
ZenLite — FastAPI Main Application
AI Gateway with OpenCode Free + Zen providers and proxy rotation.
"""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import (
    HOST,
    PORT,
    DASHBOARD_TITLE,
    DASHBOARD_VERSION,
    ZENLITE_API_KEY,
    OPENAI_BASE_URL,
    USE_FREE_PROXIES,
)
from app.router.chat import router as chat_router
from app.dashboard.routes import router as dashboard_router
from app.logs import install_log_bridge
from app.proxy.manager import proxy_manager

# ── Logging Setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(name)-20s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("zenlite")


# ── Lifespan ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("═" * 60)
    logger.info("  ZenLite v%s — AI Gateway", DASHBOARD_VERSION)
    logger.info("  Listening on http://%s:%s", HOST, PORT)
    logger.info("  Dashboard  → http://%s:%s/", HOST, PORT)
    logger.info("  API Base   → http://%s:%s/v1/", HOST, PORT)
    logger.info("  Upstream   → %s", OPENAI_BASE_URL)
    logger.info(
        "  Auth /v1/* : %s",
        "ENABLED (ZENLITE_API_KEY set)" if ZENLITE_API_KEY else "disabled (open access)",
    )
    logger.info(
        "  Proxies    : %s (%d IPVanish + %d free)",
        "enabled" if USE_FREE_PROXIES else "IPVanish only",
        len(proxy_manager._ipvanish_pool),
        len(proxy_manager._free_pool),
    )
    logger.info("═" * 60)

    # Bridge ZenLite logs into the dashboard's live log viewer.
    install_log_bridge()

    # Create the shared direct-path client before serving, then fetch free
    # proxies on startup and start the background refresh.
    proxy_manager.start()
    await proxy_manager.refresh_proxies()
    proxy_manager.start_refresh_task()

    yield

    proxy_manager.stop_refresh_task()
    await proxy_manager.aclose()
    logger.info("ZenLite shutting down.")


# ── FastAPI App ──────────────────────────────────────────────────────────────
app = FastAPI(
    title=DASHBOARD_TITLE,
    version=DASHBOARD_VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Static files and templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Register routers
app.include_router(chat_router)
app.include_router(dashboard_router)


# ── Optional Gateway Auth ────────────────────────────────────────────────────
@app.middleware("http")
async def gateway_auth(request: Request, call_next):
    """When ZENLITE_API_KEY is set, require `Authorization: Bearer <key>` on
    all /v1/* routes. Off (open access) when the env var is empty."""
    if ZENLITE_API_KEY and request.url.path.startswith("/v1/"):
        auth = request.headers.get("authorization", "")
        if auth.strip() != f"Bearer {ZENLITE_API_KEY}":
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


# ── Request Logging ──────────────────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every request (method, path, status, duration). Registered last so
    it wraps the other middleware and also logs rejected auth requests."""
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "%s %s -> %d (%.0f ms)",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


# ── Root → Dashboard ────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Serve the dashboard."""
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "title": DASHBOARD_TITLE,
            "version": DASHBOARD_VERSION,
        },
    )


@app.get("/health")
async def health():
    """Simple health check."""
    return {"status": "ok", "version": DASHBOARD_VERSION}
