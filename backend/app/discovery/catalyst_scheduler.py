"""CatalystScheduler — fires pre-positioning theses on scheduled catalysts.

This is the "catalyst" half of the catalyst-vs-reactive split (DESIGN.md §8).
Reactive theses are latency-disadvantaged by design (free data is delayed);
catalyst theses fire on KNOWN events, in advance, which removes the latency
problem entirely. That's why the per-bucket scoring matters — catalyst is
the bucket where measurable edge is at all plausible.

Trigger logic:
- Every N minutes, find CatalystEvents where:
  * state = scheduled
  * event_date is between today and (today + lead_days)
  * thesis_id is null (we haven't fired yet)
- For each, build a catalyst prompt for the copilot and run it with
  source_bucket="catalyst", persist the thesis, transition the event to
  TRIGGERED with thesis_id and triggered_at.
- Per-event failure isolation: one bad triage / copilot crash doesn't stop
  the others. Failures stay scheduled and retry next tick (up to expiry).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.copilot import Copilot, CopilotError
from app.core.correlation import set_correlation_id
from app.core.logging import get_logger
from app.discovery.models import CatalystEvent, CatalystEventState
from app.eval.persistence import write_thesis

_log = get_logger(__name__)


@dataclass(frozen=True)
class ScheduleResult:
    """One tick's summary."""

    triggered: int
    skipped: int
    failures: int
    candidates: int


def _build_catalyst_prompt(event: CatalystEvent) -> str:
    """Convert a catalyst row into a user-thesis-style prompt for the copilot."""
    days_out = (event.event_date - datetime.now(UTC).date()).days
    when = "today" if days_out == 0 else f"in {days_out} day{'s' if days_out != 1 else ''}"

    pieces = [
        f"CATALYST PRE-POSITIONING: scheduled {event.event_type} for {event.symbol} {when} "
        f"({event.event_date.isoformat()}).",
    ]
    if event.event_hour:
        hour_label = {"bmo": "before market open", "amc": "after market close", "dmh": "intraday"}
        pieces.append(f"Timing: {hour_label.get(event.event_hour, event.event_hour)}.")
    if event.estimated_eps is not None:
        pieces.append(f"Consensus EPS estimate: {event.estimated_eps}.")
    if event.estimated_revenue_usd is not None:
        pieces.append(f"Consensus revenue estimate: ${event.estimated_revenue_usd:,.0f}.")
    pieces.append(
        "Research this thesis: how should we pre-position for this catalyst? Pull the "
        "earnings surprise history, recent analyst ratings, and current options chain. "
        "Pick a defined-risk single-leg play that expires shortly AFTER the event."
    )
    return "\n".join(pieces)


class CatalystScheduler:
    """Triggers catalyst theses for events within the lead window."""

    def __init__(
        self,
        *,
        copilot: Copilot,
        session_factory: async_sessionmaker[AsyncSession],
        lead_days: int = 2,
        risk_budget_usd: float = 500.0,
    ) -> None:
        self._copilot = copilot
        self._session_factory = session_factory
        self._lead_days = lead_days
        self._risk_budget = risk_budget_usd

        # Counters surfaced via worker.status().
        self.runs_completed = 0
        self.theses_triggered = 0
        self.last_run_at: datetime | None = None
        self.last_trigger_at: datetime | None = None
        self.last_error: str | None = None

    async def run_once(self) -> ScheduleResult:
        """One tick: find candidates inside the lead window and trigger each."""
        today = datetime.now(UTC).date()
        deadline = today + timedelta(days=self._lead_days)

        triggered = skipped = failures = 0
        async with self._session_factory() as session:
            candidates = (
                (
                    await session.execute(
                        select(CatalystEvent)
                        .where(CatalystEvent.state == CatalystEventState.SCHEDULED.value)
                        .where(CatalystEvent.event_date >= today)
                        .where(CatalystEvent.event_date <= deadline)
                        .where(CatalystEvent.thesis_id.is_(None))
                        .order_by(CatalystEvent.event_date)
                    )
                )
                .scalars()
                .all()
            )

            for event in candidates:
                try:
                    fired = await self._trigger_one(session, event)
                except Exception as e:
                    failures += 1
                    self.last_error = f"{type(e).__name__}: {e}"
                    _log.exception(
                        "catalyst_trigger_unexpected_error",
                        event_id=event.id,
                        symbol=event.symbol,
                    )
                    continue
                if fired:
                    triggered += 1
                else:
                    skipped += 1

            if triggered:
                await session.commit()

        self.runs_completed += 1
        self.theses_triggered += triggered
        self.last_run_at = datetime.now(UTC)
        if triggered:
            self.last_trigger_at = self.last_run_at
        if triggered or failures:
            _log.info(
                "catalyst_scheduler_tick",
                triggered=triggered,
                skipped=skipped,
                failures=failures,
                candidates=len(candidates),
            )
        return ScheduleResult(
            triggered=triggered,
            skipped=skipped,
            failures=failures,
            candidates=len(candidates),
        )

    async def _trigger_one(self, session: AsyncSession, event: CatalystEvent) -> bool:
        """Run the copilot for one catalyst and persist the thesis.

        Returns True if a thesis was written (and the event was transitioned
        to TRIGGERED); False if the copilot raised a recoverable error and
        we'll retry next tick.
        """
        set_correlation_id(f"cat{event.id}")
        prompt = _build_catalyst_prompt(event)
        try:
            run = await self._copilot.generate(
                prompt,
                risk_budget_usd=self._risk_budget,
                source_bucket="catalyst",
            )
        except CopilotError as e:
            _log.warning(
                "catalyst_copilot_recoverable_failure",
                event_id=event.id,
                symbol=event.symbol,
                error=str(e),
            )
            return False

        thesis_id = await write_thesis(run.thesis)
        event.state = CatalystEventState.TRIGGERED.value
        event.thesis_id = thesis_id
        event.triggered_at = datetime.now(UTC)

        # Hook the paper engine (mirrors what the reactive runner does so
        # catalyst theses contribute to the shadow portfolio too).
        try:
            from app.portfolio.engine import get_paper_engine

            await get_paper_engine().consider_thesis(thesis_id, run.thesis)
        except Exception:
            _log.exception(
                "catalyst_paper_engine_failed",
                event_id=event.id,
                thesis_id=thesis_id,
            )

        _log.info(
            "catalyst_thesis_fired",
            event_id=event.id,
            symbol=event.symbol,
            thesis_id=thesis_id,
            direction=run.thesis.direction,
            confidence=run.thesis.confidence,
        )
        return True
