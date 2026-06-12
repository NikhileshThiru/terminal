"""Typed data primitives passed between providers and callers.

Pydantic everywhere. Providers either return one of these (or a list) on
success, or raise ProviderUnavailable on failure — never a raw exception
that crashes the agent funnel (DESIGN.md §7: graceful degradation).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

OptionType = Literal["call", "put"]


class Quote(BaseModel):
    """Single bid/ask/last quote."""

    symbol: str
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    timestamp: datetime

    def safe_price(self) -> Decimal | None:
        """Best usable price: `last` when positive, else the mid of a
        TWO-SIDED bid/ask. After-hours IEX quotes are routinely one-sided
        (ask=0); averaging a zero in halves the price, so zero/None on
        either side means "no usable price" — callers must skip, not guess.
        (This bug once graded two AAPL theses as -50% misses.)"""
        if self.last is not None and self.last > 0:
            return self.last
        if self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / Decimal(2)
        return None


class OHLCBar(BaseModel):
    """Open-High-Low-Close-Volume bar."""

    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class OptionContract(BaseModel):
    """One option contract on the chain."""

    symbol: str  # underlying
    occ_symbol: str  # full OCC option symbol, e.g. AAPL250117C00150000
    expiration: date
    strike: Decimal
    option_type: OptionType
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    volume: int | None = None
    open_interest: int | None = None
    implied_volatility: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None


class NewsItem(BaseModel):
    """A news article or release."""

    id: str  # source-provided id, used for dedup
    headline: str
    summary: str | None = None
    body: str | None = None
    url: str | None = None
    source: str  # e.g. "benzinga", "edgar", "rss:pr-newswire"
    symbols: list[str] = Field(default_factory=list)
    published_at: datetime


class FilingType(StrEnum):
    F_8K = "8-K"
    F_10K = "10-K"
    F_10Q = "10-Q"
    F_4 = "4"
    F_13F = "13F"
    OTHER = "OTHER"


class Filing(BaseModel):
    """An SEC filing."""

    accession: str
    cik: str
    symbol: str | None = None
    filing_type: FilingType
    filed_at: datetime
    url: str
    title: str | None = None


class AnalystRating(BaseModel):
    """Analyst recommendation snapshot at a point in time (per Finnhub)."""

    symbol: str
    period: date  # snapshot date
    strong_buy: int = 0
    buy: int = 0
    hold: int = 0
    sell: int = 0
    strong_sell: int = 0

    @property
    def total(self) -> int:
        return self.strong_buy + self.buy + self.hold + self.sell + self.strong_sell


EarningsHour = Literal["bmo", "amc", "dmh", "unknown"]


class EarningsEvent(BaseModel):
    """An upcoming or past earnings event."""

    symbol: str
    event_date: date
    eps_estimate: float | None = None
    eps_actual: float | None = None
    revenue_estimate: int | None = None
    revenue_actual: int | None = None
    hour: EarningsHour = "unknown"
    quarter: int | None = None
    year: int | None = None


class EarningsSurprise(BaseModel):
    """A historical earnings surprise (actual vs estimate)."""

    symbol: str
    period: date  # the quarter end date
    eps_actual: float | None = None
    eps_estimate: float | None = None
    surprise: float | None = None
    surprise_pct: float | None = None
    quarter: int | None = None
    year: int | None = None


# === Failure modeling ===


class ProviderUnavailableReason(StrEnum):
    RATE_LIMITED = "rate_limited"
    CIRCUIT_OPEN = "circuit_open"
    UPSTREAM_ERROR = "upstream_error"
    AUTH_MISSING = "auth_missing"
    NOT_IMPLEMENTED = "not_implemented"
    DATA_MISSING = "data_missing"
    TIMEOUT = "timeout"


class ProviderUnavailable(Exception):
    """Raised by providers when they cannot serve a request.

    The agent funnel must catch this and degrade gracefully: log the gap,
    mark the thesis as having partial data, and continue. The funnel must
    NEVER crash because a single provider is down (DESIGN.md §7).
    """

    def __init__(
        self,
        reason: ProviderUnavailableReason,
        message: str,
        provider: str | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.provider = provider
        self.retryable = retryable

    def __repr__(self) -> str:
        return (
            f"ProviderUnavailable(provider={self.provider!r}, "
            f"reason={self.reason!r}, retryable={self.retryable})"
        )
