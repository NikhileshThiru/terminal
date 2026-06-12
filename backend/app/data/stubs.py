"""Stub implementations of every provider interface.

These are wired in Phase 1 so the rest of the app can import providers
without real implementations existing yet. Real implementations land in
Phase 2 and replace these via config-driven registration.

Every stub raises ProviderUnavailable(NOT_IMPLEMENTED) — the same typed
failure path that real providers use when they're down. Callers wired
against the stubs will correctly handle real provider failures too.
"""

from __future__ import annotations

from datetime import date, datetime

from app.data.types import (
    Filing,
    FilingType,
    NewsItem,
    OHLCBar,
    OptionContract,
    ProviderUnavailable,
    ProviderUnavailableReason,
    Quote,
)


def _unimplemented(provider: str, what: str) -> ProviderUnavailable:
    return ProviderUnavailable(
        reason=ProviderUnavailableReason.NOT_IMPLEMENTED,
        message=f"{provider}.{what}() is a Phase 1 stub; real impl lands in Phase 2",
        provider=provider,
        retryable=False,
    )


class StubPriceProvider:
    name = "stub-prices"

    async def get_latest_quote(self, symbol: str) -> Quote:
        raise _unimplemented(self.name, "get_latest_quote")

    async def get_ohlc(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str = "1Day",
    ) -> list[OHLCBar]:
        raise _unimplemented(self.name, "get_ohlc")


class StubOptionsProvider:
    name = "stub-options"

    async def get_expirations(self, symbol: str) -> list[date]:
        raise _unimplemented(self.name, "get_expirations")

    async def get_chain(self, symbol: str, expiration: date) -> list[OptionContract]:
        raise _unimplemented(self.name, "get_chain")

    async def get_contract_quote(self, occ_symbol: str) -> OptionContract:
        raise _unimplemented(self.name, "get_contract_quote")


class StubNewsProvider:
    name = "stub-news"

    async def get_recent(
        self,
        since: datetime,
        symbols: list[str] | None = None,
    ) -> list[NewsItem]:
        raise _unimplemented(self.name, "get_recent")


class StubFilingsProvider:
    name = "stub-filings"

    async def get_recent_filings(
        self,
        symbol: str,
        filing_types: list[FilingType] | None = None,
        limit: int = 20,
    ) -> list[Filing]:
        raise _unimplemented(self.name, "get_recent_filings")

    async def get_latest_universe_filings(
        self,
        filing_types: list[FilingType] | None = None,
        limit: int = 40,
    ) -> list[Filing]:
        raise _unimplemented(self.name, "get_latest_universe_filings")

    async def get_filing_text(self, accession: str) -> str:
        raise _unimplemented(self.name, "get_filing_text")
