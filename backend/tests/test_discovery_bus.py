"""Discovery types + event bus tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.discovery.bus import EventBus, InMemoryEventBus
from app.discovery.types import DiscoveryEvent


def _ev(idx: int = 0) -> DiscoveryEvent:
    return DiscoveryEvent(
        id=f"e-{idx}",
        source="edgar",
        kind="filing",
        symbols=["AAPL"],
        headline=f"AAPL filing {idx}",
        published_at=datetime.now(UTC),
    )


def test_event_short_summary() -> None:
    e = _ev(1)
    s = e.short_summary()
    assert "edgar" in s
    assert "AAPL" in s
    assert "filing" in s


def test_bus_satisfies_protocol() -> None:
    assert isinstance(InMemoryEventBus(), EventBus)


@pytest.mark.asyncio
async def test_publish_and_consume_in_order() -> None:
    bus = InMemoryEventBus()
    for i in range(3):
        await bus.publish(_ev(i))
    out = [await bus.consume() for _ in range(3)]
    assert [e.id for e in out] == ["e-0", "e-1", "e-2"]


@pytest.mark.asyncio
async def test_qsize_reflects_buffer() -> None:
    bus = InMemoryEventBus()
    assert bus.qsize() == 0
    await bus.publish(_ev(0))
    await bus.publish(_ev(1))
    assert bus.qsize() == 2
    await bus.consume()
    assert bus.qsize() == 1


@pytest.mark.asyncio
async def test_maxsize_applies_back_pressure() -> None:
    bus = InMemoryEventBus(maxsize=1)
    await bus.publish(_ev(0))
    # Second publish blocks until something consumes.
    publish_task = asyncio.create_task(bus.publish(_ev(1)))
    await asyncio.sleep(0.01)
    assert not publish_task.done()
    await bus.consume()
    await asyncio.wait_for(publish_task, timeout=1.0)
