"""In-memory event bus for the autonomous discovery pipeline.

Phase 6 MVP uses a single asyncio.Queue (one producer-set, one consumer).
The interface lets us swap to Redis Streams in Phase 6+ without touching
producers or consumers (DESIGN.md §3 upgrade trigger: not now).
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from app.discovery.types import DiscoveryEvent


@runtime_checkable
class EventBus(Protocol):
    async def publish(self, event: DiscoveryEvent) -> None: ...
    async def consume(self) -> DiscoveryEvent: ...
    def qsize(self) -> int: ...


class InMemoryEventBus:
    """Single-process bus backed by asyncio.Queue.

    `maxsize` bounds the buffer so a runaway producer can't OOM the process —
    publishers will await space; the queue applies natural back-pressure.
    """

    def __init__(self, maxsize: int = 1000) -> None:
        self._queue: asyncio.Queue[DiscoveryEvent] = asyncio.Queue(maxsize=maxsize)

    async def publish(self, event: DiscoveryEvent) -> None:
        await self._queue.put(event)

    async def consume(self) -> DiscoveryEvent:
        return await self._queue.get()

    def qsize(self) -> int:
        return self._queue.qsize()
