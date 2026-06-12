"""Finnhub provider — analyst ratings, earnings calendar, earnings surprises (DESIGN.md §5).

Free tier: 60 req/min. Endpoint: https://finnhub.io/api/v1/

Exposes provider-specific methods rather than fitting an existing Protocol;
the Phase 4 tools call these methods directly. The same resilience layer
(cache + rate-limit + circuit-breaker) is applied via BaseProvider.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import get_settings
from app.data.base import BaseProvider
from app.data.cache import Cache
from app.data.circuit_breaker import CircuitBreaker
from app.data.rate_limit import RateLimiter
from app.data.types import (
    AnalystRating,
    EarningsEvent,
    EarningsHour,
    EarningsSurprise,
    ProviderUnavailable,
    ProviderUnavailableReason,
    Quote,
)

_BASE = "https://finnhub.io/api/v1"

_HOUR_MAP: dict[str, EarningsHour] = {
    "bmo": "bmo",
    "amc": "amc",
    "dmh": "dmh",
}


class FinnhubProvider(BaseProvider):
    """Analyst ratings + earnings (calendar + surprises) + simple quote."""

    name = "finnhub"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        rate_limiter: RateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        cache: Cache | None = None,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
    ) -> None:
        settings = get_settings()
        resolved = api_key if api_key is not None else settings.finnhub_api_key
        if not resolved:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.AUTH_MISSING,
                message="FINNHUB_API_KEY is not set",
                provider=self.name,
                retryable=False,
            )
        self._api_key = resolved
        self._client = (
            client
            if client is not None
            else httpx.AsyncClient(
                timeout=15.0,
                headers={"Accept": "application/json"},
            )
        )
        rl = rate_limiter or RateLimiter(
            rate_per_sec=settings.finnhub_requests_per_minute / 60.0,
            burst=max(1, int(settings.finnhub_requests_per_minute / 60.0)),
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

    # === Public methods (called by the Phase 4 tool layer) ===

    async def get_quote(self, symbol: str) -> Quote:
        return await self._fetch_cached(
            key=f"finnhub:quote:{symbol.upper()}",
            ttl_seconds=get_settings().cache_ttl_quotes,
            fetch=lambda: self._fetch_quote(symbol),
        )

    async def get_analyst_ratings(self, symbol: str, limit: int = 6) -> list[AnalystRating]:
        return await self._fetch_cached(
            key=f"finnhub:analyst:{symbol.upper()}:{limit}",
            ttl_seconds=3600,
            fetch=lambda: self._fetch_analyst_ratings(symbol, limit),
        )

    async def get_earnings_calendar(
        self,
        symbol: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[EarningsEvent]:
        s = (symbol or "").upper() or "ALL"
        f = (from_date or date.today()).isoformat()
        t = (to_date or date.today().replace(month=12, day=31)).isoformat()
        return await self._fetch_cached(
            key=f"finnhub:earnings_cal:{s}:{f}:{t}",
            ttl_seconds=3600,
            fetch=lambda: self._fetch_earnings_calendar(symbol, from_date, to_date),
        )

    async def get_earnings_surprises(self, symbol: str, limit: int = 8) -> list[EarningsSurprise]:
        return await self._fetch_cached(
            key=f"finnhub:surprises:{symbol.upper()}:{limit}",
            ttl_seconds=3600,
            fetch=lambda: self._fetch_earnings_surprises(symbol, limit),
        )

    # === HTTP plumbing ===

    def _raise_for_http(self, resp: httpx.Response, what: str) -> None:
        if resp.status_code == 429:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.RATE_LIMITED,
                message=f"Finnhub {what} returned 429",
                provider=self.name,
            )
        if resp.status_code in (401, 403):
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.AUTH_MISSING,
                message=f"Finnhub {what} returned {resp.status_code} (auth)",
                provider=self.name,
                retryable=False,
            )
        if resp.status_code != 200:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                message=f"Finnhub {what} returned HTTP {resp.status_code}",
                provider=self.name,
            )

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        full = {**params, "token": self._api_key}
        resp = await self._client.get(f"{_BASE}{path}", params=full)
        self._raise_for_http(resp, path)
        return resp.json()

    # === Fetchers ===

    async def _fetch_quote(self, symbol: str) -> Quote:
        data = await self._get("/quote", {"symbol": symbol.upper()})
        if not data or data.get("c") in (None, 0):
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.DATA_MISSING,
                message=f"Finnhub: no quote for {symbol}",
                provider=self.name,
            )
        ts = data.get("t")
        timestamp = (
            datetime.fromtimestamp(ts, tz=UTC)
            if isinstance(ts, int | float) and ts > 0
            else datetime.now(UTC)
        )
        return Quote(
            symbol=symbol.upper(),
            last=Decimal(str(data["c"])),
            bid=None,
            ask=None,
            timestamp=timestamp,
        )

    async def _fetch_analyst_ratings(self, symbol: str, limit: int) -> list[AnalystRating]:
        data = await self._get("/stock/recommendation", {"symbol": symbol.upper()})
        if not isinstance(data, list):
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                message="Finnhub: malformed recommendation response",
                provider=self.name,
            )
        out: list[AnalystRating] = []
        for entry in data[:limit]:
            with contextlib.suppress(ValueError, TypeError, KeyError):
                out.append(
                    AnalystRating(
                        symbol=entry.get("symbol", symbol).upper(),
                        period=date.fromisoformat(entry["period"]),
                        strong_buy=int(entry.get("strongBuy") or 0),
                        buy=int(entry.get("buy") or 0),
                        hold=int(entry.get("hold") or 0),
                        sell=int(entry.get("sell") or 0),
                        strong_sell=int(entry.get("strongSell") or 0),
                    )
                )
        return out

    async def _fetch_earnings_calendar(
        self,
        symbol: str | None,
        from_date: date | None,
        to_date: date | None,
    ) -> list[EarningsEvent]:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol.upper()
        if from_date:
            params["from"] = from_date.isoformat()
        if to_date:
            params["to"] = to_date.isoformat()
        data = await self._get("/calendar/earnings", params)
        events = data.get("earningsCalendar") or []
        out: list[EarningsEvent] = []
        for e in events:
            with contextlib.suppress(ValueError, TypeError, KeyError):
                hour_raw = (e.get("hour") or "").lower()
                hour = _HOUR_MAP.get(hour_raw, "unknown")
                out.append(
                    EarningsEvent(
                        symbol=str(e.get("symbol", "")).upper(),
                        event_date=date.fromisoformat(e["date"]),
                        eps_estimate=e.get("epsEstimate"),
                        eps_actual=e.get("epsActual"),
                        revenue_estimate=e.get("revenueEstimate"),
                        revenue_actual=e.get("revenueActual"),
                        hour=hour,
                        quarter=e.get("quarter"),
                        year=e.get("year"),
                    )
                )
        return out

    async def _fetch_earnings_surprises(self, symbol: str, limit: int) -> list[EarningsSurprise]:
        data = await self._get("/stock/earnings", {"symbol": symbol.upper()})
        if not isinstance(data, list):
            return []
        out: list[EarningsSurprise] = []
        for e in data[:limit]:
            with contextlib.suppress(ValueError, TypeError, KeyError):
                out.append(
                    EarningsSurprise(
                        symbol=str(e.get("symbol", symbol)).upper(),
                        period=date.fromisoformat(e["period"]),
                        eps_actual=e.get("actual"),
                        eps_estimate=e.get("estimate"),
                        surprise=e.get("surprise"),
                        surprise_pct=e.get("surprisePercent"),
                        quarter=e.get("quarter"),
                        year=e.get("year"),
                    )
                )
        return out
