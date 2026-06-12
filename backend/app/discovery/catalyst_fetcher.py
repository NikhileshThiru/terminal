"""CalendarFetcher — pulls upcoming earnings dates and upserts CatalystEvents.

Runs on a slow cadence (default: every 6 hours). Iterates a configurable
"catalyst universe" of tickers (defaults to the watchlist for the first cut)
and asks Finnhub for each one's next earnings event. Idempotent on the
unique (symbol, event_type, event_date) constraint — a re-run with the same
data is a no-op except for refreshing the estimated_eps if it changed.

Why per-symbol calls instead of one big universe call:
- Finnhub's "all symbols" calendar endpoint returns ~thousands of events,
  burning quota and forcing client-side filtering.
- Per-symbol is N calls/day where N is the universe size, well under the
  free-tier ~60 calls/min limit for a watchlist of dozens.
- The flow is identical for a future broader universe — we just pass a
  bigger list.

Future: when we expand beyond earnings (Fed, FDA), each event source gets
its own fetcher; the CatalystEvent table absorbs them all by event_type.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.data.finnhub import FinnhubProvider
from app.data.types import EarningsEvent, ProviderUnavailable
from app.data.upsert import dialect_insert
from app.discovery.models import (
    CatalystEvent,
    CatalystEventState,
    CatalystEventType,
)

_log = get_logger(__name__)


@dataclass(frozen=True)
class FetchResult:
    """One tick's summary."""

    upserted: int
    skipped: int
    errors: int
    universe_size: int


class CalendarFetcher:
    """Pulls + upserts upcoming earnings dates for a symbol universe."""

    SOURCE = CatalystEventType.EARNINGS.value

    def __init__(
        self,
        *,
        finnhub: FinnhubProvider,
        session_factory: async_sessionmaker[AsyncSession],
        universe: Sequence[str],
        horizon_days: int = 60,
    ) -> None:
        self._finnhub = finnhub
        self._session_factory = session_factory
        self._universe = [s.upper() for s in universe]
        self._horizon_days = horizon_days

        # Counters surfaced via worker.status().
        self.runs_completed = 0
        self.events_upserted_total = 0
        self.last_run_at: datetime | None = None
        self.last_error: str | None = None

    async def run_once(self) -> FetchResult:
        """One tick: refresh the earnings calendar for every symbol in the universe."""
        from_date = date.today()
        to_date = from_date + timedelta(days=self._horizon_days)

        upserted = skipped = errors = 0
        async with self._session_factory() as session:
            # Mark any past-date scheduled events as EXPIRED — they never fired
            # (quota out, error, restart) and shouldn't keep showing as pending.
            await self._expire_past_events(session, today=from_date)

            for symbol in self._universe:
                try:
                    events = await self._finnhub.get_earnings_calendar(
                        symbol=symbol, from_date=from_date, to_date=to_date
                    )
                except ProviderUnavailable as e:
                    skipped += 1
                    _log.warning(
                        "calendar_fetch_skipped",
                        symbol=symbol,
                        reason=e.reason.value,
                    )
                    continue
                except Exception:
                    errors += 1
                    _log.exception("calendar_fetch_unexpected_error", symbol=symbol)
                    continue
                for ev in events:
                    if await self._upsert(session, ev):
                        upserted += 1
            await session.commit()

        self.runs_completed += 1
        self.events_upserted_total += upserted
        self.last_run_at = datetime.now(UTC)
        if upserted or skipped or errors:
            _log.info(
                "calendar_fetcher_tick",
                upserted=upserted,
                skipped=skipped,
                errors=errors,
                universe_size=len(self._universe),
            )
        return FetchResult(
            upserted=upserted,
            skipped=skipped,
            errors=errors,
            universe_size=len(self._universe),
        )

    async def _upsert(self, session: AsyncSession, ev: EarningsEvent) -> bool:
        """Insert or refresh one earnings row. Returns True if a new row landed
        (a re-run with no changes returns False)."""
        # Only refresh fields that the source might update mid-window —
        # estimated_eps/revenue can be updated by analysts. Symbol/date/type
        # are the identity and never change.
        values = {
            "symbol": ev.symbol.upper(),
            "event_type": self.SOURCE,
            "event_date": ev.event_date,
            "event_hour": ev.hour if ev.hour != "unknown" else None,
            "estimated_eps": (
                Decimal(str(ev.eps_estimate)) if ev.eps_estimate is not None else None
            ),
            "estimated_revenue_usd": (
                Decimal(str(ev.revenue_estimate)) if ev.revenue_estimate is not None else None
            ),
            "state": CatalystEventState.SCHEDULED.value,
            "scheduled_at": datetime.now(UTC),
            "raw_payload": ev.model_dump(mode="json"),
        }
        insert = dialect_insert(session)
        stmt = insert(CatalystEvent).values(**values)
        # Refresh the mutable fields on conflict; keep the original scheduled_at
        # so we have a record of when we first saw this catalyst.
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "event_type", "event_date"],
            set_={
                "event_hour": stmt.excluded.event_hour,
                "estimated_eps": stmt.excluded.estimated_eps,
                "estimated_revenue_usd": stmt.excluded.estimated_revenue_usd,
                "raw_payload": stmt.excluded.raw_payload,
            },
            where=CatalystEvent.state == CatalystEventState.SCHEDULED.value,
        )
        try:
            result = await session.execute(stmt)
        except IntegrityError:
            await session.rollback()
            return False
        # `rowcount` on a CursorResult tells us how many rows were affected;
        # >0 means we either inserted a fresh row or updated an existing one.
        # For our purposes that's "the table changed" — close enough to count.
        rowcount = getattr(result, "rowcount", 0) or 0
        return bool(rowcount > 0)

    async def _expire_past_events(self, session: AsyncSession, today: date) -> None:
        """Move scheduled events whose date has passed to EXPIRED."""
        from sqlalchemy import update

        await session.execute(
            update(CatalystEvent)
            .where(CatalystEvent.state == CatalystEventState.SCHEDULED.value)
            .where(CatalystEvent.event_date < today)
            .values(state=CatalystEventState.EXPIRED.value)
        )

    # Convenience for tests / API.
    async def upcoming(self, *, within_days: int = 14, limit: int = 50) -> list[CatalystEvent]:
        """Read-side query — scheduled events within the window."""
        today = date.today()
        end = today + timedelta(days=within_days)
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(CatalystEvent)
                        .where(CatalystEvent.state == CatalystEventState.SCHEDULED.value)
                        .where(CatalystEvent.event_date >= today)
                        .where(CatalystEvent.event_date <= end)
                        .order_by(CatalystEvent.event_date)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return list(rows)
