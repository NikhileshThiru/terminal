"""RedisStreamEventBus — at-least-once delivery between the ingestion service
and the agent worker, against fakeredis (real Streams semantics, no server)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import fakeredis
import pytest

from app.discovery.bus import InMemoryEventBus, RedisStreamEventBus
from app.discovery.ingest import STATUS_KEY, read_status
from app.discovery.reactive import ReactiveRunner
from app.discovery.triage import TriageDecision
from app.discovery.types import DiscoveryEvent


def _ev(i: int) -> DiscoveryEvent:
    return DiscoveryEvent(
        id=f"news-{i}",
        source="alpaca-news",
        kind="news",
        symbols=["AAPL"],
        headline=f"headline {i}",
        published_at=datetime.now(UTC),
    )


@pytest.fixture
def redis() -> fakeredis.FakeAsyncRedis:
    return fakeredis.FakeAsyncRedis(decode_responses=True)


def _bus(redis: fakeredis.FakeAsyncRedis, **kw: object) -> RedisStreamEventBus:
    return RedisStreamEventBus(redis, stream="t:events", block_ms=50, **kw)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_publish_consume_ack_roundtrip(redis: fakeredis.FakeAsyncRedis) -> None:
    producer, consumer = _bus(redis), _bus(redis)  # two processes, one stream
    await producer.publish(_ev(1))
    await producer.publish(_ev(2))

    first = await asyncio.wait_for(consumer.consume(), timeout=2)
    assert first.id == "news-1"
    assert first.ingested_at is not None  # stamped at publish
    await consumer.ack(first)
    second = await asyncio.wait_for(consumer.consume(), timeout=2)
    assert second.id == "news-2"
    await consumer.ack(second)

    pending = await redis.xpending("t:events", "agent")
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_unacked_event_is_redelivered_after_restart(
    redis: fakeredis.FakeAsyncRedis,
) -> None:
    """The worker dies mid-event (no ACK). A restarted worker with the same
    consumer name gets that event again before anything new."""
    producer = _bus(redis)
    await producer.publish(_ev(1))
    await producer.publish(_ev(2))

    crashed = _bus(redis)
    got = await asyncio.wait_for(crashed.consume(), timeout=2)
    assert got.id == "news-1"
    # ... process dies here: no ack.

    restarted = _bus(redis)
    again = await asyncio.wait_for(restarted.consume(), timeout=2)
    assert again.id == "news-1"
    assert restarted.redelivered == 1
    await restarted.ack(again)
    nxt = await asyncio.wait_for(restarted.consume(), timeout=2)
    assert nxt.id == "news-2"


@pytest.mark.asyncio
async def test_events_published_before_first_worker_start_are_not_lost(
    redis: fakeredis.FakeAsyncRedis,
) -> None:
    """Ingestion can start before the worker ever creates its group."""
    await _bus(redis).publish(_ev(1))
    late = _bus(redis)
    assert (await asyncio.wait_for(late.consume(), timeout=2)).id == "news-1"


@pytest.mark.asyncio
async def test_poison_entry_is_acked_away_not_redelivered_forever(
    redis: fakeredis.FakeAsyncRedis,
) -> None:
    bus = _bus(redis)
    await bus.ensure_group()
    await redis.xadd("t:events", {"event": "{not json"})
    await bus.publish(_ev(7))
    got = await asyncio.wait_for(bus.consume(), timeout=2)
    assert got.id == "news-7"
    assert bus.poison_dropped == 1


@pytest.mark.asyncio
async def test_runner_acks_after_processing(redis: fakeredis.FakeAsyncRedis) -> None:
    class _Drop:
        name = "stub"

        async def complete_structured(self, **_: object) -> TriageDecision:
            return TriageDecision(passed=False, reason="routine", confidence=0.6)

    bus = _bus(redis)
    runner = ReactiveRunner(bus=bus, copilot=None, triage_llm=_Drop(), triage_model="m")  # type: ignore[arg-type]
    await bus.publish(_ev(1))
    await runner.start()
    for _ in range(100):
        if runner.stats.events_consumed:
            break
        await asyncio.sleep(0.02)
    await runner.stop()
    assert runner.stats.events_consumed == 1
    assert (await redis.xpending("t:events", "agent"))["pending"] == 0


@pytest.mark.asyncio
async def test_stale_backlog_is_skipped_visibly() -> None:
    """Worker comes back after ingestion kept publishing: old events are
    recorded as stale skips, not triaged (no quota spent)."""

    class _MustNotTriage:
        name = "stub"

        async def complete_structured(self, **_: object) -> TriageDecision:
            raise AssertionError("stale event must not reach triage")

    runner = ReactiveRunner(
        bus=InMemoryEventBus(),
        copilot=None,  # type: ignore[arg-type]
        triage_llm=_MustNotTriage(),  # type: ignore[arg-type]
        triage_model="m",
        max_event_age_seconds=900,
    )
    old = _ev(1).model_copy(update={"ingested_at": datetime.now(UTC) - timedelta(hours=2)})
    assert await runner._process_one(old) is None
    assert runner.stats.events_stale == 1
    assert str(runner.stats.last_triage_decisions[-1]["reason"]).startswith("skipped: stale")


@pytest.mark.asyncio
async def test_ingestion_heartbeat_roundtrip(redis: fakeredis.FakeAsyncRedis) -> None:
    assert await read_status(redis) is None  # service down -> no key
    await redis.set(STATUS_KEY, json.dumps({"polls_completed": 3, "news_state": "connected"}))
    status = await read_status(redis)
    assert status == {"polls_completed": 3, "news_state": "connected"}
