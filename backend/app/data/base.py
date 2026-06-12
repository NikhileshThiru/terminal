"""BaseProvider — composes cache + circuit-breaker + rate-limit + retry (ADR-0002).

Concrete providers subclass this and call `self._fetch_cached(key, ttl, fn)`
from their public methods. Any raw exception from `fn` is translated to
`ProviderUnavailable` with the appropriate reason. The agent funnel sees
only the typed failure path; it never has to handle httpx errors directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.core.logging import get_logger
from app.data.cache import Cache, InMemoryCache
from app.data.circuit_breaker import CircuitBreaker, CircuitOpen
from app.data.rate_limit import RateLimiter
from app.data.types import ProviderUnavailable, ProviderUnavailableReason

T = TypeVar("T")

_log = get_logger(__name__)


class BaseProvider:
    """Composition of the resilience patterns from ADR-0002.

    Order: cache lookup → circuit-breaker check → rate-limit acquire →
    fetch → retry on transient errors → cache store.
    """

    def __init__(
        self,
        name: str,
        rate_limiter: RateLimiter,
        *,
        circuit_breaker: CircuitBreaker | None = None,
        cache: Cache | None = None,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
    ) -> None:
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")
        self.name = name
        self._rate_limiter = rate_limiter
        self._circuit_breaker = (
            circuit_breaker if circuit_breaker is not None else CircuitBreaker(name=name)
        )
        # Note: `cache or InMemoryCache()` would create a fresh cache because an
        # empty InMemoryCache is falsy (__len__ returns 0). Use `is None` instead.
        self._cache = cache if cache is not None else InMemoryCache()
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff_seconds

    async def _fetch_cached(
        self,
        key: str,
        ttl_seconds: float,
        fetch: Callable[[], Awaitable[T]],
    ) -> T:
        """Read-through cache + circuit-breaker + rate-limit + retry."""
        cached = await self._cache.get(key)
        if cached is not None:
            return cached  # type: ignore[no-any-return]

        async def run() -> T:
            await self._rate_limiter.acquire()
            return await fetch()

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                value = await self._circuit_breaker.call(run)
            except CircuitOpen as e:
                raise ProviderUnavailable(
                    reason=ProviderUnavailableReason.CIRCUIT_OPEN,
                    message=str(e),
                    provider=self.name,
                    retryable=True,
                ) from e
            except ProviderUnavailable:
                # Already typed by the fetch fn (e.g. AUTH_MISSING, DATA_MISSING).
                # Don't wrap, don't retry — the fetcher knows its own failure mode.
                raise
            except Exception as e:
                last_exc = e
                _log.warning(
                    "provider_fetch_failed",
                    provider=self.name,
                    attempt=attempt + 1,
                    max_attempts=self._max_retries + 1,
                    error=type(e).__name__,
                    message=str(e),
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_backoff * (2**attempt))
                    continue
                break
            else:
                await self._cache.set(key, value, ttl_seconds)
                return value

        raise ProviderUnavailable(
            reason=ProviderUnavailableReason.UPSTREAM_ERROR,
            message=(
                f"{type(last_exc).__name__}: {last_exc}"
                if last_exc is not None
                else "exhausted retries"
            ),
            provider=self.name,
        ) from last_exc
