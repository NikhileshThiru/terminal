"""yfinance provider — tests with a FakeTicker so they don't hit Yahoo.

Verifies that the provider satisfies the PriceProvider and OptionsProvider
Protocols, parses yfinance output into typed Quote/OHLCBar/OptionContract,
and translates upstream failures into typed ProviderUnavailable.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from app.data.interfaces import OptionsProvider, PriceProvider
from app.data.types import ProviderUnavailable, ProviderUnavailableReason
from app.data.yfinance_provider import YFinanceProvider, _occ_symbol, _parse_occ

# === OCC symbol parsing ===


def test_parse_occ_call() -> None:
    underlying, exp, t, strike = _parse_occ("AAPL250117C00150000")
    assert underlying == "AAPL"
    assert exp == date(2025, 1, 17)
    assert t == "C"
    assert strike == Decimal(150)


def test_parse_occ_put_fractional_strike() -> None:
    underlying, exp, t, strike = _parse_occ("MSFT261231P00400500")
    assert underlying == "MSFT"
    assert exp == date(2026, 12, 31)
    assert t == "P"
    assert strike == Decimal("400.5")


def test_parse_occ_invalid_short_string() -> None:
    with pytest.raises(ValueError):
        _parse_occ("BAD")


def test_parse_occ_invalid_type_letter() -> None:
    with pytest.raises(ValueError):
        _parse_occ("AAPL250117X00150000")


def test_occ_roundtrip() -> None:
    s = _occ_symbol("AAPL", date(2025, 1, 17), "call", Decimal("150"))
    assert s == "AAPL250117C00150000"
    underlying, exp, t, strike = _parse_occ(s)
    assert (underlying, exp, t, strike) == ("AAPL", date(2025, 1, 17), "C", Decimal(150))


# === Fake Ticker for provider tests ===


class _FakeChain:
    def __init__(self, calls: pd.DataFrame | None, puts: pd.DataFrame | None) -> None:
        self.calls = calls
        self.puts = puts


class FakeTicker:
    def __init__(
        self,
        symbol: str,
        *,
        hist: pd.DataFrame | None = None,
        options: tuple[str, ...] = (),
        chain_calls: pd.DataFrame | None = None,
        chain_puts: pd.DataFrame | None = None,
        fail_on: set[str] | None = None,
    ) -> None:
        self.symbol = symbol
        self._hist = hist
        self._options = options
        self._chain_calls = chain_calls
        self._chain_puts = chain_puts
        self._fail_on = fail_on or set()

    def history(self, **_: Any) -> pd.DataFrame | None:
        if "history" in self._fail_on:
            raise RuntimeError("yahoo broken")
        return self._hist

    @property
    def options(self) -> tuple[str, ...]:
        if "options" in self._fail_on:
            raise RuntimeError("yahoo broken")
        return self._options

    def option_chain(self, _: str) -> _FakeChain:
        if "chain" in self._fail_on:
            raise RuntimeError("yahoo broken")
        return _FakeChain(self._chain_calls, self._chain_puts)


def _hist_df(rows: list[tuple[str, float, float, float, float, int]]) -> pd.DataFrame:
    data = [
        {"Open": o, "High": h, "Low": low, "Close": c, "Volume": v} for (_, o, h, low, c, v) in rows
    ]
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for (d, *_rest) in rows])
    return pd.DataFrame(data, index=idx)


def _chain_df(strikes_and_quotes: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """Each row: (strike, bid, ask, last)."""
    return pd.DataFrame(
        [
            {
                "strike": s,
                "bid": b,
                "ask": a,
                "lastPrice": last,
                "volume": 100,
                "openInterest": 500,
                "impliedVolatility": 0.35,
            }
            for (s, b, a, last) in strikes_and_quotes
        ]
    )


def _make(factory: Callable[[str], FakeTicker]) -> YFinanceProvider:
    return YFinanceProvider(
        ticker_factory=factory,
        max_retries=0,  # tests don't want retry slowdowns
        retry_backoff_seconds=0.0,
    )


def test_satisfies_protocols() -> None:
    p = _make(lambda s: FakeTicker(s))
    assert isinstance(p, PriceProvider)
    assert isinstance(p, OptionsProvider)


# === Quotes ===


@pytest.mark.asyncio
async def test_get_latest_quote_happy_path() -> None:
    hist = _hist_df([("2026-05-30", 100.0, 105.0, 99.0, 102.5, 1_000_000)])
    p = _make(lambda s: FakeTicker(s, hist=hist))
    q = await p.get_latest_quote("AAPL")
    assert q.symbol == "AAPL"
    assert q.last == Decimal("102.5")


@pytest.mark.asyncio
async def test_get_latest_quote_empty_returns_data_missing() -> None:
    p = _make(lambda s: FakeTicker(s, hist=pd.DataFrame()))
    with pytest.raises(ProviderUnavailable) as exc:
        await p.get_latest_quote("AAPL")
    assert exc.value.reason == ProviderUnavailableReason.DATA_MISSING


@pytest.mark.asyncio
async def test_get_latest_quote_yahoo_failure_translates_to_upstream_error() -> None:
    p = _make(lambda s: FakeTicker(s, fail_on={"history"}))
    with pytest.raises(ProviderUnavailable) as exc:
        await p.get_latest_quote("AAPL")
    assert exc.value.reason == ProviderUnavailableReason.UPSTREAM_ERROR
    assert exc.value.provider == "yfinance"


# === OHLC ===


@pytest.mark.asyncio
async def test_get_ohlc_returns_typed_bars() -> None:
    hist = _hist_df(
        [
            ("2026-05-28", 100.0, 102.0, 99.0, 101.0, 1_000_000),
            ("2026-05-29", 101.5, 103.0, 100.0, 102.0, 1_100_000),
        ]
    )
    p = _make(lambda s: FakeTicker(s, hist=hist))
    bars = await p.get_ohlc(
        "AAPL",
        start=datetime(2026, 5, 28, tzinfo=UTC),
        end=datetime(2026, 5, 30, tzinfo=UTC),
    )
    assert len(bars) == 2
    assert bars[0].open == Decimal("100.0")
    assert bars[1].close == Decimal("102.0")
    assert bars[0].volume == 1_000_000


@pytest.mark.asyncio
async def test_get_ohlc_empty_returns_empty_list() -> None:
    p = _make(lambda s: FakeTicker(s, hist=pd.DataFrame()))
    bars = await p.get_ohlc(
        "AAPL",
        start=datetime(2026, 5, 28, tzinfo=UTC),
        end=datetime(2026, 5, 30, tzinfo=UTC),
    )
    assert bars == []


# === Options ===


@pytest.mark.asyncio
async def test_get_expirations_returns_dates() -> None:
    p = _make(lambda s: FakeTicker(s, options=("2026-06-20", "2026-09-19")))
    exps = await p.get_expirations("AAPL")
    assert exps == [date(2026, 6, 20), date(2026, 9, 19)]


@pytest.mark.asyncio
async def test_get_expirations_empty_raises_data_missing() -> None:
    p = _make(lambda s: FakeTicker(s, options=()))
    with pytest.raises(ProviderUnavailable) as exc:
        await p.get_expirations("AAPL")
    assert exc.value.reason == ProviderUnavailableReason.DATA_MISSING


@pytest.mark.asyncio
async def test_get_chain_returns_calls_and_puts() -> None:
    calls = _chain_df([(150.0, 5.0, 5.2, 5.1), (155.0, 3.0, 3.2, 3.1)])
    puts = _chain_df([(150.0, 4.5, 4.7, 4.6)])
    p = _make(lambda s: FakeTicker(s, chain_calls=calls, chain_puts=puts))
    chain = await p.get_chain("AAPL", date(2026, 6, 20))
    assert len(chain) == 3
    types = {c.option_type for c in chain}
    assert types == {"call", "put"}
    by_strike = {(c.strike, c.option_type): c for c in chain}
    aapl_150_call = by_strike[(Decimal("150.0"), "call")]
    assert aapl_150_call.bid == Decimal("5.0")
    assert aapl_150_call.occ_symbol == "AAPL260620C00150000"


@pytest.mark.asyncio
async def test_get_contract_quote_finds_specific_contract() -> None:
    calls = _chain_df([(150.0, 5.0, 5.2, 5.1)])
    puts = _chain_df([(150.0, 4.5, 4.7, 4.6)])
    p = _make(lambda s: FakeTicker(s, chain_calls=calls, chain_puts=puts))
    c = await p.get_contract_quote("AAPL260620C00150000")
    assert c.strike == Decimal("150.0")
    assert c.option_type == "call"
    assert c.bid == Decimal("5.0")


@pytest.mark.asyncio
async def test_get_contract_quote_missing_strike_returns_data_missing() -> None:
    calls = _chain_df([(150.0, 5.0, 5.2, 5.1)])
    puts = pd.DataFrame()
    p = _make(lambda s: FakeTicker(s, chain_calls=calls, chain_puts=puts))
    with pytest.raises(ProviderUnavailable) as exc:
        await p.get_contract_quote("AAPL260620C00200000")
    assert exc.value.reason == ProviderUnavailableReason.DATA_MISSING


@pytest.mark.asyncio
async def test_chain_failure_translates_to_upstream_error() -> None:
    p = _make(lambda s: FakeTicker(s, fail_on={"chain"}))
    with pytest.raises(ProviderUnavailable) as exc:
        await p.get_chain("AAPL", date(2026, 6, 20))
    assert exc.value.reason == ProviderUnavailableReason.UPSTREAM_ERROR
