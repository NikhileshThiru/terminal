"""/tickers — symbol-centric metadata + news.

The TickerInfoPane on the Dashboard needs company/ETF basics (name,
sector, market cap, business summary) and a tight symbol-filtered news
feed. Both live here so the frontend hits one endpoint per symbol change.

Data sources:
  - yfinance `Ticker.info` — company profile, ETF strategy, market cap.
    Unofficial scrape (DESIGN.md §5); cached for an hour to avoid rate
    issues and to absorb the occasional breakage.
  - CatalystEvent table — next earnings date, pre-positioned theses.
  - TriageDecisionRow table — symbol-filtered news + the model's
    pass/drop verdicts (more useful than raw news because the verdict
    is the editorial layer).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import lru_cache
from typing import Any

import yfinance as yf
from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import desc, select

from app.core.logging import get_logger
from app.discovery.models import CatalystEvent, TriageDecisionRow
from app.eval.persistence import get_session_factory

_log = get_logger(__name__)

router = APIRouter(prefix="/tickers", tags=["tickers"])


class TickerInfo(BaseModel):
    symbol: str
    long_name: str | None = None
    short_name: str | None = None
    sector: str | None = None
    industry: str | None = None
    quote_type: str | None = None  # "EQUITY" | "ETF" | "INDEX" | ...
    market_cap_usd: int | None = None
    employees: int | None = None
    long_business_summary: str | None = None
    website: str | None = None
    fifty_two_week_high: Decimal | None = None
    fifty_two_week_low: Decimal | None = None
    next_earnings_date: date | None = None
    next_earnings_state: str | None = None  # scheduled / triggered / expired
    next_earnings_thesis_id: int | None = None


class TickerNewsRow(BaseModel):
    event_id: str
    symbol: str | None
    headline: str
    body_excerpt: str | None
    url: str | None
    source: str
    kind: str
    passed: bool
    reason: str
    confidence: float
    decided_at: datetime
    published_at: datetime


class TickerNewsResponse(BaseModel):
    symbol: str
    rows: list[TickerNewsRow]


# In-process cache. yfinance is slow + unofficial; one-hour TTL is fine
# because company-profile data doesn't change minute-to-minute. Keyed by
# (symbol,) so two requests for the same ticker within the hour share
# the cached profile.
class _CachedProfile(BaseModel):
    payload: dict[str, Any]
    fetched_at: datetime


_PROFILE_CACHE: dict[str, _CachedProfile] = {}
_PROFILE_TTL_SECONDS = 3600


@lru_cache(maxsize=1)
def _yf_sync_executor() -> Any:
    """yfinance is a sync library; we hop to a thread to avoid blocking
    the event loop on each call. lru_cache makes this a process singleton."""
    import concurrent.futures

    return concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="yf-info")


async def _fetch_profile(symbol: str) -> dict[str, Any]:
    cached = _PROFILE_CACHE.get(symbol)
    if cached is not None:
        age = (datetime.now(UTC) - cached.fetched_at).total_seconds()
        if age < _PROFILE_TTL_SECONDS:
            return cached.payload

    def _blocking_fetch() -> dict[str, Any]:
        ticker = yf.Ticker(symbol)
        # `.info` is the canonical sync profile dict; falls back to .fast_info
        # if .info raises (some symbols hit Yahoo's anti-scrape pages).
        try:
            return dict(ticker.info or {})
        except Exception as e:  # pragma: no cover — depends on Yahoo's mood
            _log.warning("yfinance_info_failed", symbol=symbol, error=str(e))
            try:
                fast = ticker.fast_info
                # fast_info is a dict-like with limited fields.
                return {k: fast.get(k) for k in dir(fast) if not k.startswith("_")}
            except Exception:
                return {}

    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(_yf_sync_executor(), _blocking_fetch)
    _PROFILE_CACHE[symbol] = _CachedProfile(payload=payload, fetched_at=datetime.now(UTC))
    return payload


async def _next_earnings_row(symbol: str) -> CatalystEvent | None:
    factory = get_session_factory()
    async with factory() as session:
        stmt = (
            select(CatalystEvent)
            .where(
                CatalystEvent.symbol == symbol,
                CatalystEvent.event_type == "earnings",
                CatalystEvent.event_date >= date.today(),
            )
            .order_by(CatalystEvent.event_date.asc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()


def _coerce_decimal(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


@router.get("/{symbol}/info", response_model=TickerInfo)
async def get_ticker_info(symbol: str) -> TickerInfo:
    sym = symbol.upper()
    profile = await _fetch_profile(sym)
    next_earn = await _next_earnings_row(sym)
    return TickerInfo(
        symbol=sym,
        long_name=profile.get("longName") or profile.get("shortName"),
        short_name=profile.get("shortName"),
        sector=profile.get("sector"),
        industry=profile.get("industry") or profile.get("category"),
        quote_type=profile.get("quoteType"),
        market_cap_usd=(
            int(profile["marketCap"]) if profile.get("marketCap") is not None else None
        ),
        employees=(
            int(profile["fullTimeEmployees"])
            if profile.get("fullTimeEmployees") is not None
            else None
        ),
        long_business_summary=profile.get("longBusinessSummary"),
        website=profile.get("website"),
        fifty_two_week_high=_coerce_decimal(profile.get("fiftyTwoWeekHigh")),
        fifty_two_week_low=_coerce_decimal(profile.get("fiftyTwoWeekLow")),
        next_earnings_date=next_earn.event_date if next_earn else None,
        next_earnings_state=next_earn.state if next_earn else None,
        next_earnings_thesis_id=next_earn.thesis_id if next_earn else None,
    )


@router.get("/{symbol}/news", response_model=TickerNewsResponse)
async def get_ticker_news(
    symbol: str,
    limit: int = Query(default=15, ge=1, le=50),
) -> TickerNewsResponse:
    """Symbol-filtered triage history. Includes both PASS and DROP rows
    so the user can see what the system saw + what it thought, even
    when the triage gate dropped most of it."""
    sym = symbol.upper()
    factory = get_session_factory()
    async with factory() as session:
        stmt = (
            select(TriageDecisionRow)
            .where(TriageDecisionRow.symbol == sym)
            .order_by(desc(TriageDecisionRow.decided_at))
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
    # Empty list is the honest answer — autonomous worker may simply not
    # have seen news for this symbol yet. Don't 404.
    return TickerNewsResponse(
        symbol=sym,
        rows=[
            TickerNewsRow(
                event_id=r.event_id,
                symbol=r.symbol,
                headline=r.headline,
                body_excerpt=r.body_excerpt,
                url=r.url,
                source=r.source,
                kind=r.kind,
                passed=r.passed,
                reason=r.reason,
                confidence=r.confidence,
                decided_at=r.decided_at,
                published_at=r.published_at,
            )
            for r in rows
        ],
    )
