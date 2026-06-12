"""AlpacaNewsStream tests — focus on the message-handler + decode + dedup paths.

We don't test the actual WebSocket connection here (that would need a real
mock-WS server). Instead we exercise the pure-functional bits + the
_handle_news path which is where the dedup / publish logic lives.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.discovery.alpaca_news_stream import (
    AlpacaNewsStream,
    _decode,
    _news_to_event,
)
from app.discovery.bus import InMemoryEventBus
from app.discovery.models import SeenDiscoveryEvent
from app.eval.models import Base

# === Pure decode/translate helpers ===


def test_decode_handles_single_message_dict() -> None:
    assert _decode('{"T": "n", "id": 1}') == [{"T": "n", "id": 1}]


def test_decode_handles_array_of_messages() -> None:
    assert _decode('[{"T":"n","id":1},{"T":"n","id":2}]') == [
        {"T": "n", "id": 1},
        {"T": "n", "id": 2},
    ]


def test_decode_handles_bytes() -> None:
    assert _decode(b'{"T":"n","id":1}') == [{"T": "n", "id": 1}]


def test_decode_returns_empty_list_for_garbage_shape() -> None:
    assert _decode("42") == []
    assert _decode('"a string"') == []


def test_news_to_event_canonicalizes_symbols() -> None:
    ev = _news_to_event(
        {
            "id": 12345,
            "headline": "AAPL beats",
            "summary": "details",
            "symbols": ["aapl", "MsFt"],
            "created_at": "2026-06-04T10:30:00Z",
            "url": "https://example.com/x",
        }
    )
    assert ev.id == "12345"
    assert ev.symbols == ["AAPL", "MSFT"]
    assert ev.headline == "AAPL beats"
    assert ev.body == "details"
    assert ev.source == "alpaca-news"
    assert ev.kind == "news"


def test_news_to_event_falls_back_when_headline_missing() -> None:
    ev = _news_to_event({"id": 1, "symbols": []})
    assert ev.headline == "(no headline)"


# === _handle_news (dedup + publish) ===


@pytest.fixture
def alpaca_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure auth check passes — the stream's __init__ requires both keys."""
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")
    from app.core.config import get_settings

    get_settings.cache_clear()


@pytest.fixture
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_handle_news_publishes_event_and_persists_dedup(alpaca_env, db_factory) -> None:
    bus = InMemoryEventBus()
    stream = AlpacaNewsStream(bus=bus, session_factory=db_factory)
    async with db_factory() as session:
        msg: dict[str, Any] = {"id": 999, "headline": "AAPL up", "symbols": ["AAPL"]}
        await stream._handle_news(msg, session)
        await session.commit()

    assert stream.events_published == 1
    assert bus.qsize() == 1
    ev = await bus.consume()
    assert ev.symbols == ["AAPL"]

    async with db_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(SeenDiscoveryEvent).where(SeenDiscoveryEvent.source == "alpaca-news")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].external_id == "999"


@pytest.mark.asyncio
async def test_handle_news_dedupes_repeated_id(alpaca_env, db_factory) -> None:
    bus = InMemoryEventBus()
    stream = AlpacaNewsStream(bus=bus, session_factory=db_factory)
    async with db_factory() as session:
        msg = {"id": 1, "headline": "A", "symbols": []}
        await stream._handle_news(msg, session)
        await stream._handle_news(msg, session)  # duplicate
        await session.commit()
    assert stream.events_published == 1
    assert stream.duplicates_dropped == 1
    assert bus.qsize() == 1


@pytest.mark.asyncio
async def test_load_seen_from_db_warms_in_memory_set(alpaca_env, db_factory) -> None:
    """A new stream constructed against a DB with prior dedup state skips those ids."""
    bus = InMemoryEventBus()
    # First stream sees msg id=1, persists to dedup.
    stream_a = AlpacaNewsStream(bus=bus, session_factory=db_factory)
    async with db_factory() as session:
        await stream_a._handle_news({"id": 1, "headline": "A", "symbols": []}, session)
        await session.commit()
    # Second stream (restart): loads dedup state from DB, drops the same id.
    stream_b = AlpacaNewsStream(bus=bus, session_factory=db_factory)
    await stream_b._load_seen_from_db()
    async with db_factory() as session:
        await stream_b._handle_news({"id": 1, "headline": "A", "symbols": []}, session)
    assert stream_b.events_published == 0
    assert stream_b.duplicates_dropped == 1


# === WebSocket handshake (auth + subscribe) ===
#
# Alpaca's data websockets greet every new connection with
# [{"T":"success","msg":"connected"}] BEFORE responding to auth. The
# handshake must tolerate that greeting (and any interleaved success
# messages) — reading a single message and giving up caused a reconnect
# storm: 10k+ connection attempts, zero news consumed.


class _FakeWS:
    """Scripted websocket: recv() pops pre-canned frames, send() records."""

    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        if not self._frames:
            raise AssertionError("FakeWS script exhausted — handshake read too many frames")
        return self._frames.pop(0)


@pytest.mark.asyncio
async def test_authenticate_tolerates_connected_greeting(alpaca_env) -> None:
    stream = AlpacaNewsStream(bus=InMemoryEventBus())
    ws = _FakeWS(
        [
            '[{"T":"success","msg":"connected"}]',
            '[{"T":"success","msg":"authenticated"}]',
        ]
    )
    await stream._authenticate(ws)  # type: ignore[arg-type]  # duck-typed fake
    assert '"action": "auth"' in ws.sent[0]


@pytest.mark.asyncio
async def test_authenticate_raises_provider_unavailable_on_auth_error(alpaca_env) -> None:
    from app.data.types import ProviderUnavailable

    stream = AlpacaNewsStream(bus=InMemoryEventBus())
    ws = _FakeWS(
        [
            '[{"T":"success","msg":"connected"}]',
            '[{"T":"error","code":401,"msg":"not authenticated"}]',
        ]
    )
    with pytest.raises(ProviderUnavailable):
        await stream._authenticate(ws)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_subscribe_tolerates_interleaved_success_messages(alpaca_env) -> None:
    stream = AlpacaNewsStream(bus=InMemoryEventBus())
    ws = _FakeWS(
        [
            '[{"T":"success","msg":"authenticated"}]',
            '[{"T":"subscription","news":["*"]}]',
        ]
    )
    await stream._subscribe(ws)  # type: ignore[arg-type]
    assert '"action": "subscribe"' in ws.sent[0]
