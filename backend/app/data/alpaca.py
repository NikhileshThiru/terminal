"""Alpaca Markets provider — PriceProvider + OptionsProvider (DESIGN.md §5).

Primary equities source (real-time IEX) and primary options source (Indicative
Pricing Feed, paper account). Uses both `paper-api.alpaca.markets` (trading +
contract metadata) and `data.alpaca.markets` (quotes, bars, snapshots) with
the same auth headers.

Endpoints used:
- `data.alpaca.markets/v2/stocks/{symbol}/quotes/latest` — latest quote
- `data.alpaca.markets/v2/stocks/{symbol}/bars` — OHLC bars
- `paper-api.alpaca.markets/v2/options/contracts` — contract metadata (used
  to enumerate expirations)
- `data.alpaca.markets/v1beta1/options/snapshots/{underlying}` — chain
  quotes; OCC symbol → latestQuote/latestTrade

Free-tier limits (DESIGN.md §5):
- 200 requests/min on historical
- Real-time IEX only (~2% of consolidated volume)
- Options data from the Indicative Pricing Feed (not OPRA)
"""

from __future__ import annotations

import contextlib
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx

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

_TRADING_BASE = "https://paper-api.alpaca.markets"
_DATA_BASE = "https://data.alpaca.markets"

_TIMEFRAME_TO_ALPACA = {
    "1Min": "1Min",
    "5Min": "5Min",
    "15Min": "15Min",
    "30Min": "30Min",
    "1Hour": "1Hour",
    "1Day": "1Day",
    "1Week": "1Week",
    "1Month": "1Month",
}


def _parse_occ_meta(occ_symbol: str) -> tuple[date, OptionType, Decimal]:
    """Pull (expiration, type, strike) from an OCC option symbol."""
    if len(occ_symbol) < 15:
        raise ValueError(f"OCC symbol too short: {occ_symbol!r}")
    strike_str = occ_symbol[-8:]
    type_letter = occ_symbol[-9]
    date_str = occ_symbol[-15:-9]
    if type_letter not in ("C", "P"):
        raise ValueError(f"Invalid option type in OCC: {occ_symbol!r}")
    year = 2000 + int(date_str[:2])
    month = int(date_str[2:4])
    day = int(date_str[4:6])
    expiration = date(year, month, day)
    opt_type: OptionType = "call" if type_letter == "C" else "put"
    strike = Decimal(int(strike_str)) / Decimal(1000)
    return expiration, opt_type, strike


def _parse_alpaca_ts(ts: str) -> datetime:
    """Alpaca timestamps end in 'Z'; convert to a UTC-aware datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class AlpacaProvider(BaseProvider):
    """PriceProvider + OptionsProvider against Alpaca paper-trading + market data."""

    name = "alpaca"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        client: httpx.AsyncClient | None = None,
        rate_limiter: RateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        cache: Cache | None = None,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
    ) -> None:
        settings = get_settings()
        key = api_key if api_key is not None else settings.alpaca_api_key
        secret = api_secret if api_secret is not None else settings.alpaca_api_secret
        if not key or not secret:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.AUTH_MISSING,
                message="ALPACA_API_KEY and ALPACA_API_SECRET must be set in env",
                provider=self.name,
                retryable=False,
            )
        self._client = (
            client
            if client is not None
            else httpx.AsyncClient(
                timeout=15.0,
                headers={
                    "APCA-API-KEY-ID": key,
                    "APCA-API-SECRET-KEY": secret,
                    "Accept": "application/json",
                },
            )
        )
        rl = rate_limiter or RateLimiter(
            rate_per_sec=settings.alpaca_requests_per_minute / 60.0,
            burst=max(1, int(settings.alpaca_requests_per_minute / 60.0)),
        )
        super().__init__(
            name=self.name,
            rate_limiter=rl,
            circuit_breaker=circuit_breaker,
            cache=cache,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # === PriceProvider ===

    async def get_latest_quote(self, symbol: str) -> Quote:
        return await self._fetch_cached(
            key=f"alpaca:quote:{symbol.upper()}",
            ttl_seconds=get_settings().cache_ttl_quotes,
            fetch=lambda: self._fetch_latest_quote(symbol),
        )

    async def get_ohlc(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str = "1Day",
    ) -> list[OHLCBar]:
        return await self._fetch_cached(
            key=f"alpaca:ohlc:{symbol.upper()}:{start.date()}:{end.date()}:{timeframe}",
            ttl_seconds=300,
            fetch=lambda: self._fetch_ohlc(symbol, start, end, timeframe),
        )

    # === OptionsProvider ===

    async def get_expirations(self, symbol: str) -> list[date]:
        return await self._fetch_cached(
            key=f"alpaca:expirations:{symbol.upper()}",
            ttl_seconds=3600,
            fetch=lambda: self._fetch_expirations(symbol),
        )

    async def get_chain(self, symbol: str, expiration: date) -> list[OptionContract]:
        return await self._fetch_cached(
            key=f"alpaca:chain:{symbol.upper()}:{expiration}",
            ttl_seconds=60,
            fetch=lambda: self._fetch_chain(symbol, expiration),
        )

    async def get_contract_quote(self, occ_symbol: str) -> OptionContract:
        if len(occ_symbol) < 15:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.DATA_MISSING,
                message=f"Invalid OCC symbol: {occ_symbol!r}",
                provider=self.name,
                retryable=False,
            )
        underlying = occ_symbol[:-15]
        expiration, _, _ = _parse_occ_meta(occ_symbol)
        chain = await self.get_chain(underlying, expiration)
        for c in chain:
            if c.occ_symbol == occ_symbol:
                return c
        raise ProviderUnavailable(
            reason=ProviderUnavailableReason.DATA_MISSING,
            message=f"Alpaca: contract {occ_symbol} not found in chain",
            provider=self.name,
            retryable=False,
        )

    # === Internals ===

    def _raise_for_http(self, resp: httpx.Response, what: str) -> None:
        if resp.status_code == 429:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.RATE_LIMITED,
                message=f"Alpaca {what} returned 429",
                provider=self.name,
            )
        if resp.status_code in (401, 403):
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.AUTH_MISSING,
                message=f"Alpaca {what} returned {resp.status_code} (auth)",
                provider=self.name,
                retryable=False,
            )
        if resp.status_code != 200:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                message=f"Alpaca {what} returned HTTP {resp.status_code}",
                provider=self.name,
            )

    async def _fetch_latest_quote(self, symbol: str) -> Quote:
        url = f"{_DATA_BASE}/v2/stocks/{symbol.upper()}/quotes/latest"
        resp = await self._client.get(url)
        self._raise_for_http(resp, "quote")
        data = resp.json()
        q = data.get("quote")
        if not q:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.DATA_MISSING,
                message=f"Alpaca: no quote for {symbol}",
                provider=self.name,
            )
        return Quote(
            symbol=symbol.upper(),
            bid=Decimal(str(q["bp"])) if q.get("bp") is not None else None,
            ask=Decimal(str(q["ap"])) if q.get("ap") is not None else None,
            last=None,  # latest-quote doesn't include last; bars endpoint has it.
            timestamp=_parse_alpaca_ts(q["t"]),
        )

    async def _fetch_ohlc(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str,
    ) -> list[OHLCBar]:
        tf = _TIMEFRAME_TO_ALPACA.get(timeframe, "1Day")
        params: dict[str, str | int] = {
            "timeframe": tf,
            "start": start.date().isoformat(),
            "end": end.date().isoformat(),
            "feed": "iex",  # Free Basic plan: IEX feed only (DESIGN.md §5).
            "adjustment": "raw",
            "limit": 10000,
        }
        url = f"{_DATA_BASE}/v2/stocks/{symbol.upper()}/bars"
        resp = await self._client.get(url, params=params)
        self._raise_for_http(resp, "bars")
        data = resp.json()
        bars = data.get("bars", []) or []
        out: list[OHLCBar] = []
        for b in bars:
            out.append(
                OHLCBar(
                    symbol=symbol.upper(),
                    timestamp=_parse_alpaca_ts(b["t"]),
                    open=Decimal(str(b["o"])),
                    high=Decimal(str(b["h"])),
                    low=Decimal(str(b["l"])),
                    close=Decimal(str(b["c"])),
                    volume=int(b["v"]),
                )
            )
        return out

    async def _fetch_expirations(self, symbol: str) -> list[date]:
        """Walk the contracts endpoint and collect unique expiration dates."""
        url = f"{_TRADING_BASE}/v2/options/contracts"
        params: dict[str, Any] = {
            "underlying_symbols": symbol.upper(),
            "status": "active",
            "limit": 10000,
        }
        seen: set[date] = set()
        page_token: str | None = None
        while True:
            if page_token:
                params["page_token"] = page_token
            resp = await self._client.get(url, params=params)
            self._raise_for_http(resp, "contracts")
            data = resp.json()
            contracts = data.get("option_contracts", []) or []
            for c in contracts:
                d = c.get("expiration_date")
                if d:
                    with contextlib.suppress(ValueError):
                        seen.add(date.fromisoformat(d))
            page_token = data.get("next_page_token")
            if not page_token:
                break
        if not seen:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.DATA_MISSING,
                message=f"Alpaca: no option contracts for {symbol}",
                provider=self.name,
            )
        return sorted(seen)

    async def _fetch_chain(self, symbol: str, expiration: date) -> list[OptionContract]:
        """Fetch snapshots for the underlying and filter to the requested expiration.

        Snapshots are keyed by OCC symbol; we parse OCC to recover strike/type/
        expiration without a second endpoint hit.
        """
        url = f"{_DATA_BASE}/v1beta1/options/snapshots/{symbol.upper()}"
        out: list[OptionContract] = []
        page_token: str | None = None
        max_pages = 10  # safety net — AAPL has ~1500 active contracts total
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": 1000}
            if page_token:
                params["page_token"] = page_token
            resp = await self._client.get(url, params=params)
            self._raise_for_http(resp, "snapshots")
            data = resp.json()
            snaps = data.get("snapshots", {}) or {}
            for occ, snap in snaps.items():
                try:
                    exp, opt_type, strike = _parse_occ_meta(occ)
                except ValueError:
                    continue
                if exp != expiration:
                    continue
                quote = snap.get("latestQuote") or {}
                trade = snap.get("latestTrade") or {}
                out.append(
                    OptionContract(
                        symbol=symbol.upper(),
                        occ_symbol=occ,
                        expiration=exp,
                        strike=strike,
                        option_type=opt_type,
                        bid=Decimal(str(quote["bp"])) if quote.get("bp") not in (None, 0) else None,
                        ask=Decimal(str(quote["ap"])) if quote.get("ap") not in (None, 0) else None,
                        last=Decimal(str(trade["p"])) if trade.get("p") not in (None, 0) else None,
                        volume=trade.get("s") if isinstance(trade.get("s"), int) else None,
                        open_interest=None,
                        implied_volatility=None,
                    )
                )
            page_token = data.get("next_page_token")
            if not page_token:
                break
        return out
