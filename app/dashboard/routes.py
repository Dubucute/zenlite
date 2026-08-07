"""
Dashboard API Routes
Provides status, stats, and provider info for the dashboard UI.
"""

import time
from fastapi import APIRouter, Query

from app.config import PROVIDERS, DASHBOARD_TITLE, DASHBOARD_VERSION
from app.logs import log_buffer
from app.proxy.manager import proxy_manager

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

# Simple uptime tracker
_start_time = time.time()


@router.get("/status")
async def get_status():
    """Get overall gateway status."""
    return {
        "status": "running",
        "title": DASHBOARD_TITLE,
        "version": DASHBOARD_VERSION,
        "uptime_seconds": round(time.time() - _start_time, 1),
    }


@router.get("/providers")
async def get_providers():
    """List all configured providers with their details."""
    providers = []
    for pid, config in PROVIDERS.items():
        providers.append({
            "id": config.id,
            "name": config.name,
            "auth_type": config.auth_type,
            "models": config.models,
            "description": config.description,
        })
    return {"providers": providers}


@router.get("/models")
async def get_models():
    """List all available models."""
    models = []
    for pid, config in PROVIDERS.items():
        for model_id in config.models:
            models.append({
                "id": model_id,
                "provider": config.name,
                "provider_id": config.id,
            })
    return {"models": models}


@router.get("/proxy-stats")
async def get_proxy_stats():
    """Get proxy rotation statistics."""
    return proxy_manager.get_stats()


@router.get("/health")
async def health_check():
    """Health check endpoint for monitoring."""
    return {
        "status": "healthy",
        "uptime": round(time.time() - _start_time, 1),
        "proxy_stats": proxy_manager.get_stats(),
    }


@router.get("/logs")
async def get_logs(limit: int = Query(default=200, ge=1, le=500), after: int = Query(default=0, ge=0)):
    """Get recent log entries for the dashboard log viewer.

    - `limit`: how many entries to return (max 500)
    - `after`: only return entries with `seq` greater than this (for polling)
    """
    entries = log_buffer.snapshot(limit)
    # If the client's cursor is stale (server restarted, seq reset to 0),
    # reset the filter so logs resume instead of freezing forever.
    if after and after > log_buffer.max_seq:
        after = 0
    if after:
        entries = [e for e in entries if e["seq"] > after]
    return {"logs": entries}
