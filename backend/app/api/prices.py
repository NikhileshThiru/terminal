"""/prices/stream — SSE-proxied live(ish) quotes for the watchlist sidebar.

True Alpaca WebSocket integration is more plumbing than the demo needs and
the free-tier feed is 15-min delayed anyway (DESIGN.md §5). Polling the
latest-quote endpoint every few seconds gives the same UX with a fraction
of the moving parts. The frontend subscribes once and receives one event
per symbol per tick.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from functools import lru_cache

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.core.config import get_settings
from app.core.logging import get_logger
from app.data.alpaca import AlpacaProvider
from app.data.interfaces import PriceProvider
from app.data.types import ProviderUnavailable

_log = get_logger(__name__)

router = APIRouter(prefix="/prices", tags=["prices"])


@lru_cache(maxsize=1)
def _provider() -> AlpacaProvider:
    return AlpacaProvider()


def get_price_provider() -> PriceProvider:
    return _provider()


def _mid(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / Decimal(2)
    return None


@router.get("/stream")
async def stream_prices(
    symbols: str = Query(description="Comma-separated tickers, e.g. AAPL,NVDA,TSLA"),
    interval: float = Query(default=5.0, ge=1.0, le=60.0),
) -> StreamingResponse:
    """SSE stream of latest quotes for the requested symbols. One event per
    symbol per poll. Stops when the client disconnects."""
    settings = get_settings()
    # Free-tier Alpaca WS limit is 30 symbols; respect it on the polling
    # side too so we never accidentally drift over the rate budget.
    requested = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    syms = list(dict.fromkeys(requested))[:30]
    provider = get_price_provider()

    async def event_gen() -> AsyncIterator[str]:
        try:
            # Send a hello so the client can render the symbol list immediately
            # without waiting for the first poll cycle.
            yield (f"event: hello\ndata: {json.dumps({'symbols': syms, 'interval': interval})}\n\n")
            while True:
                for sym in syms:
                    try:
                        q = await provider.get_latest_quote(sym)
                        mid = _mid(q.bid, q.ask)
                        payload = {
                            "symbol": sym,
                            "bid": str(q.bid) if q.bid is not None else None,
                            "ask": str(q.ask) if q.ask is not None else None,
                            "mid": str(mid) if mid is not None else None,
                            "ts": q.timestamp.isoformat(),
                        }
                        yield f"event: quote\ndata: {json.dumps(payload)}\n\n"
                    except ProviderUnavailable as e:
                        payload = {"symbol": sym, "reason": e.reason.value}
                        yield f"event: unavailable\ndata: {json.dumps(payload)}\n\n"
                    except Exception:
                        _log.exception("price_stream_iter_failed", symbol=sym)
                # Heartbeat so clients can detect a stalled stream + so reverse
                # proxies don't time us out during quiet periods.
                yield f"event: tick\ndata: {json.dumps({'interval': interval})}\n\n"
                await asyncio.sleep(interval)
        except asyncio.CancelledError:  # pragma: no cover
            # Client went away — let the runner unwind cleanly.
            raise

    # `interval` is used; `settings` is here so future calls can scope to
    # watchlist defaults if no symbols are passed.
    _ = settings
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(event_gen(), media_type="text/event-stream", headers=headers)
