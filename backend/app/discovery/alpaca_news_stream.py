"""Alpaca News WebSocket source — universe-wide news ingestion (DESIGN.md §4, §5).

Always-on connection to Alpaca's Benzinga news feed, subscribed with the
wildcard `*` so every article across the market lands in our event bus.
Typical volume: ~130 articles/day, comfortably under triage budget.

Why a separate class from EdgarPoller: that's a polling source over HTTP;
this is a long-lived WebSocket. Different lifecycle, different failure
modes. Both publish into the same DiscoveryEvent shape on the same bus —
downstream (triage + reactive runner) doesn't care which source emitted.

Resilience:
- Auto-reconnect with exponential backoff on connection drop.
- Persistent dedup via the same seen_discovery_events table EdgarPoller
  uses (source="alpaca-news") — a restart doesn't re-publish old articles.
- Auth failures are non-retryable (raises ProviderUnavailable).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, WebSocketException

from app.core.config import get_settings
from app.core.logging import get_logger
from app.data.types import ProviderUnavailable, ProviderUnavailableReason
from app.discovery.bus import EventBus
from app.discovery.types import DiscoveryEvent

try:
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.data.upsert import dialect_insert
    from app.discovery.models import SeenDiscoveryEvent
except ImportError:  # pragma: no cover — defensive only
    AsyncSession = AsyncSessionMaker = None  # type: ignore[assignment,misc]

_log = get_logger(__name__)

# Alpaca's Benzinga news WS endpoint (the v1beta1 stream is the supported one
# on free Basic plans — DESIGN.md §5).
_NEWS_WS_URL = "wss://stream.data.alpaca.markets/v1beta1/news"

# Auth + subscribe must each complete within this window or we reconnect.
_HANDSHAKE_TIMEOUT_SECONDS = 10.0


class AlpacaNewsStream:
    """Universe-wide news source. Same start/stop shape as EdgarPoller."""

    SOURCE_NAME = "alpaca-news"

    def __init__(
        self,
        *,
        bus: EventBus,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        url: str = _NEWS_WS_URL,
        symbols: list[str] | None = None,
        # Reconnect tuning. With exponential backoff capped at 60s, a drop is
        # at most ~1 min of missed news.
        initial_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 60.0,
    ) -> None:
        settings = get_settings()
        key = api_key if api_key is not None else settings.alpaca_api_key
        secret = api_secret if api_secret is not None else settings.alpaca_api_secret
        if not key or not secret:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.AUTH_MISSING,
                message="ALPACA_API_KEY and ALPACA_API_SECRET must be set",
                provider=self.SOURCE_NAME,
                retryable=False,
            )
        self._key = key
        self._secret = secret
        self._url = url
        # Wildcard subscribes to every symbol the feed covers (DESIGN.md §4).
        self._symbols = symbols or ["*"]
        self._bus = bus
        self._session_factory = session_factory
        self._initial_backoff = initial_backoff_seconds
        self._max_backoff = max_backoff_seconds

        self._seen: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

        # Counters surfaced via worker.status().
        self.connects = 0
        self.disconnects = 0
        self.messages_received = 0
        self.events_published = 0
        self.duplicates_dropped = 0
        self.last_message_at: datetime | None = None
        self.last_error: str | None = None
        self.connected = False

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        await self._load_seen_from_db()
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())
        _log.info(
            "alpaca_news_started",
            url=self._url,
            symbols=self._symbols,
            seen_count_loaded=len(self._seen),
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                self._task.cancel()
            except asyncio.CancelledError:
                pass
        self._task = None
        self.connected = False
        _log.info("alpaca_news_stopped")

    async def _run(self) -> None:
        """Connect-loop. Auto-reconnects with exponential backoff on drop."""
        backoff = self._initial_backoff
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self._url, ping_interval=20.0) as ws:
                    self.connects += 1
                    self.connected = True
                    _log.info("alpaca_news_connected")
                    try:
                        await self._authenticate(ws)
                        await self._subscribe(ws)
                        # Only a fully-established session resets backoff. A
                        # TCP connect that then fails the handshake must keep
                        # backing off, or a handshake bug turns into a
                        # reconnect storm (observed: 10k attempts at ~4s).
                        backoff = self._initial_backoff
                        await self._consume_messages(ws)
                    finally:
                        self.connected = False
                        self.disconnects += 1
            except ProviderUnavailable:
                # Auth issue — not retryable. Re-raise so .start() surfaces it.
                raise
            except (ConnectionClosed, WebSocketException, OSError) as e:
                self.last_error = f"{type(e).__name__}: {e}"
                _log.warning("alpaca_news_disconnected", error=str(e), backoff=backoff)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                _log.exception("alpaca_news_unexpected_error", error=str(e))

            if self._stop_event.is_set():
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
            backoff = min(backoff * 2, self._max_backoff)

    async def _authenticate(self, ws: ClientConnection) -> None:
        """Send auth and wait for the "authenticated" confirmation.

        Alpaca's data websockets greet every new connection with
        `[{"T":"success","msg":"connected"}]` BEFORE any auth response, so
        we must keep reading until the confirmation arrives — reading a
        single message sees only the greeting and fails the handshake
        (the bug behind the 10k-reconnect storm)."""
        await ws.send(json.dumps({"action": "auth", "key": self._key, "secret": self._secret}))
        async for msg in self._handshake_messages(ws, waiting_for="auth"):
            mtype = msg.get("T")
            if mtype == "success" and msg.get("msg") == "authenticated":
                return
            if mtype == "success":
                continue  # "connected" greeting or other informational success
            if mtype == "error":
                code = msg.get("code")
                if code in (401, 402, 406):
                    raise ProviderUnavailable(
                        reason=ProviderUnavailableReason.AUTH_MISSING,
                        message=f"Alpaca news auth failed: {msg.get('msg')}",
                        provider=self.SOURCE_NAME,
                        retryable=False,
                    )
                raise RuntimeError(f"Alpaca news error during auth: {msg}")
        raise RuntimeError("Alpaca news stream closed before auth response")

    async def _subscribe(self, ws: ClientConnection) -> None:
        await ws.send(json.dumps({"action": "subscribe", "news": self._symbols}))
        async for msg in self._handshake_messages(ws, waiting_for="subscribe"):
            if msg.get("T") == "subscription":
                return
            if msg.get("T") == "error":
                raise RuntimeError(f"Alpaca news error during subscribe: {msg}")
            # Tolerate any other interleaved message (success echoes, etc.).
        raise RuntimeError("Alpaca news stream closed before subscribe response")

    async def _handshake_messages(
        self, ws: ClientConnection, *, waiting_for: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield decoded messages until the handshake deadline passes."""
        deadline = asyncio.get_running_loop().time() + _HANDSHAKE_TIMEOUT_SECONDS
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise RuntimeError(f"Alpaca news WS timed out waiting for {waiting_for} response")
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except TimeoutError as e:
                raise RuntimeError(
                    f"Alpaca news WS timed out waiting for {waiting_for} response"
                ) from e
            for msg in _decode(raw):
                yield msg

    async def _consume_messages(self, ws: ClientConnection) -> None:
        session: AsyncSession | None = None
        if self._session_factory is not None:
            session = self._session_factory()
            await session.__aenter__()
        try:
            async for raw in ws:
                if self._stop_event.is_set():
                    break
                messages = _decode(raw)
                for msg in messages:
                    if msg.get("T") != "n":  # "n" = news item
                        continue
                    self.messages_received += 1
                    self.last_message_at = datetime.now(UTC)
                    await self._handle_news(msg, session)
                if session is not None:
                    await session.commit()
        finally:
            if session is not None:
                await session.__aexit__(None, None, None)

    async def _handle_news(self, msg: dict[str, Any], session: AsyncSession | None) -> None:
        external_id = str(msg.get("id"))
        if not external_id or external_id == "None":
            return
        if external_id in self._seen:
            self.duplicates_dropped += 1
            return
        self._seen.add(external_id)
        if session is not None:
            await self._persist_seen(session, external_id)

        event = _news_to_event(msg)
        await self._bus.publish(event)
        self.events_published += 1
        _log.info("alpaca_news_published", summary=event.short_summary())

    async def _load_seen_from_db(self) -> None:
        if self._session_factory is None:
            return
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(SeenDiscoveryEvent.external_id).where(
                            SeenDiscoveryEvent.source == self.SOURCE_NAME
                        )
                    )
                )
                .scalars()
                .all()
            )
        self._seen.update(rows)

    async def _persist_seen(self, session: AsyncSession, external_id: str) -> None:
        if self._session_factory is None:
            return
        insert = dialect_insert(session)
        stmt = insert(SeenDiscoveryEvent).values(
            source=self.SOURCE_NAME,
            external_id=external_id,
            seen_at=datetime.now(UTC),
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=["source", "external_id"])
        try:
            await session.execute(stmt)
        except IntegrityError:
            await session.rollback()


def _decode(raw: str | bytes) -> list[dict[str, Any]]:
    """Alpaca sends arrays of messages; sometimes a single dict. Normalize to list."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    data = json.loads(raw)
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _news_to_event(msg: dict[str, Any]) -> DiscoveryEvent:
    """Translate an Alpaca news message to our DiscoveryEvent envelope."""
    symbols = [s.upper() for s in msg.get("symbols") or [] if isinstance(s, str)]
    headline = (msg.get("headline") or "").strip()
    body = (msg.get("summary") or msg.get("content") or "").strip() or None
    published_at = msg.get("created_at") or msg.get("updated_at")
    try:
        ts = (
            datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
            if published_at
            else datetime.now(UTC)
        )
    except ValueError:
        ts = datetime.now(UTC)
    return DiscoveryEvent(
        id=str(msg.get("id")),
        source="alpaca-news",
        kind="news",
        symbols=symbols,
        headline=headline or "(no headline)",
        body=body,
        url=msg.get("url"),
        published_at=ts,
        payload={
            "author": msg.get("author"),
            "source": msg.get("source"),
            "id": msg.get("id"),
        },
    )
