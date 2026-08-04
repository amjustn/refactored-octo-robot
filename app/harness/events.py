"""Harness event protocol v2 — the only outward-facing contract.

WebSocket senders, the repository and log sinks are all event subscribers.

Compatibility rules (hard constraints — the existing frontend app.js
consumes these):
- Legacy type names ``started / progress / chunk / complete / error`` are
  kept verbatim; fields are only ever ADDED, never renamed or removed.
- ``progress`` is NOT renamed to ``agent_progress`` (design doc §6 naming
  is overridden by the deployed frontend contract).
- Cancellation still emits the legacy
  ``{"type": "error", "cancelled": true, ...}`` and MAY additionally emit
  the new ``{"type": "cancelled"}``.
- New types ``context_ready / tool_call / guard_warning`` are pure
  additions; old frontends must ignore unknown types.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger("ai_berkshire.harness.events")


class EventType(StrEnum):
    STARTED = "started"
    CONTEXT_READY = "context_ready"      # new: context assembly summary
    PROGRESS = "progress"                # legacy name kept (NOT agent_progress)
    TOOL_CALL = "tool_call"              # new: tool call observability
    CHUNK = "chunk"
    GUARD_WARNING = "guard_warning"      # new: output-gate warning
    COMPLETE = "complete"
    ERROR = "error"
    CANCELLED = "cancelled"              # new: explicit cancel type (additive)


@dataclass
class Event:
    type: EventType
    task_id: str
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_ws_dict(self) -> dict:
        """Serialize for WebSocket delivery: {"type", "task_id", **payload}."""
        return {"type": str(self.type), "task_id": self.task_id, **self.payload}


class EventBus:
    """Per-task pub/sub over asyncio queues.

    Each subscriber gets its own queue so a slow/dead subscriber never
    blocks the runner. Queues are bounded; if full, the oldest event is
    dropped (streaming chunk floods must not stall execution).
    """

    _QUEUE_MAX = 2000

    def __init__(self):
        self._subs: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, task_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._QUEUE_MAX)
        self._subs.setdefault(task_id, set()).add(q)
        return q

    def unsubscribe(self, task_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(task_id)
        if subs:
            subs.discard(q)
            if not subs:
                self._subs.pop(task_id, None)

    def publish(self, event: Event) -> None:
        """Fan an event out to all subscribers of the task (non-blocking)."""
        logger.debug("event %s task=%s", event.type, event.task_id)
        for q in list(self._subs.get(event.task_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()  # drop oldest, keep the stream flowing
                    q.put_nowait(event)
                except Exception:
                    pass

    def emit(self, task_id: str, type_: EventType, **payload) -> Event:
        ev = Event(type=type_, task_id=task_id, payload=payload)
        self.publish(ev)
        return ev


_bus = EventBus()


def get_bus() -> EventBus:
    return _bus
