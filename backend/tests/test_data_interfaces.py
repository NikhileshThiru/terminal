"""Contract tests: stubs must implement the Protocol interfaces.

runtime_checkable Protocols let us isinstance-check at test time. When real
providers land in Phase 2 they pass the same tests, which is the point.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.data.interfaces import (
    FilingsProvider,
    NewsProvider,
    OptionsProvider,
    PriceProvider,
)
from app.data.stubs import (
    StubFilingsProvider,
    StubNewsProvider,
    StubOptionsProvider,
    StubPriceProvider,
)
from app.data.types import ProviderUnavailable, ProviderUnavailableReason


def test_stubs_satisfy_protocols() -> None:
    assert isinstance(StubPriceProvider(), PriceProvider)
    assert isinstance(StubOptionsProvider(), OptionsProvider)
    assert isinstance(StubNewsProvider(), NewsProvider)
    assert isinstance(StubFilingsProvider(), FilingsProvider)


@pytest.mark.asyncio
async def test_price_stub_raises_typed_unavailable() -> None:
    with pytest.raises(ProviderUnavailable) as exc:
        await StubPriceProvider().get_latest_quote("AAPL")
    assert exc.value.reason == ProviderUnavailableReason.NOT_IMPLEMENTED
    assert exc.value.provider == "stub-prices"
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_options_stub_raises_typed_unavailable() -> None:
    with pytest.raises(ProviderUnavailable):
        await StubOptionsProvider().get_expirations("AAPL")


@pytest.mark.asyncio
async def test_news_stub_raises_typed_unavailable() -> None:
    with pytest.raises(ProviderUnavailable):
        await StubNewsProvider().get_recent(since=datetime.now(UTC))


@pytest.mark.asyncio
async def test_filings_stub_raises_typed_unavailable() -> None:
    with pytest.raises(ProviderUnavailable):
        await StubFilingsProvider().get_recent_filings("AAPL")
