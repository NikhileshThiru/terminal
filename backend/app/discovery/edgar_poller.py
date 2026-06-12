"""EDGAR poller — universe-wide SEC filing source (DESIGN.md §4, §5).

Polls EDGAR's latest-filings atom feed every N seconds via
`get_latest_universe_filings()` — every public filer, not just a watchlist
(DESIGN.md §4: triage decides what's material across the whole market;
watchlist is UI prioritization, not a discovery gate). Dedupes against a
persistent (DB-backed) seen-accession set, publishes only fresh filings
to the event bus. On first start, "bootstraps" by recording every
currently-recent filing as seen so we don't fire theses for ancient news.

Persistent dedup: seen accessions are stored in the `seen_discovery_events`
table, not just an in-memory set, so a restart doesn't lose state and
silently swallow any filing that arrived during downtime. The in-memory
mirror is loaded from the DB at startup.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.data.interfaces import FilingsProvider
from app.data.types import Filing, FilingType, ProviderUnavailable
from app.data.upsert import dialect_insert
from app.discovery.bus import EventBus
from app.discovery.models import SeenDiscoveryEvent
from app.discovery.types import DiscoveryEvent

_log = get_logger(__name__)

# Default form catalogue. 8-K + 10-K + 10-Q are universe-wide (every filer,
# triage decides). Form 4 (insider transactions) is ALSO ingested but
# watchlist-filtered to control volume — 5-15k Form 4s/day across the market
# would burn the LLM triage quota. Insider buys on watchlist names are the
# academic-literature signal we actually care about.
_DEFAULT_FORMS: tuple[FilingType, ...] = (
    FilingType.F_8K,
    FilingType.F_10K,
    FilingType.F_10Q,
    FilingType.F_4,
)
# Forms whose symbol must be on the watchlist for the event to publish. Cost
# control for high-volume sources; everything else is universe-wide and
# triage-gated like normal.
_WATCHLIST_FILTERED_FORMS: frozenset[FilingType] = frozenset({FilingType.F_4})


class EdgarPoller:
    """Async task that publishes new EDGAR filings as DiscoveryEvents."""

    SOURCE_NAME = "edgar"

    def __init__(
        self,
        *,
        filings: FilingsProvider,
        bus: EventBus,
        poll_interval_seconds: float = 300.0,  # 5 min
        universe_limit: int = 40,  # EDGAR's atom feed default page size
        forms: Sequence[FilingType] = _DEFAULT_FORMS,
        watchlist_filtered_forms: frozenset[FilingType] = _WATCHLIST_FILTERED_FORMS,
        watchlist: Sequence[str] | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._filings = filings
        self._bus = bus
        self._interval = poll_interval_seconds
        self._limit = universe_limit
        self._forms = list(forms)
        self._watchlist_filtered_forms = watchlist_filtered_forms
        self._watchlist_upper: frozenset[str] = (
            frozenset(s.upper() for s in watchlist) if watchlist else frozenset()
        )
        # In-memory mirror of the DB dedup state. Loaded from DB at start();
        # writes are dual (DB + in-memory). If session_factory is None, dedup
        # is in-memory-only (legacy behavior, used in tests).
        self._seen: set[str] = set()
        self._session_factory = session_factory
        self._bootstrapped = False
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self.polls_completed = 0
        self.events_published = 0
        self.last_poll_at: datetime | None = None
        self.last_error: str | None = None

    @property
    def poll_interval(self) -> float:
        return self._interval

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        # Warm the in-memory seen set from the persistent dedup table so a
        # restart preserves the previous session's bootstrap. If the table is
        # populated, _bootstrapped flips to True and the next poll publishes
        # only NEW filings (no re-bootstrap).
        await self._load_seen_from_db()
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())
        _log.info(
            "edgar_poller_started",
            interval_seconds=self._interval,
            universe_limit=self._limit,
            seen_count_loaded=len(self._seen),
            bootstrapped=self._bootstrapped,
        )

    async def _load_seen_from_db(self) -> None:
        if self._session_factory is None:
            return
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(SeenDiscoveryEvent.external_id).where(
                            SeenDiscoveryEvent.source == self.SOURCE_NAME
                        )
                    )
                )
                .scalars()
                .all()
            )
        if rows:
            self._seen.update(rows)
            # If we have prior state, skip the bootstrap step on first poll.
            self._bootstrapped = True

    async def _persist_seen(self, session: AsyncSession, external_id: str) -> None:
        """Insert into seen table; tolerate UNIQUE conflicts (idempotent)."""
        if self._session_factory is None:
            return
        insert = dialect_insert(session)
        stmt = insert(SeenDiscoveryEvent).values(
            source=self.SOURCE_NAME,
            external_id=external_id,
            seen_at=datetime.now(UTC),
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=["source", "external_id"])
        try:
            await session.execute(stmt)
        except IntegrityError:
            # Defensive — race shouldn't happen in single-process tick, but
            # we never want dedup writes to crash the poll loop.
            await session.rollback()

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                self._task.cancel()
        self._task = None
        _log.info("edgar_poller_stopped")

    async def _run(self) -> None:
        """The poll loop. Catches all errors so one bad poll doesn't kill the task."""
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                _log.exception("edgar_poller_tick_failed", error=str(e))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
            except TimeoutError:
                continue
            else:
                break

    async def tick(self) -> int:
        """One poll iteration. Returns count of newly-published events."""
        self.polls_completed += 1
        self.last_poll_at = datetime.now(UTC)
        published = 0
        # One DB session for the entire tick — cheaper than per-filing connections.
        session: AsyncSession | None = None
        if self._session_factory is not None:
            session = self._session_factory()
            await session.__aenter__()
        try:
            try:
                filings = await self._filings.get_latest_universe_filings(
                    filing_types=self._forms, limit=self._limit
                )
            except ProviderUnavailable as e:
                _log.warning("edgar_poller_provider_unavailable", reason=e.reason.value)
                filings = []

            for f in filings:
                if f.accession in self._seen:
                    continue
                self._seen.add(f.accession)
                if session is not None:
                    await self._persist_seen(session, f.accession)
                if not self._bootstrapped:
                    # First poll: just learn what's already out there.
                    continue
                # Universe filings already carry the resolved symbol; drop
                # anything we couldn't map (defensive — provider drops these
                # before they reach us, but mypy can't see that).
                if not f.symbol:
                    continue
                # High-volume forms (Form 4) are watchlist-filtered to bound
                # triage cost. 5k+ Form 4s/day across the market would
                # exhaust the LLM quota even on Groq's free tier.
                if (
                    f.filing_type in self._watchlist_filtered_forms
                    and self._watchlist_upper
                    and f.symbol.upper() not in self._watchlist_upper
                ):
                    continue
                ev = _filing_to_event(f.symbol, f)
                await self._bus.publish(ev)
                self.events_published += 1
                published += 1
                _log.info("edgar_event_published", summary=ev.short_summary())

            if session is not None:
                await session.commit()
        finally:
            if session is not None:
                await session.__aexit__(None, None, None)
        if not self._bootstrapped:
            self._bootstrapped = True
            _log.info(
                "edgar_poller_bootstrapped",
                seen_count=len(self._seen),
                note="Only events filed after now will be published.",
            )
        return published


def _filing_to_event(symbol: str, f: Filing) -> DiscoveryEvent:
    return DiscoveryEvent(
        id=f.accession,
        source="edgar",
        kind="filing",
        symbols=[symbol.upper()],
        headline=(
            f"{f.filing_type.value} filed by {symbol.upper()}: {f.title or f.filing_type.value}"
        ),
        body=None,
        url=f.url,
        published_at=f.filed_at,
        payload={
            "accession": f.accession,
            "cik": f.cik,
            "filing_type": f.filing_type.value,
            "title": f.title,
        },
    )
