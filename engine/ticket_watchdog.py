"""Event-driven ticket watchdog — auto-retry, stale detection, working hours.

No polling. Timers are created per-ticket only when needed.
"""

import asyncio
import sys
from datetime import datetime, time as dtime
from typing import Callable, Optional

from engine.env_manager import get_env
from engine.state import StateManager
from models.ticket import TicketState


class TicketWatchdog:
    """Manages per-ticket timers for retry and staleness detection."""

    def __init__(self, state: StateManager, broadcaster):
        self.state = state
        self.broadcaster = broadcaster
        self._timers: dict[str, asyncio.TimerHandle] = {}  # ticket_id -> handle
        self._retry_counts: dict[str, int] = {}  # ticket_id -> retries so far
        self._requeue_callback: Optional[Callable] = None  # set by orchestrator
        self._pause_callback: Optional[Callable] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_callbacks(self, requeue_cb: Callable, pause_cb: Optional[Callable] = None):
        """Set callbacks for retry/pause actions."""
        self._requeue_callback = requeue_cb
        self._pause_callback = pause_cb

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.get_event_loop()
        return self._loop

    # --- Public API (called by orchestrator on state transitions) ---

    def on_ticket_active(self, ticket_id: str):
        """Called when a ticket enters planning/developing state."""
        self._cancel_timer(ticket_id)
        timeout = self._get_worker_timeout()
        if timeout > 0:
            self._set_timer(ticket_id, timeout, self._on_stale, ticket_id)

    def on_ticket_completed(self, ticket_id: str):
        """Called when a ticket reaches done/review state."""
        self._cancel_timer(ticket_id)
        self._retry_counts.pop(ticket_id, None)

    def on_ticket_failed(self, ticket_id: str):
        """Called when a ticket fails."""
        self._cancel_timer(ticket_id)

        if not self._is_auto_retry_enabled():
            return

        count = self._retry_counts.get(ticket_id, 0)
        max_retries = self._get_max_retries()
        if count >= max_retries:
            print(f"[watchdog] {ticket_id}: max retries ({max_retries}) reached", file=sys.stderr)
            self._retry_counts.pop(ticket_id, None)
            return

        delay = self._get_retry_delay()
        self._retry_counts[ticket_id] = count + 1
        print(f"[watchdog] {ticket_id}: scheduling retry {count + 1}/{max_retries} in {delay}s",
              file=sys.stderr)
        self._set_timer(ticket_id, delay, self._on_retry, ticket_id)

    def on_ticket_manual_move(self, ticket_id: str):
        """Called when user manually moves a ticket — cancel any pending timers."""
        self._cancel_timer(ticket_id)
        self._retry_counts.pop(ticket_id, None)

    # --- Working hours ---

    def is_within_working_hours(self) -> bool:
        """Check if current time is within configured working hours."""
        if not self._is_working_hours_enabled():
            return True

        now = datetime.now()
        day_abbr = now.strftime("%a").lower()[:3]
        allowed_days = [d.strip().lower()[:3] for d in get_env("WORKING_HOURS_DAYS", "mon,tue,wed,thu,fri").split(",")]

        if day_abbr not in allowed_days:
            return False

        try:
            start = dtime.fromisoformat(get_env("WORKING_HOURS_START", "09:00"))
            end = dtime.fromisoformat(get_env("WORKING_HOURS_END", "18:00"))
        except ValueError:
            return True

        current_time = now.time()
        if start <= end:
            return start <= current_time <= end
        else:
            # Overnight window (e.g., 22:00-06:00)
            return current_time >= start or current_time <= end

    # --- Timer callbacks ---

    async def _on_stale(self, ticket_id: str):
        """Handle a ticket that exceeded worker timeout."""
        ticket = await self.state.get_ticket(ticket_id)
        if not ticket:
            return
        if ticket.state not in (TicketState.PLANNING, TicketState.DEVELOPING):
            return  # Already moved, nothing to do

        error = f"Worker timeout exceeded ({self._get_worker_timeout() // 60}min)"
        print(f"[watchdog] {ticket_id}: stale — {error}", file=sys.stderr)
        await self.state.update_ticket(ticket_id, state=TicketState.FAILED, error=error)
        await self.broadcaster.broadcast_update(ticket.run_id)

        # Auto-retry will be triggered by on_ticket_failed if enabled
        self.on_ticket_failed(ticket_id)

    async def _on_retry(self, ticket_id: str):
        """Retry a failed ticket by re-queuing it."""
        if not self.is_within_working_hours():
            # Defer retry to start of next working window
            print(f"[watchdog] {ticket_id}: outside working hours, deferring retry", file=sys.stderr)
            self._set_timer(ticket_id, 300, self._on_retry, ticket_id)  # check again in 5min
            return

        ticket = await self.state.get_ticket(ticket_id)
        if not ticket:
            return
        if ticket.state != TicketState.FAILED:
            return  # Already moved

        count = self._retry_counts.get(ticket_id, 0)
        print(f"[watchdog] {ticket_id}: retrying (attempt {count})", file=sys.stderr)

        await self.state.update_ticket(ticket_id, state=TicketState.QUEUED, error=None)
        await self.broadcaster.broadcast_update(ticket.run_id)

        if self._requeue_callback:
            await self._requeue_callback(ticket.run_id)

    # --- Internal helpers ---

    def _set_timer(self, ticket_id: str, delay_seconds: float, coro_func, *args):
        """Set a one-shot timer for a ticket."""
        loop = self._get_loop()

        async def _run():
            await coro_func(*args)

        handle = loop.call_later(delay_seconds, lambda: asyncio.ensure_future(_run()))
        self._timers[ticket_id] = handle

    def _cancel_timer(self, ticket_id: str):
        """Cancel any pending timer for a ticket."""
        handle = self._timers.pop(ticket_id, None)
        if handle:
            handle.cancel()

    def _is_auto_retry_enabled(self) -> bool:
        return get_env("AUTO_RETRY_ENABLED", "false").lower() == "true"

    def _get_retry_delay(self) -> int:
        """Get retry delay in seconds."""
        try:
            return int(get_env("AUTO_RETRY_DELAY_MINUTES", "15")) * 60
        except ValueError:
            return 900

    def _get_max_retries(self) -> int:
        try:
            return int(get_env("AUTO_RETRY_MAX", "3"))
        except ValueError:
            return 3

    def _get_worker_timeout(self) -> int:
        """Get worker timeout in seconds."""
        try:
            return int(get_env("WORKER_TIMEOUT_MINUTES", "30")) * 60
        except ValueError:
            return 1800

    def _is_working_hours_enabled(self) -> bool:
        return get_env("WORKING_HOURS_ENABLED", "false").lower() == "true"

    def cancel_all(self):
        """Cancel all pending timers."""
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
        self._retry_counts.clear()

    def get_status(self) -> dict:
        """Get current watchdog status for API/UI."""
        return {
            "active_timers": len(self._timers),
            "pending_retries": {tid: count for tid, count in self._retry_counts.items()},
            "auto_retry_enabled": self._is_auto_retry_enabled(),
            "working_hours_enabled": self._is_working_hours_enabled(),
            "within_working_hours": self.is_within_working_hours(),
        }
