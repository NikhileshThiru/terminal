"""/tickers profile fetch — yfinance hang must degrade, not hang the request."""

from __future__ import annotations

import time

import pytest

from app.api import tickers


@pytest.mark.asyncio
async def test_hung_yfinance_info_degrades_to_empty_and_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HungTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        @property
        def info(self) -> dict[str, object]:
            time.sleep(0.3)  # Yahoo hanging
            return {"longName": "late"}

    monkeypatch.setattr(tickers.yf, "Ticker", _HungTicker)
    monkeypatch.setattr(tickers, "_PROFILE_FETCH_TIMEOUT_SECONDS", 0.05)
    tickers._PROFILE_CACHE.pop("HANG", None)

    started = time.monotonic()
    assert await tickers._fetch_profile("HANG") == {}
    assert time.monotonic() - started < 0.25  # returned at the timeout, not the hang
    assert "HANG" not in tickers._PROFILE_CACHE  # a miss is retried next time
