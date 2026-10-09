"""Circuit breaker state transitions with a controlled clock."""

from __future__ import annotations

import asyncio

import pytest

from app.data.circuit_breaker import CircuitBreaker, CircuitOpen, CircuitState


async def _fail() -> None:
    raise RuntimeError("upstream down")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.asyncio
async def test_closed_breaker_passes_calls() -> None:
    cb = CircuitBreaker(name="t", failure_threshold=3)

    async def ok() -> int:
        return 42

    assert await cb.call(ok) == 42
    assert cb.state == CircuitState.CLOSED
    assert cb.failures == 0


@pytest.mark.asyncio
async def test_opens_after_threshold_failures() -> None:
    cb = CircuitBreaker(name="t", failure_threshold=3)

    async def fail() -> None:
        raise RuntimeError("upstream broken")

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cb.call(fail)

    assert cb.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_open_breaker_raises_circuit_open() -> None:
    cb = CircuitBreaker(name="t", failure_threshold=1, recovery_timeout=60)

    async def fail() -> None:
        raise RuntimeError("broken")

    with pytest.raises(RuntimeError):
        await cb.call(fail)

    async def ok() -> int:
        return 1

    with pytest.raises(CircuitOpen) as exc:
        await cb.call(ok)
    assert exc.value.name == "t"
    assert exc.value.retry_after > 0


@pytest.mark.asyncio
async def test_half_open_then_closed_on_success() -> None:
    clock = FakeClock()
    cb = CircuitBreaker(name="t", failure_threshold=1, recovery_timeout=10, clock=clock)

    async def fail() -> None:
        raise RuntimeError("broken")

    with pytest.raises(RuntimeError):
        await cb.call(fail)
    assert cb.state == CircuitState.OPEN

    # Recovery timeout passes.
    clock.advance(11)

    async def ok() -> int:
        return 1

    assert await cb.call(ok) == 1
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_half_open_failure_reopens() -> None:
    clock = FakeClock()
    cb = CircuitBreaker(name="t", failure_threshold=1, recovery_timeout=10, clock=clock)

    async def fail() -> None:
        raise RuntimeError("broken")

    with pytest.raises(RuntimeError):
        await cb.call(fail)

    clock.advance(11)

    # Now half-open. One failure should immediately reopen.
    with pytest.raises(RuntimeError):
        await cb.call(fail)
    assert cb.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_success_resets_failure_counter() -> None:
    cb = CircuitBreaker(name="t", failure_threshold=3)

    async def fail() -> None:
        raise RuntimeError("flaky")

    async def ok() -> int:
        return 1

    with pytest.raises(RuntimeError):
        await cb.call(fail)
    with pytest.raises(RuntimeError):
        await cb.call(fail)
    # 2 failures, below threshold.
    assert cb.state == CircuitState.CLOSED
    assert cb.failures == 2

    # Successful call resets.
    assert await cb.call(ok) == 1
    assert cb.failures == 0


def test_rejects_invalid_threshold() -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(name="t", failure_threshold=0)


def test_rejects_invalid_recovery_timeout() -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(name="t", failure_threshold=1, recovery_timeout=0)


@pytest.mark.asyncio
async def test_half_open_admits_exactly_one_concurrent_probe() -> None:
    """Regression: HALF_OPEN used to let every concurrent caller through."""
    clock = FakeClock()
    cb = CircuitBreaker(name="t", failure_threshold=1, recovery_timeout=10, clock=clock)
    with pytest.raises(RuntimeError):
        await cb.call(_fail)
    clock.advance(11)

    release = asyncio.Event()
    probe_calls = 0

    async def slow_probe() -> str:
        nonlocal probe_calls
        probe_calls += 1
        await release.wait()
        return "ok"

    probe = asyncio.create_task(cb.call(slow_probe))
    await asyncio.sleep(0)  # let the probe take the slot
    with pytest.raises(CircuitOpen):
        await cb.call(slow_probe)  # concurrent caller is turned away
    release.set()
    assert await probe == "ok"
    assert probe_calls == 1
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_cancelled_probe_releases_slot() -> None:
    clock = FakeClock()
    cb = CircuitBreaker(name="t", failure_threshold=1, recovery_timeout=10, clock=clock)
    with pytest.raises(RuntimeError):
        await cb.call(_fail)
    clock.advance(11)

    async def hang() -> str:
        await asyncio.Event().wait()
        return "never"

    probe = asyncio.create_task(cb.call(hang))
    await asyncio.sleep(0)
    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe

    async def ok() -> str:
        return "ok"

    assert await cb.call(ok) == "ok"  # next caller can probe; breaker not wedged
    assert cb.state == CircuitState.CLOSED
