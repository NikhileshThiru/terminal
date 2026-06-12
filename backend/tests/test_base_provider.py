"""BaseProvider — composition of cache + circuit-breaker + rate-limit + retry."""

from __future__ import annotations

import pytest

from app.data.base import BaseProvider
from app.data.cache import InMemoryCache
from app.data.circuit_breaker import CircuitBreaker
from app.data.rate_limit import RateLimiter
from app.data.types import ProviderUnavailable, ProviderUnavailableReason


def make_provider(
    *,
    max_retries: int = 2,
    failure_threshold: int = 5,
    cache: InMemoryCache | None = None,
) -> BaseProvider:
    return BaseProvider(
        name="test",
        rate_limiter=RateLimiter(rate_per_sec=1000, burst=100),
        circuit_breaker=CircuitBreaker(name="test", failure_threshold=failure_threshold),
        cache=cache,
        max_retries=max_retries,
        retry_backoff_seconds=0.001,  # keep tests fast
    )


@pytest.mark.asyncio
async def test_fetch_returns_value_and_caches_it() -> None:
    p = make_provider()
    calls = 0

    async def fetch() -> str:
        nonlocal calls
        calls += 1
        return "value"

    assert await p._fetch_cached("k", 60, fetch) == "value"
    assert await p._fetch_cached("k", 60, fetch) == "value"
    assert calls == 1


@pytest.mark.asyncio
async def test_transient_error_then_success_via_retry() -> None:
    p = make_provider(max_retries=2)
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise RuntimeError("transient")
        return "ok"

    assert await p._fetch_cached("k", 60, flaky) == "ok"
    assert attempts == 2


@pytest.mark.asyncio
async def test_raw_exception_becomes_typed_unavailable() -> None:
    p = make_provider(max_retries=0)

    async def fetch() -> str:
        raise ValueError("boom")

    with pytest.raises(ProviderUnavailable) as exc:
        await p._fetch_cached("k", 60, fetch)
    assert exc.value.reason == ProviderUnavailableReason.UPSTREAM_ERROR
    assert exc.value.provider == "test"


@pytest.mark.asyncio
async def test_typed_unavailable_passes_through_unwrapped() -> None:
    """Fetcher-raised ProviderUnavailable should propagate as-is (no double-wrap)."""
    p = make_provider(max_retries=0)

    async def fetch() -> str:
        raise ProviderUnavailable(
            reason=ProviderUnavailableReason.AUTH_MISSING,
            message="no key",
            provider="upstream",
        )

    with pytest.raises(ProviderUnavailable) as exc:
        await p._fetch_cached("k", 60, fetch)
    assert exc.value.reason == ProviderUnavailableReason.AUTH_MISSING
    assert exc.value.provider == "upstream"


@pytest.mark.asyncio
async def test_circuit_open_translates_to_unavailable() -> None:
    p = make_provider(max_retries=0, failure_threshold=2)

    async def fetch() -> str:
        raise RuntimeError("broken upstream")

    # Two raw failures should trip the breaker (threshold=2).
    for i in range(2):
        with pytest.raises(ProviderUnavailable) as exc:
            await p._fetch_cached(f"k{i}", 60, fetch)
        assert exc.value.reason == ProviderUnavailableReason.UPSTREAM_ERROR

    # Third call sees the open breaker.
    with pytest.raises(ProviderUnavailable) as exc:
        await p._fetch_cached("k3", 60, fetch)
    assert exc.value.reason == ProviderUnavailableReason.CIRCUIT_OPEN


@pytest.mark.asyncio
async def test_cache_persists_across_provider_calls() -> None:
    """The cache hits without going through circuit breaker or rate limiter."""
    shared_cache = InMemoryCache()
    p1 = make_provider(cache=shared_cache)
    p2 = make_provider(cache=shared_cache)

    async def fetch() -> str:
        return "shared"

    await p1._fetch_cached("shared-key", 60, fetch)

    calls = 0

    async def fetch2() -> str:
        nonlocal calls
        calls += 1
        return "different"

    # p2 with the same cache should see p1's value.
    assert await p2._fetch_cached("shared-key", 60, fetch2) == "shared"
    assert calls == 0


def test_rejects_negative_retries() -> None:
    with pytest.raises(ValueError):
        BaseProvider(
            name="t",
            rate_limiter=RateLimiter(rate_per_sec=1),
            max_retries=-1,
        )
