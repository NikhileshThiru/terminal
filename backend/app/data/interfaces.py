"""Protocol interfaces for data providers (DESIGN.md §7).

Using Protocol (structural typing) so any class with matching methods qualifies,
without inheritance ceremony. Implementations live in app/data/<source>.py.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, runtime_checkable

from app.data.types import (
    Filing,
    FilingType,
    NewsItem,
    OHLCBar,
    OptionContract,
    Quote,
)


@runtime_checkable
class PriceProvider(Protocol):
    """Real-time and historical equity prices."""

    name: str

    async def get_latest_quote(self, symbol: str) -> Quote: ...

    async def get_ohlc(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str = "1Day",
    ) -> list[OHLCBar]: ...


@runtime_checkable
class OptionsProvider(Protocol):
    """Options chains and individual contract quotes."""

    name: str

    async def get_expirations(self, symbol: str) -> list[date]: ...

    async def get_chain(self, symbol: str, expiration: date) -> list[OptionContract]: ...

    async def get_contract_quote(self, occ_symbol: str) -> OptionContract: ...


@runtime_checkable
class NewsProvider(Protocol):
    """News articles and releases tagged with affected symbols."""

    name: str

    async def get_recent(
        self,
        since: datetime,
        symbols: list[str] | None = None,
    ) -> list[NewsItem]: ...


@runtime_checkable
class FilingsProvider(Protocol):
    """SEC filings (10-K, 10-Q, 8-K, Form 4, 13F)."""

    name: str

    async def get_recent_filings(
        self,
        symbol: str,
        filing_types: list[FilingType] | None = None,
        limit: int = 20,
    ) -> list[Filing]: ...

    async def get_latest_universe_filings(
        self,
        filing_types: list[FilingType] | None = None,
        limit: int = 40,
    ) -> list[Filing]:
        """All recent filings across every public filer (used by the universe poller)."""
        ...

    async def get_filing_text(self, accession: str) -> str: ...
