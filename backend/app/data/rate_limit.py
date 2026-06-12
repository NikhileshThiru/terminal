"""Token-bucket rate limiter for outbound API calls (ADR-0002).

Each provider owns one RateLimiter sized to the source's published quota
(EDGAR 10/s, Finnhub 60/min, Alpaca 200/min). `acquire()` blocks until a
token is available so we never exceed the upstream limit.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class RateLimiter:
    """Async token-bucket limiter.

    `rate_per_sec` tokens replenish per second; bucket holds up to `burst`.
    `acquire()` blocks until one token is available; `try_acquire()` is
    non-blocking.
    """

    def __init__(
        self,
        rate_per_sec: float,
        burst: int | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate_per_sec <= 0:
            raise ValueError(f"rate_per_sec must be positive, got {rate_per_sec}")
        self._rate = rate_per_sec
        self._capacity = burst if burst is not None else max(1, int(rate_per_sec))
        if self._capacity <= 0:
            raise ValueError(f"burst must be positive, got {self._capacity}")
        self._tokens = float(self._capacity)
        self._clock = clock
        self._updated_at = self._clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until one token is available, then consume it."""
        while True:
            wait = await self._try_consume()
            if wait <= 0:
                return
            await asyncio.sleep(wait)

    async def try_acquire(self) -> bool:
        """Non-blocking: consume a token if available, else return False."""
        wait = await self._try_consume()
        return wait <= 0

    async def _try_consume(self) -> float:
        """Returns 0 if a token was consumed, else seconds to wait."""
        async with self._lock:
            now = self._clock()
            elapsed = now - self._updated_at
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._updated_at = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            return (1.0 - self._tokens) / self._rate
