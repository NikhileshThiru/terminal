"""Three-state circuit breaker (ADR-0002).

States:
- CLOSED: calls pass through; failures increment a counter.
- OPEN: calls raise CircuitOpen immediately. After `recovery_timeout`
  seconds, transitions to HALF_OPEN.
- HALF_OPEN: one trial call is allowed. Success → CLOSED. Failure → OPEN.

Protects upstream quotas during outages and keeps the agent funnel from
piling up failed retries.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TypeVar

T = TypeVar("T")


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpen(Exception):
    """Raised by CircuitBreaker.call() when the breaker is open."""

    def __init__(self, name: str, retry_after: float) -> None:
        super().__init__(f"Circuit '{name}' is open; retry after {retry_after:.2f}s")
        self.name = name
        self.retry_after = retry_after


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError(f"failure_threshold must be >= 1, got {failure_threshold}")
        if recovery_timeout <= 0:
            raise ValueError(f"recovery_timeout must be positive, got {recovery_timeout}")
        self.name = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None
        self._clock = clock
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def failures(self) -> int:
        return self._failures

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Run fn() under the breaker. Raises CircuitOpen if open."""
        await self._check_state()
        try:
            result = await fn()
        except Exception:
            await self._on_failure()
            raise
        else:
            await self._on_success()
            return result

    async def _check_state(self) -> None:
        async with self._lock:
            if self._state == CircuitState.OPEN:
                assert self._opened_at is not None
                elapsed = self._clock() - self._opened_at
                remaining = self._recovery_timeout - elapsed
                if remaining > 0:
                    raise CircuitOpen(self.name, remaining)
                # Recovery time elapsed — allow one trial call.
                self._state = CircuitState.HALF_OPEN

    async def _on_success(self) -> None:
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._opened_at = None

    async def _on_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            tripped_by_threshold = self._failures >= self._failure_threshold
            half_open_failure = self._state == CircuitState.HALF_OPEN
            if half_open_failure or tripped_by_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()
