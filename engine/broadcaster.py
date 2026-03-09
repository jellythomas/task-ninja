"""SSE broadcaster for real-time UI updates."""

import asyncio
import json
from enum import Enum
from typing import Any


class _EnumEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Enum):
            return obj.value
        return super().default(obj)


class Broadcaster:
    """Manages SSE connections and broadcasts events to all listeners."""

    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue]] = {}  # run_id -> queues

    def subscribe(self, run_id: str) -> asyncio.Queue:
        """Subscribe to events for a run. Returns a queue to read from."""
        queue: asyncio.Queue = asyncio.Queue()
        if run_id not in self._subscribers:
            self._subscribers[run_id] = []
        self._subscribers[run_id].append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        """Remove a subscriber."""
        if run_id in self._subscribers:
            self._subscribers[run_id] = [q for q in self._subscribers[run_id] if q is not queue]

    async def broadcast(self, run_id: str, event: str, data: Any) -> None:
        """Send an event to all subscribers of a run."""
        message = json.dumps({"event": event, "data": data}, cls=_EnumEncoder)
        for queue in self._subscribers.get(run_id, []):
            await queue.put(message)

    async def broadcast_ticket_update(self, run_id: str, ticket_id: str, state: str = None, **extra) -> None:
        """Broadcast a ticket update (state change, PR URL, etc)."""
        data = {"ticket_id": ticket_id, **extra}
        if state is not None:
            data["state"] = state
        await self.broadcast(run_id, "ticket_update", data)

    async def broadcast_log(self, run_id: str, ticket_id: str, line: str) -> None:
        """Broadcast a log line from a worker."""
        await self.broadcast(run_id, "log", {
            "ticket_id": ticket_id,
            "line": line,
        })

    async def broadcast_run_status(self, run_id: str, status: str) -> None:
        """Broadcast a run status change."""
        await self.broadcast(run_id, "run_status", {"status": status})
