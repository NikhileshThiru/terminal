"""Global pub-sub for agent events.

The manual copilot and the autonomous reactive runner both call
`Copilot.generate(..., event_sink=...)`. Each call's sink fan-outs to the
per-request consumer (the SSE response for that one user). The
broadcaster is a second, global fan-out: any client that subscribes to
`/agent/events/stream` receives every agent event from every run, no
matter who initiated it.

That's what makes the Agent Reasoning pane on the Dashboard come alive
when autonomous mode is producing theses — the same wire format,
multiplexed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import contextmanager

from app.agent.copilot import CopilotEvent
from app.core.logging import get_logger

_log = get_logger(__name__)


class AgentEventBroadcaster:
    """Singleton fan-out. Each subscriber gets its own bounded async
    queue; full queues drop the oldest events so a slow consumer can
    never wedge the runner."""

    _MAX_QUEUE = 128

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[CopilotEvent]] = set()
        self._lock = asyncio.Lock()

    @contextmanager
    def subscribe(self):  # type: ignore[no-untyped-def]
        """Synchronous context manager for tests / non-async wiring.
        Prefer `subscribe_async` for production callers."""
        q: asyncio.Queue[CopilotEvent] = asyncio.Queue(maxsize=self._MAX_QUEUE)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)

    async def publish(self, event: CopilotEvent) -> None:
        # Snapshot to avoid mutating during iteration; we never await while
        # holding the snapshot.
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop oldest to make room. A slow client must not stop the
                # rest of the system; quality of the stream degrades for
                # that one subscriber only.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    _log.warning("broadcaster_queue_drop_failed")

    async def stream(self) -> AsyncIterator[CopilotEvent]:
        """Subscribe + iterate. The consumer should run inside a try/finally
        so cancellation removes the queue from the subscriber set."""
        q: asyncio.Queue[CopilotEvent] = asyncio.Queue(maxsize=self._MAX_QUEUE)
        self._subscribers.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers.discard(q)


_broadcaster = AgentEventBroadcaster()


def get_agent_broadcaster() -> AgentEventBroadcaster:
    return _broadcaster


async def broadcast_event(event: CopilotEvent) -> None:
    """Convenience helper: publish one event to the singleton."""
    await _broadcaster.publish(event)
