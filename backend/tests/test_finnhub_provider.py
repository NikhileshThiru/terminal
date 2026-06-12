"""Finnhub provider — respx-mocked tests against real endpoint shapes."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx

from app.data.finnhub import FinnhubProvider
from app.data.types import ProviderUnavailable, ProviderUnavailableReason


def _make_provider() -> FinnhubProvider:
    return FinnhubProvider(api_key="TEST_KEY")


def test_missing_credentials_raises_unavailable() -> None:
    with pytest.raises(ProviderUnavailable) as exc:
        FinnhubProvider(api_key="")
    assert exc.value.reason == ProviderUnavailableReason.AUTH_MISSING


# === Quote ===


@pytest.mark.asyncio
@respx.mock
async def test_get_quote_parses_finnhub_response() -> None:
    respx.get("https://finnhub.io/api/v1/quote").mock(
        return_value=httpx.Response(
            200,
            json={
                "c": 312.06,
                "d": -0.45,
                "dp": -0.144,
                "h": 315,
                "l": 309.53,
                "o": 311.775,
                "pc": 312.51,
                "t": 1780084800,
            },
        )
    )
    p = _make_provider()
    q = await p.get_quote("AAPL")
    assert q.symbol == "AAPL"
    assert q.last == Decimal("312.06")
    assert q.timestamp.tzinfo is not None
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_get_quote_zero_price_treated_as_missing() -> None:
    respx.get("https://finnhub.io/api/v1/quote").mock(
        return_value=httpx.Response(200, json={"c": 0, "t": 0})
    )
    p = _make_provider()
    with pytest.raises(ProviderUnavailable) as exc:
        await p.get_quote("NOTREAL")
    assert exc.value.reason == ProviderUnavailableReason.DATA_MISSING
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_429_propagates_as_rate_limited() -> None:
    respx.get("https://finnhub.io/api/v1/quote").mock(return_value=httpx.Response(429))
    p = _make_provider()
    with pytest.raises(ProviderUnavailable) as exc:
        await p.get_quote("AAPL")
    assert exc.value.reason == ProviderUnavailableReason.RATE_LIMITED
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_401_propagates_as_auth_missing() -> None:
    respx.get("https://finnhub.io/api/v1/quote").mock(return_value=httpx.Response(401))
    p = _make_provider()
    with pytest.raises(ProviderUnavailable) as exc:
        await p.get_quote("AAPL")
    assert exc.value.reason == ProviderUnavailableReason.AUTH_MISSING
    assert exc.value.retryable is False
    await p.aclose()


# === Analyst ratings ===


@pytest.mark.asyncio
@respx.mock
async def test_get_analyst_ratings_parses_array() -> None:
    respx.get("https://finnhub.io/api/v1/stock/recommendation").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "buy": 25,
                    "hold": 8,
                    "period": "2026-05-01",
                    "sell": 1,
                    "strongBuy": 12,
                    "strongSell": 0,
                    "symbol": "AAPL",
                },
                {
                    "buy": 24,
                    "hold": 9,
                    "period": "2026-04-01",
                    "sell": 2,
                    "strongBuy": 11,
                    "strongSell": 0,
                    "symbol": "AAPL",
                },
            ],
        )
    )
    p = _make_provider()
    ratings = await p.get_analyst_ratings("AAPL")
    assert len(ratings) == 2
    assert ratings[0].period == date(2026, 5, 1)
    assert ratings[0].buy == 25
    assert ratings[0].strong_buy == 12
    assert ratings[0].total == 46
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_get_analyst_ratings_limit_truncates() -> None:
    respx.get("https://finnhub.io/api/v1/stock/recommendation").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "period": f"2026-0{i + 1}-01",
                    "buy": i,
                    "hold": 0,
                    "sell": 0,
                    "strongBuy": 0,
                    "strongSell": 0,
                    "symbol": "AAPL",
                }
                for i in range(5)
            ],
        )
    )
    p = _make_provider()
    ratings = await p.get_analyst_ratings("AAPL", limit=3)
    assert len(ratings) == 3
    await p.aclose()


# === Earnings calendar ===


@pytest.mark.asyncio
@respx.mock
async def test_get_earnings_calendar_parses() -> None:
    respx.get("https://finnhub.io/api/v1/calendar/earnings").mock(
        return_value=httpx.Response(
            200,
            json={
                "earningsCalendar": [
                    {
                        "date": "2026-07-25",
                        "epsActual": None,
                        "epsEstimate": 1.50,
                        "hour": "amc",
                        "quarter": 3,
                        "revenueActual": None,
                        "revenueEstimate": 90000000000,
                        "symbol": "AAPL",
                        "year": 2026,
                    },
                    {
                        "date": "2026-08-01",
                        "epsActual": None,
                        "epsEstimate": 2.10,
                        "hour": "bmo",
                        "quarter": 3,
                        "revenueActual": None,
                        "revenueEstimate": 45000000000,
                        "symbol": "MSFT",
                        "year": 2026,
                    },
                ]
            },
        )
    )
    p = _make_provider()
    events = await p.get_earnings_calendar(
        symbol=None,
        from_date=date(2026, 7, 1),
        to_date=date(2026, 8, 31),
    )
    assert len(events) == 2
    aapl = next(e for e in events if e.symbol == "AAPL")
    assert aapl.event_date == date(2026, 7, 25)
    assert aapl.hour == "amc"
    assert aapl.eps_estimate == 1.50
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_get_earnings_calendar_empty_returns_empty_list() -> None:
    respx.get("https://finnhub.io/api/v1/calendar/earnings").mock(
        return_value=httpx.Response(200, json={"earningsCalendar": None})
    )
    p = _make_provider()
    events = await p.get_earnings_calendar(symbol="AAPL")
    assert events == []
    await p.aclose()


# === Earnings surprises ===


@pytest.mark.asyncio
@respx.mock
async def test_get_earnings_surprises_parses_array() -> None:
    respx.get("https://finnhub.io/api/v1/stock/earnings").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "actual": 1.65,
                    "estimate": 1.50,
                    "period": "2026-03-31",
                    "quarter": 1,
                    "surprise": 0.15,
                    "surprisePercent": 10.0,
                    "symbol": "AAPL",
                    "year": 2026,
                },
                {
                    "actual": 1.92,
                    "estimate": 1.85,
                    "period": "2025-12-31",
                    "quarter": 4,
                    "surprise": 0.07,
                    "surprisePercent": 3.78,
                    "symbol": "AAPL",
                    "year": 2025,
                },
            ],
        )
    )
    p = _make_provider()
    surprises = await p.get_earnings_surprises("AAPL")
    assert len(surprises) == 2
    assert surprises[0].surprise == 0.15
    assert surprises[0].surprise_pct == 10.0
    await p.aclose()
