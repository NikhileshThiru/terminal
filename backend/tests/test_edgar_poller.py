"""EdgarPoller tests — universe-mode behavior: dedup, bootstrap, persistent state."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.data.types import (
    Filing,
    FilingType,
    ProviderUnavailable,
    ProviderUnavailableReason,
)
from app.discovery.bus import InMemoryEventBus
from app.discovery.edgar_poller import EdgarPoller


class FakeFilings:
    """FilingsProvider-shaped stub for universe-feed polling."""

    name = "fake-edgar"

    def __init__(self, universe: list[Filing] | None = None) -> None:
        self.universe: list[Filing] = list(universe or [])
        self.fail_universe: bool = False
        self.calls: int = 0

    async def get_recent_filings(self, *_: object, **__: object) -> list[Filing]:
        raise NotImplementedError("universe-mode tests use get_latest_universe_filings")

    async def get_latest_universe_filings(
        self,
        filing_types: list[FilingType] | None = None,
        limit: int = 40,
    ) -> list[Filing]:
        self.calls += 1
        if self.fail_universe:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                message="edgar feed down",
                provider="fake-edgar",
            )
        return list(self.universe[:limit])

    async def get_filing_text(self, accession: str) -> str:
        raise NotImplementedError


def _filing(acc: str, sym: str = "AAPL", form: FilingType = FilingType.F_8K) -> Filing:
    return Filing(
        accession=acc,
        cik="320193",
        symbol=sym,
        filing_type=form,
        filed_at=datetime.now(UTC),
        url=f"https://example.com/{acc}",
        title=f"{form.value} report",
    )


@pytest.mark.asyncio
async def test_first_tick_bootstraps_and_publishes_nothing() -> None:
    """On first poll, all currently-recent filings are marked as seen with zero events."""
    bus = InMemoryEventBus()
    filings = FakeFilings([_filing("acc-1"), _filing("acc-2")])
    poller = EdgarPoller(filings=filings, bus=bus)
    n = await poller.tick()
    assert n == 0
    assert bus.qsize() == 0
    assert poller.polls_completed == 1
    assert poller.events_published == 0


@pytest.mark.asyncio
async def test_second_tick_publishes_only_new_filings() -> None:
    bus = InMemoryEventBus()
    filings = FakeFilings([_filing("acc-1")])
    poller = EdgarPoller(filings=filings, bus=bus)
    await poller.tick()  # bootstrap
    filings.universe.insert(0, _filing("acc-2", sym="MSFT"))
    n = await poller.tick()
    assert n == 1
    assert bus.qsize() == 1
    ev = await bus.consume()
    assert ev.id == "acc-2"
    assert ev.symbols == ["MSFT"]  # symbol carried through from the Filing
    assert ev.source == "edgar"


@pytest.mark.asyncio
async def test_dedup_across_polls() -> None:
    bus = InMemoryEventBus()
    filings = FakeFilings([_filing("acc-1")])
    poller = EdgarPoller(filings=filings, bus=bus)
    await poller.tick()  # bootstrap
    filings.universe.insert(0, _filing("acc-2"))
    await poller.tick()  # publishes acc-2
    await poller.tick()  # acc-2 already seen — no double publish
    assert bus.qsize() == 1


@pytest.mark.asyncio
async def test_provider_failure_skipped_gracefully() -> None:
    """An EDGAR outage during a tick logs + skips; doesn't crash the loop."""
    bus = InMemoryEventBus()
    filings = FakeFilings([_filing("acc-1")])
    filings.fail_universe = True
    poller = EdgarPoller(filings=filings, bus=bus)
    n = await poller.tick()
    assert n == 0
    assert bus.qsize() == 0


@pytest.mark.asyncio
async def test_filings_without_symbol_are_dropped() -> None:
    """The provider should drop them, but defensively the poller also skips empty symbols."""
    bus = InMemoryEventBus()
    filings = FakeFilings([_filing("acc-1", sym="")])
    poller = EdgarPoller(filings=filings, bus=bus)
    await poller.tick()  # bootstrap
    filings.universe.insert(0, _filing("acc-2", sym=""))
    n = await poller.tick()
    assert n == 0
    assert bus.qsize() == 0


def test_poll_interval_property_reflects_constructor_value() -> None:
    """Public read accessor — replaces the prior `_interval` private-attribute reach."""
    bus = InMemoryEventBus()
    filings = FakeFilings()
    poller = EdgarPoller(filings=filings, bus=bus, poll_interval_seconds=42.0)
    assert poller.poll_interval == 42.0


# === Persistent dedup ===


@pytest.fixture
async def db_factory():
    """Async session factory backed by an in-memory SQLite DB."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    # Need the discovery model registered.
    from app.discovery.models import SeenDiscoveryEvent  # noqa: F401
    from app.eval.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_persistent_dedup_survives_restart(db_factory) -> None:
    """Two pollers sharing a DB factory share dedup state across `restarts`."""
    bus = InMemoryEventBus()
    filings = FakeFilings([_filing("acc-1")])

    poller_a = EdgarPoller(filings=filings, bus=bus, session_factory=db_factory)
    await poller_a._load_seen_from_db()
    await poller_a.tick()
    assert bus.qsize() == 0, "bootstrap must not publish"

    filings.universe.insert(0, _filing("acc-2"))
    poller_b = EdgarPoller(filings=filings, bus=bus, session_factory=db_factory)
    await poller_b._load_seen_from_db()
    assert poller_b._bootstrapped, "prior DB state should set bootstrapped"
    n = await poller_b.tick()

    assert n == 1, "acc-2 should publish without going through bootstrap"
    assert bus.qsize() == 1
    ev = await bus.consume()
    assert ev.id == "acc-2"


@pytest.mark.asyncio
async def test_persistent_dedup_idempotent_on_conflict(db_factory) -> None:
    """Re-tick with the same filing must not crash on the unique constraint."""
    bus = InMemoryEventBus()
    filings = FakeFilings([_filing("acc-1")])
    poller = EdgarPoller(filings=filings, bus=bus, session_factory=db_factory)
    await poller.tick()
    poller._seen.clear()
    await poller.tick()
    # No exception means the conflict was handled gracefully.
