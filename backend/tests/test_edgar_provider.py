"""EDGAR FilingsProvider — tests with respx-mocked HTTP responses.

Verifies that the provider satisfies the FilingsProvider Protocol, returns
typed Filing objects, filters by form, propagates upstream failures as
ProviderUnavailable with the correct reason, and caches the ticker→CIK map.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.data.edgar import EdgarFilingsProvider
from app.data.interfaces import FilingsProvider
from app.data.types import FilingType, ProviderUnavailable, ProviderUnavailableReason

TICKER_RESPONSE = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
}

SUBMISSIONS_AAPL = {
    "cik": "320193",
    "name": "Apple Inc.",
    "filings": {
        "recent": {
            "form": ["8-K", "10-Q", "4", "8-K", "13F-HR"],
            "accessionNumber": [
                "0000320193-26-000050",
                "0000320193-26-000049",
                "0000320193-26-000048",
                "0000320193-26-000047",
                "0000320193-26-000046",
            ],
            "filingDate": ["2026-05-15", "2026-05-01", "2026-04-30", "2026-04-25", "2026-04-20"],
            "primaryDocument": [
                "aapl-2026.htm",
                "aapl-q.htm",
                "form4.htm",
                "8-k.htm",
                "13f.htm",
            ],
            "primaryDocDescription": [
                "8-K Current Report",
                "Quarterly Report",
                "Form 4",
                "8-K Item 2.02",
                "13F-HR",
            ],
        }
    },
}


def _make_provider() -> EdgarFilingsProvider:
    # respx hooks the global httpx transport, so a default client is fine here.
    return EdgarFilingsProvider(user_agent="Test/0.1 (test@example.com)")


def test_satisfies_filings_provider_protocol() -> None:
    """Static contract check using runtime_checkable."""
    p = _make_provider()
    assert isinstance(p, FilingsProvider)


@pytest.mark.asyncio
@respx.mock
async def test_get_recent_returns_typed_filings() -> None:
    respx.get("https://www.sec.gov/files/company_tickers.json").mock(
        return_value=httpx.Response(200, json=TICKER_RESPONSE)
    )
    respx.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
        return_value=httpx.Response(200, json=SUBMISSIONS_AAPL)
    )

    p = _make_provider()
    try:
        filings = await p.get_recent_filings("AAPL", limit=10)
        assert len(filings) == 5
        first = filings[0]
        assert first.symbol == "AAPL"
        assert first.cik == "320193"
        assert first.accession == "0000320193-26-000050"
        assert first.filing_type == FilingType.F_8K
        assert first.url.startswith("https://www.sec.gov/Archives/edgar/data/320193/")
    finally:
        await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_filter_by_filing_type() -> None:
    respx.get("https://www.sec.gov/files/company_tickers.json").mock(
        return_value=httpx.Response(200, json=TICKER_RESPONSE)
    )
    respx.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
        return_value=httpx.Response(200, json=SUBMISSIONS_AAPL)
    )

    p = _make_provider()
    try:
        only_8k = await p.get_recent_filings("AAPL", filing_types=[FilingType.F_8K], limit=10)
        assert len(only_8k) == 2
        assert all(f.filing_type == FilingType.F_8K for f in only_8k)
    finally:
        await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_limit_truncates_results() -> None:
    respx.get("https://www.sec.gov/files/company_tickers.json").mock(
        return_value=httpx.Response(200, json=TICKER_RESPONSE)
    )
    respx.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
        return_value=httpx.Response(200, json=SUBMISSIONS_AAPL)
    )

    p = _make_provider()
    try:
        few = await p.get_recent_filings("AAPL", limit=2)
        assert len(few) == 2
    finally:
        await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_unknown_symbol_raises_data_missing() -> None:
    respx.get("https://www.sec.gov/files/company_tickers.json").mock(
        return_value=httpx.Response(200, json=TICKER_RESPONSE)
    )

    p = _make_provider()
    try:
        with pytest.raises(ProviderUnavailable) as exc:
            await p.get_recent_filings("NOTREAL", limit=10)
        assert exc.value.reason == ProviderUnavailableReason.DATA_MISSING
        assert exc.value.retryable is False
    finally:
        await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_429_propagates_as_rate_limited() -> None:
    respx.get("https://www.sec.gov/files/company_tickers.json").mock(
        return_value=httpx.Response(200, json=TICKER_RESPONSE)
    )
    respx.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
        return_value=httpx.Response(429, text="rate limit")
    )

    p = _make_provider()
    try:
        with pytest.raises(ProviderUnavailable) as exc:
            await p.get_recent_filings("AAPL", limit=10)
        # The fetcher raises RATE_LIMITED directly; BaseProvider passes typed
        # ProviderUnavailable through without rewrapping.
        assert exc.value.reason == ProviderUnavailableReason.RATE_LIMITED
    finally:
        await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_ticker_map_cached_across_symbols() -> None:
    """The ~2MB ticker file should be fetched once, not per symbol."""
    ticker_route = respx.get("https://www.sec.gov/files/company_tickers.json").mock(
        return_value=httpx.Response(200, json=TICKER_RESPONSE)
    )
    respx.get("https://data.sec.gov/submissions/CIK0000320193.json").mock(
        return_value=httpx.Response(200, json=SUBMISSIONS_AAPL)
    )
    respx.get("https://data.sec.gov/submissions/CIK0000789019.json").mock(
        return_value=httpx.Response(200, json={**SUBMISSIONS_AAPL, "cik": "789019"})
    )

    p = _make_provider()
    try:
        await p.get_recent_filings("AAPL", limit=5)
        await p.get_recent_filings("MSFT", limit=5)
        assert ticker_route.call_count == 1
    finally:
        await p.aclose()


@pytest.mark.asyncio
async def test_get_filing_text_is_explicitly_unimplemented() -> None:
    p = _make_provider()
    try:
        with pytest.raises(ProviderUnavailable) as exc:
            await p.get_filing_text("0000320193-26-000050")
        assert exc.value.reason == ProviderUnavailableReason.NOT_IMPLEMENTED
    finally:
        await p.aclose()


# === Universe-wide latest filings (Step 4) ===

_LATEST_ATOM = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>8-K - APPLE INC (0000320193) (Filer)</title>
    <link rel="alternate" type="text/html" href="https://example.com/aapl-8k.html"/>
    <updated>2026-06-04T10:30:00-04:00</updated>
    <id>urn:tag:sec.gov,2008:accession-number=0000320193-26-000111</id>
    <summary type="html">8-K Item 2.02</summary>
  </entry>
  <entry>
    <title>10-Q - MICROSOFT CORP (0000789019) (Filer)</title>
    <link rel="alternate" type="text/html" href="https://example.com/msft-10q.html"/>
    <updated>2026-06-04T09:00:00-04:00</updated>
    <id>urn:tag:sec.gov,2008:accession-number=0000789019-26-000222</id>
  </entry>
  <entry>
    <title>4 - SOME UNKNOWN FILER (0000999999) (Filer)</title>
    <link rel="alternate" type="text/html" href="https://example.com/unknown-4.html"/>
    <updated>2026-06-04T08:00:00-04:00</updated>
    <id>urn:tag:sec.gov,2008:accession-number=0000999999-26-000333</id>
  </entry>
</feed>"""


@pytest.mark.asyncio
@respx.mock(base_url="https://www.sec.gov")
async def test_universe_filings_parses_atom_and_maps_cik_to_ticker(respx_mock) -> None:
    respx_mock.get("/files/company_tickers.json").mock(
        return_value=httpx.Response(200, json=TICKER_RESPONSE)
    )
    respx_mock.get("/cgi-bin/browse-edgar").mock(
        return_value=httpx.Response(200, text=_LATEST_ATOM)
    )
    p = _make_provider()
    try:
        results = await p.get_latest_universe_filings(limit=10)
        symbols = [f.symbol for f in results]
        # AAPL + MSFT mapped; the unknown CIK 999999 is dropped.
        assert symbols == ["AAPL", "MSFT"]
        aapl = results[0]
        assert aapl.filing_type == FilingType.F_8K
        assert aapl.accession == "0000320193-26-000111"
        assert aapl.cik == "320193"
        assert aapl.url == "https://example.com/aapl-8k.html"
    finally:
        await p.aclose()


@pytest.mark.asyncio
@respx.mock(base_url="https://www.sec.gov")
async def test_universe_filings_filter_by_form(respx_mock) -> None:
    respx_mock.get("/files/company_tickers.json").mock(
        return_value=httpx.Response(200, json=TICKER_RESPONSE)
    )
    respx_mock.get("/cgi-bin/browse-edgar").mock(
        return_value=httpx.Response(200, text=_LATEST_ATOM)
    )
    p = _make_provider()
    try:
        results = await p.get_latest_universe_filings(filing_types=[FilingType.F_8K], limit=10)
        assert [f.symbol for f in results] == ["AAPL"]
    finally:
        await p.aclose()


@pytest.mark.asyncio
@respx.mock(base_url="https://www.sec.gov", assert_all_called=False)
async def test_universe_filings_429_propagates(respx_mock) -> None:
    respx_mock.get("/cgi-bin/browse-edgar").mock(return_value=httpx.Response(429))
    p = _make_provider()
    try:
        with pytest.raises(ProviderUnavailable) as exc:
            await p.get_latest_universe_filings(limit=5)
        assert exc.value.reason == ProviderUnavailableReason.RATE_LIMITED
    finally:
        await p.aclose()


@pytest.mark.asyncio
@respx.mock(base_url="https://www.sec.gov", assert_all_called=False)
async def test_universe_filings_invalid_xml_raises_upstream_error(respx_mock) -> None:
    respx_mock.get("/cgi-bin/browse-edgar").mock(
        return_value=httpx.Response(200, text="<not-valid-xml")
    )
    p = _make_provider()
    try:
        with pytest.raises(ProviderUnavailable) as exc:
            await p.get_latest_universe_filings(limit=5)
        assert exc.value.reason == ProviderUnavailableReason.UPSTREAM_ERROR
    finally:
        await p.aclose()
