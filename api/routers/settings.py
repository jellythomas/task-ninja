"""Settings, environment config, watchdog, notification, and Jira status routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import get_jira_client, get_notifier, get_orchestrator, get_state
from engine.env_manager import get_env, get_public_env, update_env
from engine.jira_client import JiraClient
from engine.notifier import Notifier
from engine.orchestrator import Orchestrator
from engine.state import StateManager
from models.ticket import UpdateSettingsRequest

router = APIRouter(tags=["settings"])


# --- Environment Config ---


@router.get("/api/env")
async def get_env_config():
    """Get .env configuration (secrets masked)."""
    return get_public_env()


@router.put("/api/env")
async def update_env_config(req: dict):
    """Update .env configuration."""
    updates = req.get("settings", req)
    if "TASK_NINJA_SECRET" in updates and not updates["TASK_NINJA_SECRET"]:
        del updates["TASK_NINJA_SECRET"]
    update_env(updates)
    return {"status": "updated"}


# --- Settings ---


@router.get("/api/settings")
async def get_settings(state: StateManager = Depends(get_state)):
    all_settings = await state.get_all_settings()
    masked = {}
    for k, v in all_settings.items():
        if "token" in k.lower() or "secret" in k.lower() or "password" in k.lower():
            masked[k] = v[:4] + "****" if len(v) > 4 else "****"
        else:
            masked[k] = v
    return masked


@router.put("/api/settings")
async def update_settings(
    req: UpdateSettingsRequest,
    state: StateManager = Depends(get_state),
):
    await state.set_settings(req.settings)
    return {"status": "updated"}


@router.get("/api/watchdog/status")
async def watchdog_status(orchestrator: Orchestrator = Depends(get_orchestrator)):
    """Get watchdog status (active timers, retries, working hours)."""
    return orchestrator.watchdog.get_status()


# --- Jira Status ---


@router.get("/api/settings/jira-status")
async def jira_status(jira_client: JiraClient = Depends(get_jira_client)):
    """Check if Jira credentials are configured."""
    configured = await jira_client.is_configured()
    return {"configured": configured}


@router.post("/api/settings/test-jira")
async def test_jira_connection():
    """Test Jira API connection with .env credentials."""
    jira_url = get_env("JIRA_BASE_URL")
    jira_email = get_env("JIRA_EMAIL")
    jira_token = get_env("JIRA_API_TOKEN")
    if not all([jira_url, jira_email, jira_token]):
        raise HTTPException(400, "Jira credentials not configured in .env")
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{jira_url.rstrip('/')}/rest/api/3/myself",
                auth=(jira_email, jira_token),
                timeout=10,
            )
            if resp.status_code == 200:
                user = resp.json()
                return {"status": "connected", "user": user.get("displayName", user.get("emailAddress"))}
            raise HTTPException(resp.status_code, f"Jira API returned {resp.status_code}")
    except httpx.RequestError as e:
        raise HTTPException(502, f"Connection failed: {e}") from e


# --- Notifications ---


@router.get("/api/notifications/vapid-key")
async def get_vapid_key(notifier: Notifier = Depends(get_notifier)):
    """Get VAPID public key for Web Push subscription."""
    key = notifier.get_vapid_public_key()
    return {"key": key, "enabled": notifier.is_enabled()}


@router.post("/api/notifications/subscribe")
async def subscribe_push(req: dict, notifier: Notifier = Depends(get_notifier)):
    """Store a Web Push subscription."""
    subscription = req.get("subscription")
    if not subscription:
        raise HTTPException(400, "Missing subscription object")
    await notifier.store_subscription(subscription)
    return {"status": "subscribed"}


@router.delete("/api/notifications/subscribe")
async def unsubscribe_push(req: dict, notifier: Notifier = Depends(get_notifier)):
    """Remove a Web Push subscription."""
    endpoint = req.get("endpoint", "")
    if not endpoint:
        raise HTTPException(400, "Missing endpoint")
    await notifier.remove_subscription(endpoint)
    return {"status": "unsubscribed"}
