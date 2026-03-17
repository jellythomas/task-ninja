"""Push notification manager — stores subscriptions, sends via pywebpush or SSE."""

from __future__ import annotations

import json
import logging

from engine.env_manager import get_env
from engine.state import StateManager

logger = logging.getLogger(__name__)


class Notifier:
    """Manages browser push notification subscriptions and sending."""

    def __init__(self, state: StateManager):
        self.state = state
        # In-memory list of SSE-based notification listeners
        self._listeners: list = []

    def is_enabled(self) -> bool:
        return get_env("NOTIFICATIONS_ENABLED", "false").lower() == "true"

    async def store_subscription(self, subscription: dict) -> None:
        """Store a Web Push subscription in the DB."""
        endpoint = subscription.get("endpoint", "")
        if not endpoint:
            return
        await self.state.set_setting(
            f"push_sub_{hash(endpoint) & 0xFFFFFFFF}",
            json.dumps(subscription),
        )

    async def remove_subscription(self, endpoint: str) -> None:
        """Remove a push subscription."""
        await self.state.delete_setting(f"push_sub_{hash(endpoint) & 0xFFFFFFFF}")

    async def notify(self, title: str, body: str, tag: str = "", url: str = "") -> None:
        """Send notification to all registered channels."""
        if not self.is_enabled():
            return

        payload = {"title": title, "body": body, "tag": tag, "url": url}

        # Try Web Push (requires pywebpush + VAPID keys)
        await self._send_web_push(payload)

        # Also broadcast via SSE for tab-open notifications
        for listener in list(self._listeners):
            try:
                await listener(payload)
            except (RuntimeError, OSError):
                self._listeners.remove(listener)

    async def notify_ticket_completed(self, jira_key: str, ticket_id: str) -> None:
        await self.notify(
            title=f"{jira_key} completed",
            body="Ticket has been completed and is ready for review.",
            tag=f"ticket-{ticket_id}",
        )

    async def notify_ticket_failed(self, jira_key: str, ticket_id: str, error: str = "") -> None:
        short_error = (error[:80] + "...") if len(error) > 80 else error
        await self.notify(
            title=f"{jira_key} failed",
            body=short_error or "Ticket execution failed.",
            tag=f"ticket-{ticket_id}",
        )

    async def notify_run_completed(self, run_name: str) -> None:
        await self.notify(
            title="Run completed",
            body=f'All tickets in "{run_name}" have finished.',
            tag="run-completed",
        )

    async def _send_web_push(self, payload: dict) -> None:
        """Send Web Push notifications using pywebpush."""
        vapid_private = get_env("VAPID_PRIVATE_KEY")
        vapid_email = get_env("VAPID_EMAIL")
        if not vapid_private or not vapid_email:
            return

        try:
            from pywebpush import webpush
        except ImportError:
            return  # pywebpush not installed, skip silently

        # Load all stored subscriptions
        all_settings = await self.state.get_all_settings()
        subs = [json.loads(v) for k, v in all_settings.items() if k.startswith("push_sub_")]

        vapid_claims = {"sub": f"mailto:{vapid_email}"}
        data = json.dumps(payload)

        for sub in subs:
            try:
                webpush(
                    subscription_info=sub,
                    data=data,
                    vapid_private_key=vapid_private,
                    vapid_claims=vapid_claims,
                )
            except Exception as e:
                logger.warning("Web push failed: %s", e)

    def get_vapid_public_key(self) -> str | None:
        """Get VAPID public key for client-side subscription."""
        return get_env("VAPID_PUBLIC_KEY") or None
