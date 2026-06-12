"""yfinance Price + Options provider (DESIGN.md §5 fallback for Alpaca).

yfinance is unofficial — it scrapes Yahoo Finance's HTML/JSON endpoints and
breaks periodically when Yahoo changes them. We use it as the fallback for
Alpaca prices/options. Always behind the BaseProvider resilience layer so a
yfinance outage doesn't crash the agent funnel.

yfinance is synchronous. We wrap its calls in `asyncio.to_thread` so the
async funnel doesn't block on network I/O.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import yfinance as yf

from app.core.config import get_settings
from app.data.base import BaseProvider
from app.data.cache import Cache
from app.data.circuit_breaker import CircuitBreaker
from app.data.rate_limit import RateLimiter
from app.data.types import (
    OHLCBar,
    OptionContract,
    OptionType,
    ProviderUnavailable,
    ProviderUnavailableReason,
    Quote,
)

_TIMEFRAME_TO_YF = {
    "1Min": "1m",
    "5Min": "5m",
    "15Min": "15m",
    "30Min": "30m",
    "1Hour": "1h",
    "1Day": "1d",
    "1Week": "1wk",
    "1Month": "1mo",
}


def _isnan(x: Any) -> bool:
    try:
        return bool(x != x)  # NaN is the only value not equal to itself
    except (TypeError, ValueError):
        return False


def _parse_occ(occ_symbol: str) -> tuple[str, date, str, Decimal]:
    """Parse an OCC option symbol like 'AAPL250117C00150000'.

    Layout (from the right): 8-char strike (x1000), 1-char type (C/P),
    6-char YYMMDD, then the underlying.
    """
    if len(occ_symbol) < 15:
        raise ValueError(f"OCC symbol too short: {occ_symbol!r}")
    strike_str = occ_symbol[-8:]
    type_letter = occ_symbol[-9]
    date_str = occ_symbol[-15:-9]
    underlying = occ_symbol[:-15]
    if type_letter not in ("C", "P"):
        raise ValueError(f"Invalid option type in OCC: {occ_symbol!r}")
    try:
        year = 2000 + int(date_str[:2])
        month = int(date_str[2:4])
        day = int(date_str[4:6])
        expiration = date(year, month, day)
        strike = Decimal(int(strike_str)) / Decimal(1000)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Cannot parse OCC symbol {occ_symbol!r}: {e}") from e
    return underlying, expiration, type_letter, strike


def _occ_symbol(symbol: str, expiration: date, opt_type: OptionType, strike: Decimal) -> str:
    type_letter = "C" if opt_type == "call" else "P"
    strike_int = int(strike * 1000)
    return f"{symbol.upper()}{expiration:%y%m%d}{type_letter}{strike_int:08d}"


class YFinanceProvider(BaseProvider):
    """PriceProvider + OptionsProvider via the yfinance library."""

    name = "yfinance"

    def __init__(
        self,
        *,
        rate_limiter: RateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        cache: Cache | None = None,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
        ticker_factory: Any | None = None,  # for tests; defaults to yf.Ticker
    ) -> None:
        # yfinance has no documented rate limit; be polite.
        rl = rate_limiter or RateLimiter(rate_per_sec=2.0, burst=5)
        super().__init__(
            name=self.name,
            rate_limiter=rl,
            circuit_breaker=circuit_breaker,
            cache=cache,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
        )
        self._ticker_factory = ticker_factory if ticker_factory is not None else yf.Ticker

    # === PriceProvider ===

    async def get_latest_quote(self, symbol: str) -> Quote:
        return await self._fetch_cached(
            key=f"yf:quote:{symbol.upper()}",
            ttl_seconds=get_settings().cache_ttl_quotes,
            fetch=lambda: asyncio.to_thread(self._fetch_quote_sync, symbol),
        )

    async def get_ohlc(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str = "1Day",
    ) -> list[OHLCBar]:
        return await self._fetch_cached(
            key=f"yf:ohlc:{symbol.upper()}:{start.date()}:{end.date()}:{timeframe}",
            ttl_seconds=300,
            fetch=lambda: asyncio.to_thread(self._fetch_ohlc_sync, symbol, start, end, timeframe),
        )

    # === OptionsProvider ===

    async def get_expirations(self, symbol: str) -> list[date]:
        return await self._fetch_cached(
            key=f"yf:expirations:{symbol.upper()}",
            ttl_seconds=3600,
            fetch=lambda: asyncio.to_thread(self._fetch_expirations_sync, symbol),
        )

    async def get_chain(self, symbol: str, expiration: date) -> list[OptionContract]:
        return await self._fetch_cached(
            key=f"yf:chain:{symbol.upper()}:{expiration}",
            ttl_seconds=60,
            fetch=lambda: asyncio.to_thread(self._fetch_chain_sync, symbol, expiration),
        )

    async def get_contract_quote(self, occ_symbol: str) -> OptionContract:
        underlying, expiration, type_letter, strike = _parse_occ(occ_symbol)
        chain = await self.get_chain(underlying, expiration)
        target_type: OptionType = "call" if type_letter == "C" else "put"
        for c in chain:
            if c.strike == strike and c.option_type == target_type:
                return c
        raise ProviderUnavailable(
            reason=ProviderUnavailableReason.DATA_MISSING,
            message=f"yfinance: contract {occ_symbol} not found in chain",
            provider=self.name,
            retryable=False,
        )

    # === Sync internals (called via asyncio.to_thread) ===

    def _fetch_quote_sync(self, symbol: str) -> Quote:
        t = self._ticker_factory(symbol)
        try:
            hist = t.history(period="1d", auto_adjust=False)
        except Exception as e:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                message=f"yfinance.history failed for {symbol}: {e}",
                provider=self.name,
            ) from e
        if hist is None or hist.empty:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.DATA_MISSING,
                message=f"yfinance returned no quote for {symbol}",
                provider=self.name,
            )
        last_row = hist.iloc[-1]
        idx = hist.index[-1]
        ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else datetime.now(UTC)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return Quote(
            symbol=symbol.upper(),
            last=Decimal(str(last_row["Close"])),
            bid=None,
            ask=None,
            timestamp=ts,
        )

    def _fetch_ohlc_sync(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str,
    ) -> list[OHLCBar]:
        interval = _TIMEFRAME_TO_YF.get(timeframe, "1d")
        t = self._ticker_factory(symbol)
        try:
            df = t.history(
                start=start.date().isoformat(),
                end=end.date().isoformat(),
                interval=interval,
                auto_adjust=False,
            )
        except Exception as e:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                message=f"yfinance.history failed for {symbol}: {e}",
                provider=self.name,
            ) from e
        if df is None or df.empty:
            return []
        out: list[OHLCBar] = []
        for ts, row in df.iterrows():
            dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            out.append(
                OHLCBar(
                    symbol=symbol.upper(),
                    timestamp=dt,
                    open=Decimal(str(row["Open"])),
                    high=Decimal(str(row["High"])),
                    low=Decimal(str(row["Low"])),
                    close=Decimal(str(row["Close"])),
                    volume=int(row["Volume"]),
                )
            )
        return out

    def _fetch_expirations_sync(self, symbol: str) -> list[date]:
        t = self._ticker_factory(symbol)
        try:
            exps = t.options
        except Exception as e:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                message=f"yfinance.options failed for {symbol}: {e}",
                provider=self.name,
            ) from e
        if not exps:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.DATA_MISSING,
                message=f"yfinance returned no expirations for {symbol}",
                provider=self.name,
            )
        return [date.fromisoformat(e) for e in exps]

    def _fetch_chain_sync(self, symbol: str, expiration: date) -> list[OptionContract]:
        t = self._ticker_factory(symbol)
        try:
            chain = t.option_chain(expiration.isoformat())
        except Exception as e:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                message=f"yfinance.option_chain failed for {symbol} {expiration}: {e}",
                provider=self.name,
            ) from e

        out: list[OptionContract] = []
        iterations: tuple[tuple[Any, OptionType], tuple[Any, OptionType]] = (
            (chain.calls, "call"),
            (chain.puts, "put"),
        )
        for df, opt_type in iterations:
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                strike = Decimal(str(row["strike"]))
                bid = (
                    Decimal(str(row["bid"]))
                    if "bid" in row and not _isnan(row.get("bid"))
                    else None
                )
                ask = (
                    Decimal(str(row["ask"]))
                    if "ask" in row and not _isnan(row.get("ask"))
                    else None
                )
                last_px = (
                    Decimal(str(row["lastPrice"]))
                    if "lastPrice" in row and not _isnan(row.get("lastPrice"))
                    else None
                )
                volume = (
                    int(row["volume"])
                    if "volume" in row and not _isnan(row.get("volume"))
                    else None
                )
                oi = (
                    int(row["openInterest"])
                    if "openInterest" in row and not _isnan(row.get("openInterest"))
                    else None
                )
                iv = (
                    float(row["impliedVolatility"])
                    if "impliedVolatility" in row and not _isnan(row.get("impliedVolatility"))
                    else None
                )
                out.append(
                    OptionContract(
                        symbol=symbol.upper(),
                        occ_symbol=_occ_symbol(symbol, expiration, opt_type, strike),
                        expiration=expiration,
                        strike=strike,
                        option_type=opt_type,
                        bid=bid,
                        ask=ask,
                        last=last_px,
                        volume=volume,
                        open_interest=oi,
                        implied_volatility=iv,
                    )
                )
        return out
