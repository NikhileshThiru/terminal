"""/bars — historical OHLC bars for the chart pane.

Thin read-only view over the PriceProvider's `get_ohlc`. Used by the
chart pane in the Bloomberg grid to render a price line/candle series
for the selected symbol. 15-min delayed (free-tier Alpaca IEX).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.logging import get_logger
from app.data.alpaca import AlpacaProvider
from app.data.alpaca_bars_stream import get_bars_stream
from app.data.interfaces import PriceProvider
from app.data.types import ProviderUnavailable

_log = get_logger(__name__)

router = APIRouter(prefix="/bars", tags=["bars"])


@lru_cache(maxsize=1)
def _provider() -> AlpacaProvider:
    return AlpacaProvider()


def get_price_provider() -> PriceProvider:
    return _provider()


class BarPoint(BaseModel):
    t: datetime
    o: Decimal
    h: Decimal
    low: Decimal
    c: Decimal
    v: int


class BarsResponse(BaseModel):
    symbol: str
    timeframe: str
    bars: list[BarPoint]


_VALID_TIMEFRAMES = {"1Day", "1Hour", "15Min", "5Min", "1Min"}


@router.get("/{symbol}", response_model=BarsResponse)
async def get_bars(
    symbol: str,
    timeframe: str = Query("1Day"),
    days: int = Query(90, ge=1, le=730),
) -> BarsResponse:
    if timeframe not in _VALID_TIMEFRAMES:
        raise HTTPException(
            status_code=400,
            detail=f"timeframe must be one of {sorted(_VALID_TIMEFRAMES)}",
        )
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    sym = symbol.upper()
    try:
        bars = await get_price_provider().get_ohlc(sym, start, end, timeframe)
    except ProviderUnavailable as e:
        raise HTTPException(
            status_code=503,
            detail={"reason": e.reason.value, "provider": e.provider, "message": str(e)},
        ) from e
    points = [
        BarPoint(t=b.timestamp, o=b.open, h=b.high, low=b.low, c=b.close, v=b.volume) for b in bars
    ]
    return BarsResponse(symbol=sym, timeframe=timeframe, bars=points)


class BatchBarsResponse(BaseModel):
    timeframe: str
    series: dict[str, list[BarPoint]]
    unavailable: list[str]


@router.get("/batch/series", response_model=BatchBarsResponse)
async def get_bars_batch(
    symbols: str = Query(description="Comma-separated tickers"),
    timeframe: str = Query("1Day"),
    days: int = Query(30, ge=1, le=180),
) -> BatchBarsResponse:
    """Fetch bars for several symbols in parallel. Used by the watchlist
    sparklines so the sidebar makes one round trip on mount instead of N.
    Failures per symbol come back in `unavailable` so a single missing
    feed doesn't sink the rest of the chart."""
    if timeframe not in _VALID_TIMEFRAMES:
        raise HTTPException(
            status_code=400,
            detail=f"timeframe must be one of {sorted(_VALID_TIMEFRAMES)}",
        )
    import asyncio

    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    requested = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    syms = list(dict.fromkeys(requested))[:30]
    provider = get_price_provider()

    async def fetch_one(sym: str) -> tuple[str, list[BarPoint] | None]:
        try:
            bars = await provider.get_ohlc(sym, start, end, timeframe)
            return sym, [
                BarPoint(t=b.timestamp, o=b.open, h=b.high, low=b.low, c=b.close, v=b.volume)
                for b in bars
            ]
        except ProviderUnavailable:
            return sym, None

    results = await asyncio.gather(*(fetch_one(s) for s in syms))
    series: dict[str, list[BarPoint]] = {}
    unavailable: list[str] = []
    for sym, points in results:
        if points is None:
            unavailable.append(sym)
        else:
            series[sym] = points
    return BatchBarsResponse(timeframe=timeframe, series=series, unavailable=unavailable)


@router.get("/{symbol}/stream")
async def stream_bars(symbol: str) -> StreamingResponse:
    """SSE: live 1-minute bars for one symbol. Multiplexed off the shared
    Alpaca IEX WebSocket so multiple browser tabs viewing the same ticker
    don't open multiple upstream subscriptions.

    Frontend uses this for intraday timeframes (1D/1W) so the chart ticks
    in real time as IEX bars close."""
    sym = symbol.upper()
    try:
        stream = get_bars_stream()
    except ProviderUnavailable as e:
        raise HTTPException(
            status_code=503,
            detail={"reason": e.reason.value, "provider": e.provider, "message": str(e)},
        ) from e

    async def gen() -> AsyncIterator[str]:
        try:
            async with stream.subscribe(sym) as queue:
                yield f"event: hello\ndata: {json.dumps({'symbol': sym})}\n\n"
                while True:
                    # Heartbeat every 25s during quiet windows so reverse
                    # proxies don't time out the connection.
                    try:
                        bar = await asyncio.wait_for(queue.get(), timeout=25.0)
                        yield f"event: bar\ndata: {json.dumps(bar.to_payload())}\n\n"
                    except TimeoutError:
                        yield f"event: tick\ndata: {json.dumps({})}\n\n"
        except asyncio.CancelledError:  # pragma: no cover
            raise

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)
