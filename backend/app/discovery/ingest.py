"""Ingestion service — the always-on producer (CLAUDE.md §4 architecture).

Owns the long-lived connections: the SEC EDGAR latest-filings poller and the
Alpaca news websocket (universe-wide, wildcard `*`). Normalizes everything
into DiscoveryEvents and publishes them to the event bus. Dedup state lives
in Postgres, so restarts don't re-publish old filings.

Runs two ways, same class:

- **embedded** (INGESTION_MODE=embedded): the agent worker starts an
  IngestionService in its own process, publishing to whichever bus is
  configured. Simple; what tests and a plain `uvicorn` run use.
- **external** (docker-compose): its own container,
  `python -m app.discovery.ingest`, publishing to Redis Streams. It writes a
  heartbeat snapshot to Redis every few seconds (with a TTL), which is how
  the worker's /autonomous/status reports ingestion health without the two
  processes ever calling each other.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.data.types import ProviderUnavailable, ProviderUnavailableReason
from app.discovery.alpaca_news_stream import AlpacaNewsStream
from app.discovery.bus import EventBus
from app.discovery.edgar_poller import EdgarPoller

_log = get_logger(__name__)

STATUS_KEY = "terminal:ingest:status"
HEARTBEAT_SECONDS = 10.0
# Missing two-plus heartbeats = the ingestion process is down.
STATUS_TTL_SECONDS = 45


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


class IngestionService:
    """EDGAR poller + Alpaca news WS, publishing to one bus."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self.poller: EdgarPoller | None = None
        self.news: AlpacaNewsStream | None = None
        self.started_at: datetime | None = None

    async def start(self) -> None:
        from app.data.registry import get_edgar
        from app.eval.persistence import get_session_factory

        settings = get_settings()
        self.poller = EdgarPoller(
            filings=get_edgar(),
            bus=self._bus,
            poll_interval_seconds=settings.discovery_poll_interval_seconds,
            # High-volume forms (Form 4 insider trades) are filtered to the
            # watchlist so the triage budget isn't eaten by routine insider
            # filings from no-name issuers.
            watchlist=settings.watchlist,
            session_factory=get_session_factory(),
        )
        # Universe-wide news. Skipped if Alpaca creds aren't set — the poller
        # alone keeps the pipeline alive.
        try:
            self.news = AlpacaNewsStream(bus=self._bus, session_factory=get_session_factory())
        except ProviderUnavailable as e:
            if e.reason != ProviderUnavailableReason.AUTH_MISSING:
                raise
            _log.warning("alpaca_news_stream_disabled", reason="ALPACA_API_KEY/SECRET not set")
            self.news = None
        await self.poller.start()
        if self.news is not None:
            await self.news.start()
        self.started_at = datetime.now(UTC)
        _log.info("ingestion_started", news_enabled=self.news is not None)

    async def stop(self) -> None:
        if self.news is not None:
            try:
                await self.news.stop()
            except Exception as e:  # pragma: no cover
                _log.exception("ingestion_news_stop_failed", error=str(e))
        if self.poller is not None:
            try:
                await self.poller.stop()
            except Exception as e:  # pragma: no cover
                _log.exception("ingestion_poller_stop_failed", error=str(e))
        _log.info("ingestion_stopped")

    def snapshot(self) -> dict[str, Any]:
        """JSON-able health counters (same shape in both modes)."""
        p, n = self.poller, self.news
        return {
            "started_at": _iso(self.started_at),
            "heartbeat_at": datetime.now(UTC).isoformat(),
            "poll_interval_seconds": p.poll_interval if p else None,
            "polls_completed": p.polls_completed if p else 0,
            "events_published": p.events_published if p else 0,
            "last_poll_at": _iso(p.last_poll_at) if p else None,
            "poller_last_error": p.last_error if p else None,
            "news_state": n.state if n else "disabled",
            "news_connected": n.connected if n else False,
            "news_connects": n.connects if n else 0,
            "news_messages": n.messages_received if n else 0,
            "news_events_published": n.events_published if n else 0,
            "news_duplicates_dropped": n.duplicates_dropped if n else 0,
            "last_news_message_at": _iso(n.last_message_at) if n else None,
            "news_last_error": n.last_error if n else None,
        }


async def read_status(redis: Any) -> dict[str, Any] | None:
    """The external ingestion service's latest heartbeat, or None if it's down."""
    raw = await redis.get(STATUS_KEY)
    if not raw:
        return None
    try:
        data: dict[str, Any] = json.loads(raw)
    except ValueError:
        return None
    return data


async def _heartbeat(redis: Any, service: IngestionService, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await redis.set(STATUS_KEY, json.dumps(service.snapshot()), ex=STATUS_TTL_SECONDS)
        except Exception as e:
            _log.warning("ingestion_heartbeat_failed", error=str(e)[:200])
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)


async def run() -> None:
    """Entrypoint for the standalone ingestion container."""
    configure_logging()
    settings = get_settings()
    if settings.event_bus != "redis":
        raise SystemExit("The standalone ingestion service needs EVENT_BUS=redis.")

    from redis.asyncio import Redis

    from app.discovery.bus import build_event_bus

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    service = IngestionService(build_event_bus())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    await service.start()
    beat = asyncio.create_task(_heartbeat(redis, service, stop))
    await stop.wait()
    await service.stop()
    beat.cancel()
    with contextlib.suppress(Exception):
        await redis.delete(STATUS_KEY)  # report "down" immediately, not after TTL
    await redis.aclose()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(run())
