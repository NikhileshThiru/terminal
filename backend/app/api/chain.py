"""/chain — options chain viewer endpoints (Phase 3, DESIGN.md §9).

Read-only views over the OptionsProvider so the frontend can browse a
chain without going through the agent. Backed by Alpaca's indicative-
pricing options feed (free, 15-min delayed, paper-account).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.core.logging import get_logger
from app.data.alpaca import AlpacaProvider
from app.data.interfaces import OptionsProvider, PriceProvider
from app.data.types import ProviderUnavailable

_log = get_logger(__name__)

router = APIRouter(prefix="/chain", tags=["chain"])


@lru_cache(maxsize=1)
def _provider() -> AlpacaProvider:
    return AlpacaProvider()


def get_options_provider() -> OptionsProvider:
    return _provider()


def get_price_provider() -> PriceProvider:
    return _provider()


class ExpirationsResponse(BaseModel):
    symbol: str
    expirations: list[date]


class OptionRow(BaseModel):
    occ_symbol: str
    expiration: date
    strike: Decimal
    option_type: str  # call | put
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    mid: Decimal | None


class ChainResponse(BaseModel):
    symbol: str
    expiration: date
    underlying_price: Decimal | None
    calls: list[OptionRow]
    puts: list[OptionRow]


def _mid(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / Decimal(2)
    return None


@router.get("/{symbol}/expirations", response_model=ExpirationsResponse)
async def list_expirations(symbol: str) -> ExpirationsResponse:
    try:
        exps = await get_options_provider().get_expirations(symbol.upper())
    except ProviderUnavailable as e:
        raise HTTPException(
            status_code=503,
            detail={"reason": e.reason.value, "provider": e.provider, "message": str(e)},
        ) from e
    today = date.today()
    future = sorted({e for e in exps if e >= today})
    return ExpirationsResponse(symbol=symbol.upper(), expirations=future)


@router.get("/{symbol}", response_model=ChainResponse)
async def get_chain(
    symbol: str,
    expiration: date = Query(description="YYYY-MM-DD"),
) -> ChainResponse:
    options = get_options_provider()
    prices = get_price_provider()
    sym = symbol.upper()
    try:
        contracts = await options.get_chain(sym, expiration)
    except ProviderUnavailable as e:
        raise HTTPException(
            status_code=503,
            detail={"reason": e.reason.value, "provider": e.provider, "message": str(e)},
        ) from e

    # Underlying — best-effort; don't fail the chain response if quote is missing.
    # After hours the IEX quote is often one-sided (ask=0): averaging it in
    # would halve the spot and mis-place the ATM highlight, so treat zeros as
    # missing and fall back to the latest daily close.
    underlying: Decimal | None = None
    try:
        q = await prices.get_latest_quote(sym)
        underlying = q.safe_price()
    except ProviderUnavailable:
        underlying = None
    if underlying is None:
        try:
            now = datetime.now(UTC)
            bars = await prices.get_ohlc(sym, now - timedelta(days=7), now, timeframe="1Day")
            if bars:
                underlying = bars[-1].close
        except ProviderUnavailable:
            underlying = None

    calls: list[OptionRow] = []
    puts: list[OptionRow] = []
    for c in sorted(contracts, key=lambda x: x.strike):
        row = OptionRow(
            occ_symbol=c.occ_symbol,
            expiration=c.expiration,
            strike=c.strike,
            option_type=c.option_type,
            bid=c.bid,
            ask=c.ask,
            last=c.last,
            mid=_mid(c.bid, c.ask),
        )
        (calls if c.option_type == "call" else puts).append(row)

    return ChainResponse(
        symbol=sym,
        expiration=expiration,
        underlying_price=underlying,
        calls=calls,
        puts=puts,
    )
