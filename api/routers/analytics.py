"""Analytics endpoints — retrospective data from completed tickets."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.dependencies import get_state
from engine.state import StateManager

router = APIRouter(tags=["analytics"])


@router.get("/api/analytics/run/{run_id}")
async def run_analytics(run_id: str, state: StateManager = Depends(get_state)):
    """Get analytics for a specific run."""
    return await state.get_run_analytics(run_id)


@router.get("/api/analytics/trends")
async def trends(state: StateManager = Depends(get_state)):
    """Get weekly trend data across all runs."""
    return await state.get_weekly_trends()
