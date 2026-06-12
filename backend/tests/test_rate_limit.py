"""Token-bucket rate limiter behavior."""

from __future__ import annotations

import pytest

from app.data.rate_limit import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.asyncio
async def test_burst_capacity_immediately_available() -> None:
    clock = FakeClock()
    rl = RateLimiter(rate_per_sec=10, burst=3, clock=clock)
    for _ in range(3):
        assert await rl.try_acquire() is True


@pytest.mark.asyncio
async def test_try_acquire_returns_false_after_burst_exhausted() -> None:
    clock = FakeClock()
    rl = RateLimiter(rate_per_sec=10, burst=2, clock=clock)
    assert await rl.try_acquire() is True
    assert await rl.try_acquire() is True
    assert await rl.try_acquire() is False


@pytest.mark.asyncio
async def test_tokens_refill_over_time() -> None:
    clock = FakeClock()
    rl = RateLimiter(rate_per_sec=10, burst=1, clock=clock)
    assert await rl.try_acquire() is True
    assert await rl.try_acquire() is False
    # 10 tokens/sec means 1 token after 0.1s
    clock.advance(0.1)
    assert await rl.try_acquire() is True


@pytest.mark.asyncio
async def test_tokens_cap_at_burst_size() -> None:
    """Idle time shouldn't accumulate unbounded tokens."""
    clock = FakeClock()
    rl = RateLimiter(rate_per_sec=10, burst=3, clock=clock)
    # Idle for 10 seconds — would yield 100 tokens uncapped, but bucket caps at 3
    clock.advance(10)
    for _ in range(3):
        assert await rl.try_acquire() is True
    assert await rl.try_acquire() is False


def test_rejects_invalid_rate() -> None:
    with pytest.raises(ValueError):
        RateLimiter(rate_per_sec=0)
    with pytest.raises(ValueError):
        RateLimiter(rate_per_sec=-1)


def test_rejects_invalid_burst() -> None:
    with pytest.raises(ValueError):
        RateLimiter(rate_per_sec=10, burst=0)
