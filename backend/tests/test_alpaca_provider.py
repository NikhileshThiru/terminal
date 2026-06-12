"""Alpaca provider — respx-mocked tests against the real endpoint shapes.

Mock payloads come from real smoke-test responses (`paper-api.alpaca.markets`
and `data.alpaca.markets`), so the parsing logic exercises the actual fields
Alpaca returns.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from app.data.alpaca import AlpacaProvider, _parse_occ_meta
from app.data.interfaces import OptionsProvider, PriceProvider
from app.data.types import ProviderUnavailable, ProviderUnavailableReason


def _make_provider() -> AlpacaProvider:
    return AlpacaProvider(api_key="PK_TEST", api_secret="SECRET_TEST")


# === OCC meta parsing (Alpaca's version reuses the standard layout) ===


def test_parse_occ_meta_call() -> None:
    exp, t, strike = _parse_occ_meta("AAPL260601C00225000")
    assert exp == date(2026, 6, 1)
    assert t == "call"
    assert strike == Decimal("225")


def test_parse_occ_meta_put_fractional() -> None:
    exp, t, strike = _parse_occ_meta("MSFT261231P00400500")
    assert exp == date(2026, 12, 31)
    assert t == "put"
    assert strike == Decimal("400.5")


def test_parse_occ_meta_rejects_short() -> None:
    with pytest.raises(ValueError):
        _parse_occ_meta("BAD")


def test_parse_occ_meta_rejects_bad_type() -> None:
    with pytest.raises(ValueError):
        _parse_occ_meta("AAPL250117X00150000")


# === Protocol satisfaction ===


def test_satisfies_protocols() -> None:
    p = _make_provider()
    assert isinstance(p, PriceProvider)
    assert isinstance(p, OptionsProvider)


def test_missing_credentials_raises_unavailable() -> None:
    """Construction with empty creds should raise AUTH_MISSING."""
    with pytest.raises(ProviderUnavailable) as exc:
        AlpacaProvider(api_key="", api_secret="")
    assert exc.value.reason == ProviderUnavailableReason.AUTH_MISSING


# === Latest quote ===


@pytest.mark.asyncio
@respx.mock
async def test_get_latest_quote_parses_bid_ask() -> None:
    respx.get("https://data.alpaca.markets/v2/stocks/AAPL/quotes/latest").mock(
        return_value=httpx.Response(
            200,
            json={
                "symbol": "AAPL",
                "quote": {
                    "ap": 296.27,
                    "as": 1,
                    "ax": "V",
                    "bp": 296.20,
                    "bs": 1,
                    "bx": "V",
                    "c": ["R"],
                    "t": "2026-05-29T20:00:02.184825082Z",
                    "z": "C",
                },
            },
        )
    )
    p = _make_provider()
    q = await p.get_latest_quote("AAPL")
    assert q.symbol == "AAPL"
    assert q.bid == Decimal("296.20")
    assert q.ask == Decimal("296.27")
    assert q.timestamp.tzinfo is not None
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_get_latest_quote_429_propagates_as_rate_limited() -> None:
    respx.get("https://data.alpaca.markets/v2/stocks/AAPL/quotes/latest").mock(
        return_value=httpx.Response(429)
    )
    p = _make_provider()
    with pytest.raises(ProviderUnavailable) as exc:
        await p.get_latest_quote("AAPL")
    assert exc.value.reason == ProviderUnavailableReason.RATE_LIMITED
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_get_latest_quote_403_propagates_as_auth() -> None:
    respx.get("https://data.alpaca.markets/v2/stocks/AAPL/quotes/latest").mock(
        return_value=httpx.Response(403)
    )
    p = _make_provider()
    with pytest.raises(ProviderUnavailable) as exc:
        await p.get_latest_quote("AAPL")
    assert exc.value.reason == ProviderUnavailableReason.AUTH_MISSING
    assert exc.value.retryable is False
    await p.aclose()


# === OHLC ===


@pytest.mark.asyncio
@respx.mock
async def test_get_ohlc_returns_typed_bars() -> None:
    respx.get("https://data.alpaca.markets/v2/stocks/AAPL/bars").mock(
        return_value=httpx.Response(
            200,
            json={
                "bars": [
                    {
                        "c": 302.25,
                        "h": 302.8,
                        "l": 298.08,
                        "n": 653718,
                        "o": 298.18,
                        "t": "2026-05-20T04:00:00Z",
                        "v": 38392499,
                        "vw": 301.05,
                    },
                    {
                        "c": 305.10,
                        "h": 306.0,
                        "l": 301.5,
                        "n": 500000,
                        "o": 302.5,
                        "t": "2026-05-21T04:00:00Z",
                        "v": 40000000,
                        "vw": 303.5,
                    },
                ],
                "next_page_token": None,
                "symbol": "AAPL",
            },
        )
    )
    p = _make_provider()
    bars = await p.get_ohlc(
        "AAPL",
        start=datetime(2026, 5, 20, tzinfo=UTC),
        end=datetime(2026, 5, 22, tzinfo=UTC),
    )
    assert len(bars) == 2
    assert bars[0].open == Decimal("298.18")
    assert bars[0].close == Decimal("302.25")
    assert bars[0].volume == 38392499
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_get_ohlc_empty_returns_empty_list() -> None:
    respx.get("https://data.alpaca.markets/v2/stocks/AAPL/bars").mock(
        return_value=httpx.Response(200, json={"bars": [], "symbol": "AAPL"})
    )
    p = _make_provider()
    bars = await p.get_ohlc(
        "AAPL",
        start=datetime(2026, 5, 20, tzinfo=UTC),
        end=datetime(2026, 5, 22, tzinfo=UTC),
    )
    assert bars == []
    await p.aclose()


# === Expirations ===


@pytest.mark.asyncio
@respx.mock
async def test_get_expirations_collects_unique_sorted_dates() -> None:
    respx.get("https://paper-api.alpaca.markets/v2/options/contracts").mock(
        return_value=httpx.Response(
            200,
            json={
                "option_contracts": [
                    {"expiration_date": "2026-06-20", "symbol": "AAPL260620C00150000"},
                    {"expiration_date": "2026-06-20", "symbol": "AAPL260620P00150000"},
                    {"expiration_date": "2026-07-18", "symbol": "AAPL260718C00150000"},
                    {"expiration_date": "2026-09-19", "symbol": "AAPL260919C00150000"},
                ],
                "next_page_token": None,
            },
        )
    )
    p = _make_provider()
    exps = await p.get_expirations("AAPL")
    assert exps == [date(2026, 6, 20), date(2026, 7, 18), date(2026, 9, 19)]
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_get_expirations_no_contracts_raises_data_missing() -> None:
    respx.get("https://paper-api.alpaca.markets/v2/options/contracts").mock(
        return_value=httpx.Response(200, json={"option_contracts": [], "next_page_token": None})
    )
    p = _make_provider()
    with pytest.raises(ProviderUnavailable) as exc:
        await p.get_expirations("AAPL")
    assert exc.value.reason == ProviderUnavailableReason.DATA_MISSING
    await p.aclose()


# === Chain ===


@pytest.mark.asyncio
@respx.mock
async def test_get_chain_parses_snapshots_and_filters_expiration() -> None:
    respx.get("https://data.alpaca.markets/v1beta1/options/snapshots/AAPL").mock(
        return_value=httpx.Response(
            200,
            json={
                "snapshots": {
                    "AAPL260620C00150000": {
                        "latestQuote": {
                            "ap": 5.20,
                            "bp": 5.00,
                            "t": "2026-05-29T20:00:00Z",
                        },
                        "latestTrade": {"p": 5.10, "s": 50, "t": "2026-05-29T19:55:00Z"},
                    },
                    "AAPL260620P00150000": {
                        "latestQuote": {
                            "ap": 3.30,
                            "bp": 3.10,
                            "t": "2026-05-29T20:00:00Z",
                        },
                    },
                    "AAPL260919C00150000": {  # different expiration; should be filtered out
                        "latestQuote": {"ap": 9.0, "bp": 8.8, "t": "2026-05-29T20:00:00Z"},
                    },
                },
                "next_page_token": None,
            },
        )
    )
    p = _make_provider()
    chain = await p.get_chain("AAPL", date(2026, 6, 20))
    assert len(chain) == 2
    by_type = {c.option_type: c for c in chain}
    call = by_type["call"]
    assert call.strike == Decimal("150")
    assert call.bid == Decimal("5.00")
    assert call.ask == Decimal("5.20")
    assert call.last == Decimal("5.10")
    put = by_type["put"]
    assert put.strike == Decimal("150")
    assert put.last is None  # no latestTrade in mock
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_get_contract_quote_finds_specific_contract() -> None:
    respx.get("https://data.alpaca.markets/v1beta1/options/snapshots/AAPL").mock(
        return_value=httpx.Response(
            200,
            json={
                "snapshots": {
                    "AAPL260620C00150000": {
                        "latestQuote": {"ap": 5.20, "bp": 5.00, "t": "2026-05-29T20:00:00Z"},
                    },
                },
                "next_page_token": None,
            },
        )
    )
    p = _make_provider()
    c = await p.get_contract_quote("AAPL260620C00150000")
    assert c.occ_symbol == "AAPL260620C00150000"
    assert c.option_type == "call"
    assert c.strike == Decimal("150")
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_get_contract_quote_missing_raises_data_missing() -> None:
    respx.get("https://data.alpaca.markets/v1beta1/options/snapshots/AAPL").mock(
        return_value=httpx.Response(
            200,
            json={
                "snapshots": {
                    "AAPL260620C00150000": {
                        "latestQuote": {"ap": 5.20, "bp": 5.00, "t": "2026-05-29T20:00:00Z"},
                    },
                },
                "next_page_token": None,
            },
        )
    )
    p = _make_provider()
    with pytest.raises(ProviderUnavailable) as exc:
        await p.get_contract_quote("AAPL260620P00200000")
    assert exc.value.reason == ProviderUnavailableReason.DATA_MISSING
    await p.aclose()
