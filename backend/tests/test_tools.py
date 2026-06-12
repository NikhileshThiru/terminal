"""Tool registry tests with mocked providers."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.agent.tools import ToolRegistry, _assert_async_handlers
from app.data.types import (
    AnalystRating,
    EarningsEvent,
    EarningsSurprise,
    Filing,
    FilingType,
    OHLCBar,
    OptionContract,
    ProviderUnavailable,
    ProviderUnavailableReason,
    Quote,
)

# === Mock providers (just enough to drive the tools) ===


class FakePriceProvider:
    name = "fake-price"

    def __init__(self, quote: Quote | None = None, bars: list[OHLCBar] | None = None) -> None:
        self._quote = quote
        self._bars = bars or []

    async def get_latest_quote(self, symbol: str) -> Quote:
        if self._quote is None:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.DATA_MISSING,
                message="no quote",
                provider=self.name,
            )
        return self._quote

    async def get_ohlc(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "1Day"
    ) -> list[OHLCBar]:
        return self._bars


class FakeOptionsProvider:
    name = "fake-options"

    def __init__(
        self,
        expirations: list[date] | None = None,
        chain: list[OptionContract] | None = None,
    ) -> None:
        self._expirations = expirations or []
        self._chain = chain or []

    async def get_expirations(self, symbol: str) -> list[date]:
        return self._expirations

    async def get_chain(self, symbol: str, expiration: date) -> list[OptionContract]:
        return self._chain

    async def get_contract_quote(self, occ_symbol: str) -> OptionContract:
        raise NotImplementedError


class FakeFilingsProvider:
    name = "fake-filings"

    def __init__(self, filings: list[Filing] | None = None) -> None:
        self._filings = filings or []

    async def get_recent_filings(
        self, symbol: str, filing_types: list[FilingType] | None = None, limit: int = 20
    ) -> list[Filing]:
        out = self._filings
        if filing_types:
            wanted = set(filing_types)
            out = [f for f in out if f.filing_type in wanted]
        return out[:limit]

    async def get_filing_text(self, accession: str) -> str:
        raise NotImplementedError


class FakeFinnhub:
    name = "fake-finnhub"

    def __init__(
        self,
        ratings: list[AnalystRating] | None = None,
        calendar: list[EarningsEvent] | None = None,
        surprises: list[EarningsSurprise] | None = None,
    ) -> None:
        self._ratings = ratings or []
        self._calendar = calendar or []
        self._surprises = surprises or []

    async def get_analyst_ratings(self, symbol: str, limit: int = 6) -> list[AnalystRating]:
        return self._ratings[:limit]

    async def get_earnings_calendar(
        self, symbol: str | None = None, from_date: date | None = None, to_date: date | None = None
    ) -> list[EarningsEvent]:
        return self._calendar

    async def get_earnings_surprises(self, symbol: str, limit: int = 8) -> list[EarningsSurprise]:
        return self._surprises[:limit]


def _make_registry(**overrides: Any) -> ToolRegistry:
    defaults = {
        "price_provider": FakePriceProvider(),
        "options_provider": FakeOptionsProvider(),
        "filings_provider": FakeFilingsProvider(),
        "finnhub": FakeFinnhub(),
    }
    defaults.update(overrides)
    return ToolRegistry(**defaults)


# === Registry-level ===


def test_registers_six_tools() -> None:
    reg = _make_registry()
    names = sorted(t.name for t in reg.tools())
    assert names == [
        "get_analyst_ratings",
        "get_earnings_context",
        "get_ohlc",
        "get_options_chain",
        "get_quote",
        "get_recent_filings",
    ]


def test_all_handlers_are_async() -> None:
    _assert_async_handlers(_make_registry())


def test_agent_defs_are_provider_agnostic() -> None:
    reg = _make_registry()
    defs = reg.agent_defs()
    assert len(defs) == 6
    names = {d.name for d in defs}
    assert "get_quote" in names
    # Each def carries a JSON-schema-style parameters dict.
    quote = next(d for d in defs if d.name == "get_quote")
    assert quote.parameters["type"] == "object"
    assert "symbol" in quote.parameters["properties"]


@pytest.mark.asyncio
async def test_unknown_tool_returns_typed_failure() -> None:
    reg = _make_registry()
    r = await reg.execute("not_a_tool", {})
    assert r.success is False
    assert "unknown tool" in (r.error or "")


@pytest.mark.asyncio
async def test_invalid_arguments_returns_typed_failure() -> None:
    reg = _make_registry()
    r = await reg.execute("get_ohlc", {"days_back": 9999})  # exceeds le=365
    assert r.success is False
    assert "invalid arguments" in (r.error or "")


# === Per-tool happy/sad paths ===


@pytest.mark.asyncio
async def test_get_quote_happy() -> None:
    q = Quote(symbol="AAPL", last=Decimal("312.06"), timestamp=datetime.now(UTC))
    reg = _make_registry(price_provider=FakePriceProvider(quote=q))
    r = await reg.execute("get_quote", {"symbol": "AAPL"})
    assert r.success is True
    assert r.data["symbol"] == "AAPL"
    assert r.data["last"] == "312.06"
    assert r.provider == "fake-price"


@pytest.mark.asyncio
async def test_get_quote_provider_unavailable_surfaces_typed_error() -> None:
    reg = _make_registry(price_provider=FakePriceProvider(quote=None))
    r = await reg.execute("get_quote", {"symbol": "AAPL"})
    assert r.success is False
    assert "data_missing" in (r.error or "").lower()


@pytest.mark.asyncio
async def test_get_ohlc_happy() -> None:
    bars = [
        OHLCBar(
            symbol="AAPL",
            timestamp=datetime(2026, 5, 20, tzinfo=UTC),
            open=Decimal("298.18"),
            high=Decimal("302.8"),
            low=Decimal("298.08"),
            close=Decimal("302.25"),
            volume=38392499,
        ),
    ]
    reg = _make_registry(price_provider=FakePriceProvider(bars=bars))
    r = await reg.execute("get_ohlc", {"symbol": "AAPL", "days_back": 5})
    assert r.success is True
    assert len(r.data["bars"]) == 1
    assert r.data["bars"][0]["close"] == "302.25"


@pytest.mark.asyncio
async def test_get_options_chain_picks_nearest_expiration() -> None:
    past = date.today() - timedelta(days=5)
    future_near = date.today() + timedelta(days=3)
    future_far = date.today() + timedelta(days=30)
    chain = [
        OptionContract(
            symbol="AAPL",
            occ_symbol=f"AAPL{future_near:%y%m%d}C00150000",
            expiration=future_near,
            strike=Decimal("150"),
            option_type="call",
        )
    ]
    reg = _make_registry(
        options_provider=FakeOptionsProvider(
            expirations=[past, future_near, future_far], chain=chain
        )
    )
    r = await reg.execute("get_options_chain", {"symbol": "AAPL"})
    assert r.success is True
    assert r.data["expiration"] == future_near.isoformat()
    assert len(r.data["contracts"]) == 1


@pytest.mark.asyncio
async def test_get_options_chain_honors_explicit_expiration() -> None:
    target = date.today() + timedelta(days=10)
    chain = [
        OptionContract(
            symbol="AAPL",
            occ_symbol=f"AAPL{target:%y%m%d}P00200000",
            expiration=target,
            strike=Decimal("200"),
            option_type="put",
        )
    ]
    reg = _make_registry(options_provider=FakeOptionsProvider(chain=chain))
    r = await reg.execute(
        "get_options_chain",
        {"symbol": "AAPL", "expiration_date": target.isoformat()},
    )
    assert r.success is True
    assert r.data["expiration"] == target.isoformat()


@pytest.mark.asyncio
async def test_get_recent_filings_filters_by_type() -> None:
    filings = [
        Filing(
            accession="0001-1",
            cik="320193",
            symbol="AAPL",
            filing_type=FilingType.F_8K,
            filed_at=datetime.now(UTC),
            url="https://example.com/1",
            title="8-K",
        ),
        Filing(
            accession="0001-2",
            cik="320193",
            symbol="AAPL",
            filing_type=FilingType.F_10Q,
            filed_at=datetime.now(UTC),
            url="https://example.com/2",
            title="10-Q",
        ),
    ]
    reg = _make_registry(filings_provider=FakeFilingsProvider(filings=filings))
    r = await reg.execute(
        "get_recent_filings", {"symbol": "AAPL", "filing_types": ["8-K"], "limit": 5}
    )
    assert r.success is True
    assert len(r.data["filings"]) == 1
    assert r.data["filings"][0]["filing_type"] == "8-K"


@pytest.mark.asyncio
async def test_get_analyst_ratings_returns_typed_snapshots() -> None:
    ratings = [
        AnalystRating(
            symbol="AAPL",
            period=date(2026, 5, 1),
            strong_buy=12,
            buy=24,
            hold=8,
            sell=1,
            strong_sell=0,
        ),
    ]
    reg = _make_registry(finnhub=FakeFinnhub(ratings=ratings))
    r = await reg.execute("get_analyst_ratings", {"symbol": "AAPL"})
    assert r.success is True
    assert len(r.data["snapshots"]) == 1
    assert r.data["snapshots"][0]["buy"] == 24


@pytest.mark.asyncio
async def test_get_earnings_context_combines_calendar_and_surprises() -> None:
    calendar = [
        EarningsEvent(symbol="AAPL", event_date=date(2026, 7, 25), eps_estimate=1.5, hour="amc")
    ]
    surprises = [
        EarningsSurprise(
            symbol="AAPL",
            period=date(2026, 3, 31),
            eps_actual=1.65,
            eps_estimate=1.5,
            surprise=0.15,
            surprise_pct=10.0,
        )
    ]
    reg = _make_registry(finnhub=FakeFinnhub(calendar=calendar, surprises=surprises))
    r = await reg.execute("get_earnings_context", {"symbol": "AAPL"})
    assert r.success is True
    assert len(r.data["upcoming"]) == 1
    assert r.data["upcoming"][0]["event_date"] == "2026-07-25"
    assert len(r.data["recent_surprises"]) == 1
    assert r.data["recent_surprises"][0]["surprise_pct"] == 10.0
