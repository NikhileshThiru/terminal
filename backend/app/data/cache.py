"""TTL cache abstraction (ADR-0002).

Phase 2: in-memory implementation. The `Cache` Protocol leaves room for a
Redis-backed implementation in Phase 6+ when multiple processes need to
share state (DESIGN.md §3 — Redis is the cache/hot store).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Cache(Protocol):
    """A read-through TTL cache. Async-safe."""

    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any, ttl_seconds: float) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def clear(self) -> None: ...


@dataclass
class _Entry:
    value: Any
    expires_at: float


class InMemoryCache:
    """Single-process TTL cache.

    Constructor accepts a `clock` callable for deterministic testing. In
    production it defaults to `time.monotonic`.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._data: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        self._clock = clock

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self._clock():
                del self._data[key]
                return None
            return entry.value

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
        async with self._lock:
            self._data[key] = _Entry(value=value, expires_at=self._clock() + ttl_seconds)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
