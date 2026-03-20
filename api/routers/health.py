"""Health check endpoint for monitoring."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends

from api.dependencies import get_state
from engine.bitbucket_client import BitbucketClient
from engine.state import StateManager

router = APIRouter(tags=["health"])

_start_time = time.time()


@router.get("/health")
async def health(state: StateManager = Depends(get_state)):
    """Return server health status including DB connectivity and uptime."""
    try:
        await state.get_all_settings()
        db_ok = True
    except Exception:
        db_ok = False

    bitbucket_configured = await BitbucketClient().is_configured()

    return {
        "status": "healthy" if db_ok else "degraded",
        "uptime_seconds": round(time.time() - _start_time),
        "db_ok": db_ok,
        "bitbucket_configured": bitbucket_configured,
    }
