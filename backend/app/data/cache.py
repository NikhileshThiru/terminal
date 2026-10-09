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
    """Single-process TTL cache, bounded.

    Expired entries are swept every `sweep_every` writes, and the cache never
    holds more than `max_entries` (oldest-written evicted first). Without
    both, keys that rotate and are never read again — e.g. Alpaca OHLC keys
    embed the end date, so every symbol mints a fresh key each day — pile up
    forever in the 24/7 worker.

    Constructor accepts a `clock` callable for deterministic testing. In
    production it defaults to `time.monotonic`.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 10_000,
        sweep_every: int = 256,
    ) -> None:
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}")
        self._data: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        self._clock = clock
        self._max_entries = max_entries
        self._sweep_every = max(1, sweep_every)
        self._writes = 0

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
            now = self._clock()
            # pop + insert moves the key to the end, so dict order == write order.
            self._data.pop(key, None)
            self._data[key] = _Entry(value=value, expires_at=now + ttl_seconds)
            self._writes += 1
            if self._writes % self._sweep_every == 0 or len(self._data) > self._max_entries:
                self._purge_expired(now)
            while len(self._data) > self._max_entries:
                del self._data[next(iter(self._data))]

    def _purge_expired(self, now: float) -> None:
        for k in [k for k, e in self._data.items() if e.expires_at <= now]:
            del self._data[k]

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
