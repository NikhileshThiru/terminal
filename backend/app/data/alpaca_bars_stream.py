"""Alpaca IEX bars WebSocket — live-tick source for the chart pane.

Singleton service that holds one WS connection to Alpaca's IEX bars
stream and multiplexes incoming bars to per-symbol SSE subscribers.
Ref-counts subscriptions: when the first SSE client for a symbol
arrives, sends Alpaca `subscribe`; when the last one leaves, sends
`unsubscribe` so we never pay for symbols no one is looking at.

Free-tier IEX feed: 1-minute bars, ~2% of US equity volume (single
exchange). Quiet symbols may sit idle for minutes; active symbols tick
every minute during regular hours. DESIGN.md §5.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, WebSocketException

from app.core.config import get_settings
from app.core.logging import get_logger
from app.data.types import ProviderUnavailable, ProviderUnavailableReason

_log = get_logger(__name__)

_BARS_WS_URL = "wss://stream.data.alpaca.markets/v2/iex"


class LiveBar:
    """One bar emitted to subscribers. Plain object so SSE can serialise
    it directly without an ORM round trip."""

    __slots__ = ("close", "high", "low", "open", "symbol", "timestamp", "volume")

    def __init__(
        self,
        *,
        symbol: str,
        timestamp: datetime,
        open: Decimal,
        high: Decimal,
        low: Decimal,
        close: Decimal,
        volume: int,
    ) -> None:
        self.symbol = symbol
        self.timestamp = timestamp
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume

    def to_payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "t": self.timestamp.isoformat(),
            "o": str(self.open),
            "h": str(self.high),
            "low": str(self.low),
            "c": str(self.close),
            "v": self.volume,
        }


class AlpacaBarsStream:
    """Ref-counted IEX bars subscription. One WS connection serves all
    consumers; per-symbol subscribe/unsubscribe messages are sent only
    when the ref-count for that symbol changes between 0 and 1."""

    _MAX_QUEUE = 64

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        url: str = _BARS_WS_URL,
        initial_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 60.0,
    ) -> None:
        settings = get_settings()
        self._key = api_key if api_key is not None else settings.alpaca_api_key
        self._secret = api_secret if api_secret is not None else settings.alpaca_api_secret
        if not self._key or not self._secret:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.AUTH_MISSING,
                message="ALPACA_API_KEY and ALPACA_API_SECRET must be set",
                provider="alpaca-bars",
                retryable=False,
            )
        self._url = url
        self._initial_backoff = initial_backoff_seconds
        self._max_backoff = max_backoff_seconds

        # Per-symbol subscriber queues.
        self._subscribers: dict[str, set[asyncio.Queue[LiveBar]]] = {}
        # Lock guards mutations to _subscribers and to the WS subscribe state.
        self._sub_lock = asyncio.Lock()

        self._ws: ClientConnection | None = None
        self._connect_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._runner_task: asyncio.Task[None] | None = None
        self._authenticated = asyncio.Event()
        # The set of symbols we're currently subscribed to on the Alpaca side,
        # so we can re-subscribe after a reconnect.
        self._alpaca_subs: set[str] = set()
        self.connected = False
        self.last_error: str | None = None

    @property
    def is_running(self) -> bool:
        return self._runner_task is not None and not self._runner_task.done()

    async def _ensure_running(self) -> None:
        """Start the connect-loop the first time anyone subscribes."""
        async with self._connect_lock:
            if self._runner_task is None or self._runner_task.done():
                self._stop_event.clear()
                self._authenticated.clear()
                self._runner_task = asyncio.create_task(self._run())

    async def shutdown(self) -> None:
        self._stop_event.set()
        if self._runner_task is not None:
            try:
                await asyncio.wait_for(self._runner_task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._runner_task.cancel()
        self._runner_task = None
        self.connected = False

    @contextlib.asynccontextmanager
    async def subscribe(self, symbol: str) -> AsyncIterator[asyncio.Queue[LiveBar]]:
        """Reserve a queue that receives bars for `symbol`. Caller is
        responsible for draining the queue promptly; full queues drop the
        oldest event. Use as `async with stream.subscribe('AAPL') as q:`."""
        sym = symbol.upper()
        queue: asyncio.Queue[LiveBar] = asyncio.Queue(maxsize=self._MAX_QUEUE)
        await self._ensure_running()
        async with self._sub_lock:
            existed = sym in self._subscribers and len(self._subscribers[sym]) > 0
            self._subscribers.setdefault(sym, set()).add(queue)
        if not existed:
            await self._send_alpaca_subscribe([sym])
        try:
            yield queue
        finally:
            async with self._sub_lock:
                queues = self._subscribers.get(sym)
                if queues is not None:
                    queues.discard(queue)
                    remaining = len(queues)
                    if remaining == 0:
                        del self._subscribers[sym]
                else:
                    remaining = 0
            if remaining == 0:
                await self._send_alpaca_unsubscribe([sym])

    async def _send_alpaca_subscribe(self, symbols: list[str]) -> None:
        # Hold off until the WS is connected + authenticated; the runner
        # will re-send the full subscribe list on reconnect anyway.
        try:
            await asyncio.wait_for(self._authenticated.wait(), timeout=10.0)
        except TimeoutError:
            _log.warning("alpaca_bars_subscribe_timeout", symbols=symbols)
            return
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps({"action": "subscribe", "bars": symbols}))
            self._alpaca_subs.update(symbols)
        except Exception as e:
            _log.warning("alpaca_bars_subscribe_send_failed", error=str(e))

    async def _send_alpaca_unsubscribe(self, symbols: list[str]) -> None:
        ws = self._ws
        if ws is None:
            for s in symbols:
                self._alpaca_subs.discard(s)
            return
        try:
            await ws.send(json.dumps({"action": "unsubscribe", "bars": symbols}))
        except Exception as e:
            _log.warning("alpaca_bars_unsubscribe_send_failed", error=str(e))
        finally:
            for s in symbols:
                self._alpaca_subs.discard(s)

    async def _run(self) -> None:
        backoff = self._initial_backoff
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self._url, ping_interval=20.0) as ws:
                    self._ws = ws
                    self.connected = True
                    backoff = self._initial_backoff
                    _log.info("alpaca_bars_connected")
                    try:
                        await self._authenticate(ws)
                        self._authenticated.set()
                        # On reconnect, restore the subscriptions we had.
                        active_symbols = sorted(self._subscribers.keys())
                        if active_symbols:
                            await ws.send(
                                json.dumps({"action": "subscribe", "bars": active_symbols})
                            )
                            self._alpaca_subs = set(active_symbols)
                        await self._consume(ws)
                    finally:
                        self.connected = False
                        self._authenticated.clear()
                        self._ws = None
            except ProviderUnavailable as e:
                self.last_error = str(e)
                _log.exception("alpaca_bars_auth_failed")
                # Auth failure — don't reconnect; the user must fix creds.
                self._stop_event.set()
                break
            except (ConnectionClosed, WebSocketException, OSError) as e:
                self.last_error = f"{type(e).__name__}: {e}"
                _log.warning("alpaca_bars_disconnected", error=str(e), backoff=backoff)
            except Exception as e:  # pragma: no cover — guard against unexpected
                self.last_error = f"{type(e).__name__}: {e}"
                _log.exception("alpaca_bars_unexpected_error")
            if self._stop_event.is_set():
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
            backoff = min(backoff * 2, self._max_backoff)

    async def _authenticate(self, ws: ClientConnection) -> None:
        await ws.send(json.dumps({"action": "auth", "key": self._key, "secret": self._secret}))
        # Read messages until we see authenticated; raise on auth errors.
        async for raw in ws:
            messages = _decode(raw)
            for msg in messages:
                mtype = msg.get("T")
                if mtype == "success" and msg.get("msg") == "authenticated":
                    return
                if mtype == "error":
                    code = msg.get("code")
                    if code in (401, 402, 406):
                        raise ProviderUnavailable(
                            reason=ProviderUnavailableReason.AUTH_MISSING,
                            message=f"Alpaca bars auth failed: {msg.get('msg')}",
                            provider="alpaca-bars",
                            retryable=False,
                        )
                    raise RuntimeError(f"Alpaca bars error during auth: {msg}")
            return  # other messages can land after auth; bail this loop iter
        raise RuntimeError("Alpaca bars stream closed before auth response")

    async def _consume(self, ws: ClientConnection) -> None:
        async for raw in ws:
            if self._stop_event.is_set():
                break
            for msg in _decode(raw):
                if msg.get("T") != "b":  # "b" = bar
                    continue
                bar = _parse_bar(msg)
                if bar is None:
                    continue
                queues = list(self._subscribers.get(bar.symbol, ()))
                for q in queues:
                    try:
                        q.put_nowait(bar)
                    except asyncio.QueueFull:
                        # Drop oldest to make room.
                        try:
                            q.get_nowait()
                            q.put_nowait(bar)
                        except Exception:
                            pass


def _decode(raw: bytes | str) -> list[dict[str, Any]]:
    text = raw.decode() if isinstance(raw, bytes) else raw
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _parse_bar(msg: dict[str, Any]) -> LiveBar | None:
    try:
        return LiveBar(
            symbol=str(msg["S"]).upper(),
            timestamp=datetime.fromisoformat(str(msg["t"]).replace("Z", "+00:00")),
            open=Decimal(str(msg["o"])),
            high=Decimal(str(msg["h"])),
            low=Decimal(str(msg["l"])),
            close=Decimal(str(msg["c"])),
            volume=int(msg.get("v") or 0),
        )
    except (KeyError, ValueError, TypeError) as e:
        _log.warning("alpaca_bars_parse_failed", error=str(e), msg_keys=sorted(msg.keys()))
        return None


_singleton: AlpacaBarsStream | None = None


def get_bars_stream() -> AlpacaBarsStream:
    """Process-global singleton. Lazy-instantiated; callers should expect
    ProviderUnavailable on first construction if Alpaca creds are missing."""
    global _singleton
    if _singleton is None:
        _singleton = AlpacaBarsStream()
    return _singleton
