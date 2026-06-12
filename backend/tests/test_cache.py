"""TTL cache behavior, with an injected clock for deterministic expiry tests."""

from __future__ import annotations

import pytest

from app.data.cache import InMemoryCache


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.asyncio
async def test_set_then_get_returns_value() -> None:
    c = InMemoryCache()
    await c.set("k", "v", ttl_seconds=60)
    assert await c.get("k") == "v"


@pytest.mark.asyncio
async def test_get_missing_returns_none() -> None:
    c = InMemoryCache()
    assert await c.get("missing") is None


@pytest.mark.asyncio
async def test_ttl_expires() -> None:
    clock = FakeClock()
    c = InMemoryCache(clock=clock)
    await c.set("k", "v", ttl_seconds=5)
    assert await c.get("k") == "v"
    clock.advance(6)
    assert await c.get("k") is None


@pytest.mark.asyncio
async def test_overwrites_existing_key() -> None:
    c = InMemoryCache()
    await c.set("k", "v1", ttl_seconds=60)
    await c.set("k", "v2", ttl_seconds=60)
    assert await c.get("k") == "v2"


@pytest.mark.asyncio
async def test_delete_removes_key() -> None:
    c = InMemoryCache()
    await c.set("k", "v", ttl_seconds=60)
    await c.delete("k")
    assert await c.get("k") is None


@pytest.mark.asyncio
async def test_delete_missing_is_noop() -> None:
    c = InMemoryCache()
    await c.delete("missing")  # should not raise


@pytest.mark.asyncio
async def test_clear_removes_all() -> None:
    c = InMemoryCache()
    await c.set("a", 1, ttl_seconds=60)
    await c.set("b", 2, ttl_seconds=60)
    await c.clear()
    assert len(c) == 0


def test_set_rejects_nonpositive_ttl() -> None:
    c = InMemoryCache()
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(c.set("k", "v", ttl_seconds=0))
    with pytest.raises(ValueError):
        asyncio.run(c.set("k", "v", ttl_seconds=-1))
