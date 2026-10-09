"""Event bus for the autonomous discovery pipeline (CLAUDE.md §3, §4).

Two implementations behind one Protocol:

- `InMemoryEventBus` — an asyncio.Queue. Single process; what tests and a
  plain `uvicorn` dev run use (EVENT_BUS=memory, the default).
- `RedisStreamEventBus` — Redis Streams with a consumer group. What the
  docker-compose stack runs (EVENT_BUS=redis): the ingestion service and the
  agent worker are separate processes that meet only at Redis + Postgres,
  exactly as the architecture diagram draws them.

Why Streams rather than a list or pub/sub:
- **Durable + acknowledged.** The worker ACKs an event only after processing
  it. If the worker dies mid-event, the entry stays in the consumer's pending
  list and is re-delivered on restart (at-least-once). Pub/sub would drop it.
- **The stream is the event log.** Entries are retained (MAXLEN-capped), so
  `python -m app.discovery.replay --source redis` can re-run them offline.
- **Producers don't care whether anyone is listening.** Ingestion keeps
  publishing while the worker is stopped; the worker's staleness gate decides
  what's still worth triaging when it comes back.

At ~800 events/day, Redis Streams is the right size; Kafka/Redpanda stays
unearned (CLAUDE.md §6).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from app.core.logging import get_logger
from app.discovery.types import DiscoveryEvent

if TYPE_CHECKING:
    from redis.asyncio import Redis

_log = get_logger(__name__)


@runtime_checkable
class EventBus(Protocol):
    async def publish(self, event: DiscoveryEvent) -> None: ...
    async def consume(self) -> DiscoveryEvent: ...
    async def ack(self, event: DiscoveryEvent) -> None: ...
    def qsize(self) -> int: ...


def _stamp(event: DiscoveryEvent) -> DiscoveryEvent:
    """Record when the event entered the bus (the staleness gate's clock —
    `published_at` is the source's timestamp, which for EDGAR is just a date)."""
    if event.ingested_at is not None:
        return event
    return event.model_copy(update={"ingested_at": datetime.now(UTC)})


class InMemoryEventBus:
    """Single-process bus backed by asyncio.Queue.

    `maxsize` bounds the buffer so a runaway producer can't OOM the process —
    publishers will await space; the queue applies natural back-pressure.
    """

    def __init__(self, maxsize: int = 1000) -> None:
        self._queue: asyncio.Queue[DiscoveryEvent] = asyncio.Queue(maxsize=maxsize)

    async def publish(self, event: DiscoveryEvent) -> None:
        await self._queue.put(_stamp(event))

    async def consume(self) -> DiscoveryEvent:
        return await self._queue.get()

    async def ack(self, event: DiscoveryEvent) -> None:
        """Nothing to acknowledge: an in-memory event is gone once consumed."""

    def qsize(self) -> int:
        return self._queue.qsize()


class RedisStreamEventBus:
    """Redis Streams bus with one consumer group (see module docstring).

    One logical consumer (`consumer`) with a stable name, so a restarted
    worker reclaims its own pending entries before reading new ones.
    """

    FIELD = "event"

    def __init__(
        self,
        redis: Redis,
        *,
        stream: str = "terminal:events",
        group: str = "agent",
        consumer: str = "agent-worker",
        maxlen: int = 50_000,
        block_ms: int = 5_000,
    ) -> None:
        self._redis = redis
        self.stream = stream
        self.group = group
        self.consumer = consumer
        self._maxlen = maxlen
        self._block_ms = block_ms
        self._group_ready = False
        # On first consume, drain our own pending list (entries delivered to a
        # previous incarnation of this consumer but never ACKed) before ">".
        self._recovering = True
        self._inflight: dict[str, str] = {}  # event.id -> stream entry id
        self._depth = 0  # last-known undelivered + pending count (for qsize)
        self.redelivered = 0
        self.poison_dropped = 0

    async def ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            # id="0": a brand-new group starts from the beginning of whatever
            # is retained, so events published before the first worker start
            # are not silently skipped.
            await self._redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as e:  # BUSYGROUP: already exists — fine
            if "BUSYGROUP" not in str(e):
                raise
        self._group_ready = True

    async def publish(self, event: DiscoveryEvent) -> None:
        await self._redis.xadd(
            self.stream,
            {self.FIELD: _stamp(event).model_dump_json()},
            maxlen=self._maxlen,
            approximate=True,
        )

    async def consume(self) -> DiscoveryEvent:
        await self.ensure_group()
        while True:
            if self._recovering:
                resp = await self._redis.xreadgroup(
                    self.group, self.consumer, {self.stream: "0"}, count=1
                )
                entries = _entries(resp)
                if not entries:
                    self._recovering = False
                    continue
                self.redelivered += 1
            else:
                await self._refresh_depth()
                resp = await self._redis.xreadgroup(
                    self.group,
                    self.consumer,
                    {self.stream: ">"},
                    count=1,
                    block=self._block_ms,
                )
                entries = _entries(resp)
                if not entries:
                    # Yield explicitly: a client that completes the blocking read
                    # without suspending (fakeredis does) would otherwise spin
                    # this loop and starve the runner's stop signal.
                    await asyncio.sleep(0)
                    continue

            entry_id, fields = entries[0]
            raw = fields.get(self.FIELD) if fields else None
            try:
                event = DiscoveryEvent.model_validate_json(raw or "")
            except Exception as e:
                # A malformed entry would be re-delivered forever; ACK it away.
                self.poison_dropped += 1
                _log.warning("event_bus_poison_entry", entry_id=entry_id, error=str(e)[:200])
                await self._redis.xack(self.stream, self.group, entry_id)
                continue
            self._inflight[event.id] = entry_id
            return event

    async def ack(self, event: DiscoveryEvent) -> None:
        entry_id = self._inflight.pop(event.id, None)
        if entry_id is not None:
            await self._redis.xack(self.stream, self.group, entry_id)

    def qsize(self) -> int:
        return self._depth

    async def _refresh_depth(self) -> None:
        """Best-effort backlog size: undelivered (`lag`, Redis >= 7) + pending."""
        with contextlib.suppress(Exception):
            for g in await self._redis.xinfo_groups(self.stream):
                if _field(g, "name") == self.group:
                    lag = _field(g, "lag") or 0
                    pending = _field(g, "pending") or 0
                    self._depth = int(lag) + int(pending)
                    return


def _entries(resp: Any) -> list[tuple[str, dict[str, str]]]:
    """XREADGROUP reply -> [(entry_id, fields)] for our single stream."""
    if not resp:
        return []
    # redis-py returns [[stream, [(id, fields), ...]]] (RESP2) or a dict (RESP3).
    if isinstance(resp, dict):
        streams = list(resp.values())
        return list(streams[0][0]) if streams and streams[0] else []
    return list(resp[0][1])


def _field(group_info: Any, key: str) -> Any:
    if isinstance(group_info, dict):
        return group_info.get(key, group_info.get(key.encode(), None))
    return None


def build_event_bus() -> EventBus:
    """The configured bus (EVENT_BUS=memory|redis)."""
    from app.core.config import get_settings

    s = get_settings()
    if s.event_bus == "redis":
        from redis.asyncio import Redis

        block_ms = 5_000
        return RedisStreamEventBus(
            # The socket read timeout must comfortably exceed XREADGROUP's
            # BLOCK window, or every idle read races it and raises
            # "Timeout reading from redis".
            Redis.from_url(
                s.redis_url,
                decode_responses=True,
                socket_timeout=block_ms / 1000 + 10,
                socket_keepalive=True,
                health_check_interval=30,
            ),
            stream=s.event_stream_key,
            maxlen=s.event_stream_maxlen,
            block_ms=block_ms,
        )
    return InMemoryEventBus(maxsize=1000)
