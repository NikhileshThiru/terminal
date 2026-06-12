"""SEC EDGAR FilingsProvider (DESIGN.md §5).

Free, no key, no SSN. EDGAR requires a `User-Agent` header in the format
`"Name/Version (contact@email)"` — set via `SEC_USER_AGENT` in .env. EDGAR's
published rate limit is 10 requests/second per host.

We hit the public JSON endpoints directly via httpx (rather than edgartools)
so the request shape is stable for respx-mocked tests.

Endpoints:
- `https://www.sec.gov/files/company_tickers.json` — ticker → CIK mapping.
- `https://data.sec.gov/submissions/CIK{cik:010d}.json` — recent filings.

`get_filing_text` is intentionally deferred to Phase 4 (when the agent
actually needs to read filing bodies); raises NOT_IMPLEMENTED for now.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from datetime import time as dtime
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from app.core.config import get_settings
from app.data.base import BaseProvider
from app.data.cache import Cache
from app.data.circuit_breaker import CircuitBreaker
from app.data.rate_limit import RateLimiter
from app.data.types import (
    Filing,
    FilingType,
    ProviderUnavailable,
    ProviderUnavailableReason,
)

_TICKER_LOOKUP_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
# EDGAR's "latest filings" feed — universe-wide, across all filers (DESIGN.md §5).
# `type` is optional; we pass an empty string to get every form, then filter
# client-side. `output=atom` returns parseable XML.
_LATEST_FILINGS_URL = "https://www.sec.gov/cgi-bin/browse-edgar"

_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

# Map SEC form names → our FilingType enum. Anything unmapped becomes OTHER.
_FORM_TO_TYPE: dict[str, FilingType] = {
    "8-K": FilingType.F_8K,
    "10-K": FilingType.F_10K,
    "10-Q": FilingType.F_10Q,
    "4": FilingType.F_4,
    "13F-HR": FilingType.F_13F,
    "13F-NT": FilingType.F_13F,
}


def _form_to_type(form: str) -> FilingType:
    return _FORM_TO_TYPE.get(form.upper(), FilingType.OTHER)


class EdgarFilingsProvider(BaseProvider):
    """FilingsProvider backed by SEC EDGAR public JSON endpoints."""

    name = "edgar"

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        client: httpx.AsyncClient | None = None,
        rate_limiter: RateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        cache: Cache | None = None,
    ) -> None:
        settings = get_settings()
        self._user_agent = user_agent or settings.sec_user_agent
        self._client = (
            client
            if client is not None
            else httpx.AsyncClient(
                timeout=15.0,
                headers={"User-Agent": self._user_agent, "Accept": "application/json"},
            )
        )
        rl = rate_limiter or RateLimiter(
            rate_per_sec=settings.edgar_requests_per_second,
            burst=int(settings.edgar_requests_per_second),
        )
        super().__init__(
            name=self.name,
            rate_limiter=rl,
            circuit_breaker=circuit_breaker,
            cache=cache,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # === FilingsProvider Protocol ===

    async def get_recent_filings(
        self,
        symbol: str,
        filing_types: list[FilingType] | None = None,
        limit: int = 20,
    ) -> list[Filing]:
        type_key = ",".join(sorted(ft.value for ft in (filing_types or [])))
        cache_key = f"edgar:recent:{symbol.upper()}:{type_key}:{limit}"
        return await self._fetch_cached(
            key=cache_key,
            ttl_seconds=get_settings().cache_ttl_filings_list,
            fetch=lambda: self._fetch_recent(symbol, filing_types, limit),
        )

    async def get_latest_universe_filings(
        self,
        filing_types: list[FilingType] | None = None,
        limit: int = 40,
    ) -> list[Filing]:
        """All recent filings across every public filer (via EDGAR's atom feed).

        Powers universe-wide discovery: triage decides what's material across
        the whole market, not just a watchlist (DESIGN.md §4). Filings with no
        ticker mapping are dropped (most material 8-Ks come from companies
        that have public tickers; the rest is rare and would need a CIK→name
        path which we skip for now).
        """
        # Short cache — universe feed updates by the minute; cache helps the
        # poller's tick() be cheap if called multiple times within the window.
        type_key = ",".join(sorted(ft.value for ft in (filing_types or [])))
        cache_key = f"edgar:latest_universe:{type_key}:{limit}"
        return await self._fetch_cached(
            key=cache_key,
            ttl_seconds=60,
            fetch=lambda: self._fetch_latest_universe(filing_types, limit),
        )

    async def get_filing_text(self, accession: str) -> str:
        raise ProviderUnavailable(
            reason=ProviderUnavailableReason.NOT_IMPLEMENTED,
            message=(
                "get_filing_text deferred to Phase 4 (when the agent reads bodies); "
                "EDGAR returns filings as multi-part documents that need parsing"
            ),
            provider=self.name,
            retryable=False,
        )

    # === Internals ===

    async def _fetch_recent(
        self,
        symbol: str,
        filing_types: list[FilingType] | None,
        limit: int,
    ) -> list[Filing]:
        cik = await self._lookup_cik(symbol)
        url = _SUBMISSIONS_URL_TEMPLATE.format(cik=cik)
        resp = await self._client.get(url)
        if resp.status_code == 429:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.RATE_LIMITED,
                message=f"EDGAR returned 429 for CIK {cik}",
                provider=self.name,
            )
        if resp.status_code != 200:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                message=f"EDGAR submissions returned HTTP {resp.status_code}",
                provider=self.name,
            )

        data: dict[str, Any] = resp.json()
        recent = data.get("filings", {}).get("recent", {})
        forms: list[str] = recent.get("form", [])
        accessions: list[str] = recent.get("accessionNumber", [])
        dates: list[str] = recent.get("filingDate", [])
        primary_docs: list[str] = recent.get("primaryDocument", [])
        descriptions: list[str] = recent.get("primaryDocDescription", [])

        wanted_values: set[str] | None = {ft.value for ft in filing_types} if filing_types else None

        out: list[Filing] = []
        for form, acc, fdate, pdoc, desc in zip(
            forms, accessions, dates, primary_docs, descriptions, strict=False
        ):
            ft = _form_to_type(form)
            if (
                wanted_values is not None
                and form not in wanted_values
                and ft.value not in wanted_values
            ):
                continue
            try:
                filed_dt = datetime.combine(
                    datetime.fromisoformat(fdate).date(), dtime.min, tzinfo=UTC
                )
            except (ValueError, TypeError):
                filed_dt = datetime.now(UTC)
            acc_no_dashes = acc.replace("-", "")
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_no_dashes}/{pdoc}"
            out.append(
                Filing(
                    accession=acc,
                    cik=str(cik),
                    symbol=symbol.upper(),
                    filing_type=ft,
                    filed_at=filed_dt,
                    url=url,
                    title=desc or form,
                )
            )
            if len(out) >= limit:
                break
        return out

    async def _fetch_latest_universe(
        self,
        filing_types: list[FilingType] | None,
        limit: int,
    ) -> list[Filing]:
        """Hit the latest-filings atom feed, parse, map CIK → ticker."""
        params: dict[str, str] = {
            "action": "getcurrent",
            "type": "",
            "company": "",
            "dateb": "",
            "owner": "include",
            "count": str(max(limit, 40)),
            "output": "atom",
        }
        resp = await self._client.get(_LATEST_FILINGS_URL, params=params)
        if resp.status_code == 429:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.RATE_LIMITED,
                message="EDGAR latest-filings returned 429",
                provider=self.name,
            )
        if resp.status_code != 200:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                message=f"EDGAR latest-filings returned HTTP {resp.status_code}",
                provider=self.name,
            )

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as e:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                message=f"EDGAR latest-filings atom feed was not valid XML: {e}",
                provider=self.name,
            ) from e

        inverse_map = await self._get_inverse_ticker_map()
        wanted_values: set[str] | None = {ft.value for ft in filing_types} if filing_types else None

        out: list[Filing] = []
        for entry in root.findall("a:entry", _ATOM_NS):
            parsed = _parse_atom_entry(entry, inverse_map)
            if parsed is None:
                continue
            if (
                wanted_values is not None
                and parsed.filing_type.value not in wanted_values
                and _form_raw(entry) not in wanted_values
            ):
                continue
            out.append(parsed)
            if len(out) >= limit:
                break
        return out

    async def _get_inverse_ticker_map(self) -> dict[int, str]:
        """CIK → ticker (built from the cached ticker map)."""
        ticker_map = await self._get_ticker_map()
        return {cik: tick for tick, cik in ticker_map.items()}

    async def _lookup_cik(self, symbol: str) -> int:
        """Map ticker → CIK. The ticker file is cached once (it's ~2MB and stable)."""
        ticker_map = await self._get_ticker_map()
        cik = ticker_map.get(symbol.upper())
        if cik is None:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.DATA_MISSING,
                message=f"EDGAR has no CIK for symbol {symbol!r}",
                provider=self.name,
                retryable=False,
            )
        return cik

    async def _get_ticker_map(self) -> dict[str, int]:
        return await self._fetch_cached(
            key="edgar:ticker_map",
            ttl_seconds=86400,  # 24h — the ticker file is very stable
            fetch=self._fetch_ticker_map,
        )

    async def _fetch_ticker_map(self) -> dict[str, int]:
        resp = await self._client.get(_TICKER_LOOKUP_URL)
        if resp.status_code != 200:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                message=f"EDGAR ticker lookup returned HTTP {resp.status_code}",
                provider=self.name,
            )
        data: dict[str, Any] = resp.json()
        out: dict[str, int] = {}
        for entry in data.values():
            ticker = str(entry.get("ticker", "")).upper()
            cik = entry.get("cik_str")
            if ticker and cik is not None:
                out[ticker] = int(cik)
        return out


# === Atom-feed parsing helpers (used by get_latest_universe_filings) ===

# Title format from the latest-filings feed:
#   "8-K - APPLE INC (0000320193) (Filer)"
# Sometimes "Filer" is "Subject", etc.
_ATOM_TITLE_RE = re.compile(r"^(?P<form>[A-Z0-9/\-]+)\s+-\s+(?P<name>.+?)\s+\((?P<cik>\d+)\)")

# Atom id is a URN like:
#   urn:tag:sec.gov,2008:accession-number=0000320193-26-000123
_ATOM_ID_ACCESSION_RE = re.compile(r"accession-number=(?P<acc>[\d-]+)")


def _form_raw(entry: ET.Element) -> str:
    """Extract the raw form string from an atom entry's title (for filter matching)."""
    title = (entry.findtext("a:title", default="", namespaces=_ATOM_NS) or "").strip()
    m = _ATOM_TITLE_RE.match(title)
    return m.group("form").upper() if m else ""


def _parse_atom_entry(entry: ET.Element, inverse_ticker_map: dict[int, str]) -> Filing | None:
    """Parse one atom <entry> into a Filing. Returns None if the CIK can't be
    mapped to a ticker (we drop those to keep downstream symbol-aware)."""
    title = (entry.findtext("a:title", default="", namespaces=_ATOM_NS) or "").strip()
    m = _ATOM_TITLE_RE.match(title)
    if not m:
        return None
    form = m.group("form").upper()
    cik = int(m.group("cik"))
    ticker = inverse_ticker_map.get(cik)
    if not ticker:
        return None

    id_text = entry.findtext("a:id", default="", namespaces=_ATOM_NS) or ""
    acc_match = _ATOM_ID_ACCESSION_RE.search(id_text)
    if not acc_match:
        return None
    accession = acc_match.group("acc")

    updated = entry.findtext("a:updated", default="", namespaces=_ATOM_NS) or ""
    try:
        filed_at = datetime.fromisoformat(updated)
        if filed_at.tzinfo is None:
            filed_at = filed_at.replace(tzinfo=UTC)
    except ValueError:
        filed_at = datetime.now(UTC)

    # Link to the filing index page (the alternate-relation link in the entry).
    url: str | None = None
    for link in entry.findall("a:link", _ATOM_NS):
        if link.get("rel") in (None, "alternate"):
            url = link.get("href")
            break

    return Filing(
        accession=accession,
        cik=str(cik),
        symbol=ticker.upper(),
        filing_type=_form_to_type(form),
        filed_at=filed_at,
        url=url,
        title=m.group("name").strip(),
    )
