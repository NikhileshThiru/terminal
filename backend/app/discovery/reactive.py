"""Reactive runner — consumes discovery events, triages, generates theses.

This is the autonomous half of the funnel (DESIGN.md §4). Same agent loop
the manual copilot uses, just triggered by news instead of by the user.

Failure isolation: every exception in one event's processing is caught and
logged so a bad event doesn't take the whole loop down. The worker keeps
running until explicitly stopped.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.agent.copilot import Copilot, CopilotError, CopilotRun
from app.core.correlation import set_correlation_id
from app.core.logging import get_logger
from app.core.market_hours import eastern_day_start, is_market_open
from app.data.types import ProviderUnavailable, ProviderUnavailableReason
from app.discovery.bus import EventBus
from app.discovery.triage import TriageDecision, triage
from app.discovery.types import DiscoveryEvent
from app.eval.persistence import write_thesis
from app.llm.interface import LLMProvider

try:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.data.upsert import dialect_insert
    from app.discovery.models import TriageDecisionRow
except ImportError:  # pragma: no cover — defensive
    async_sessionmaker = None  # type: ignore[assignment,misc]

_log = get_logger(__name__)


@dataclass
class ReactiveStats:
    events_consumed: int = 0
    events_off_session: int = 0
    events_passed_triage: int = 0
    events_skipped_quota: int = 0
    events_stale: int = 0
    synthetic_dry_runs: int = 0
    events_skipped_thesis_cap: int = 0
    theses_produced: int = 0
    triage_failures: int = 0
    thesis_failures: int = 0
    persistence_failures: int = 0
    last_event_at: datetime | None = None
    last_thesis_at: datetime | None = None
    last_error: str | None = None
    last_triage_decisions: list[dict[str, object]] = field(default_factory=list)


def _format_event_for_copilot(event: DiscoveryEvent, triage_reason: str) -> str:
    """Turn a discovery event into a user-thesis-style prompt for the copilot."""
    sym = event.symbols[0] if event.symbols else "the affected symbol"
    other_syms = f" (also tagged: {', '.join(event.symbols[1:])})" if len(event.symbols) > 1 else ""
    pieces = [
        f"AUTONOMOUS TRIGGER: a {event.kind} event arrived from {event.source}.",
        f"Symbol: {sym}{other_syms}",
        f"Headline: {event.headline}",
    ]
    if event.body:
        body_excerpt = event.body[:1000]
        pieces.append(f"Body: {body_excerpt}")
    pieces.append(f"Published at: {event.published_at.isoformat()}")
    pieces.append(f"Triage rationale: {triage_reason}")
    pieces.append("")
    pieces.append(
        "Research this thesis: is this news material enough to take a directional "
        "options position, and if so, what's the specific trade?"
    )
    return "\n".join(pieces)


class ReactiveRunner:
    """Long-running consumer of discovery events → autonomous theses."""

    MAX_RECENT_DECISIONS = 20

    def __init__(
        self,
        *,
        bus: EventBus,
        copilot: Copilot,
        triage_llm: LLMProvider,
        triage_model: str,
        risk_budget_usd: float | None = 500.0,
        session_factory: object | None = None,
        enforce_market_hours: bool = False,
        max_theses_per_day: int | None = None,
        max_event_age_seconds: float | None = None,
    ) -> None:
        self._bus = bus
        self._copilot = copilot
        self._triage_llm = triage_llm
        self._triage_model = triage_model
        self._risk_budget = risk_budget_usd
        # Prod wiring (worker.py) passes settings.market_hours_gate; the
        # default stays off so tests and ad-hoc runs aren't wall-clock-dependent.
        self._enforce_market_hours = enforce_market_hours
        # Thesis pacing (config: autonomous_max_reactive_theses_per_day).
        # None = unbounded (tests / ad-hoc runs).
        self._max_theses_per_day = max_theses_per_day
        self._theses_by_day: dict[datetime, int] = {}  # ET-day start -> count
        # Staleness gate (config: reactive_max_event_age_seconds). None = off.
        self._max_event_age = max_event_age_seconds
        # Optional DB sink so triage decisions survive a backend restart.
        # If absent, decisions live only in the in-memory buffer (legacy /
        # tests).
        self._session_factory = session_factory
        self._task: asyncio.Task[None] | None = None
        self._persist_tasks: set[asyncio.Task[None]] = set()
        self._stop_event = asyncio.Event()
        self.stats = ReactiveStats()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())
        _log.info("reactive_runner_started")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                self._task.cancel()
        self._task = None
        _log.info("reactive_runner_stopped")

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                # Wait for next event OR a stop signal.
                consume_task = asyncio.create_task(self._bus.consume())
                stop_task = asyncio.create_task(self._stop_event.wait())
                done, pending = await asyncio.wait(
                    {consume_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()
                if stop_task in done:
                    break
                event: DiscoveryEvent = consume_task.result()
            except Exception as e:
                self.stats.last_error = f"{type(e).__name__}: {e}"
                _log.exception("reactive_consume_failed", error=str(e))
                continue

            try:
                await self._process_one(event)
            finally:
                # ACK only after processing: with the Redis bus, an event the
                # worker died on stays pending and is re-delivered on restart.
                try:
                    await self._bus.ack(event)
                except Exception as e:
                    _log.warning("reactive_ack_failed", event_id=event.id, error=str(e)[:200])

    async def _process_one(self, event: DiscoveryEvent) -> CopilotRun | None:
        self.stats.events_consumed += 1
        self.stats.last_event_at = datetime.now(UTC)
        # Each event gets its own correlation id for end-to-end tracing.
        set_correlation_id(event.id.replace("-", "")[:16] or None)

        # === Market-hours gate (CLAUDE.md §7) ===
        # Off-session events are dropped BEFORE triage: a thesis generated at
        # 3am Sunday would be graded against stale quotes (eval pollution) and
        # the drop also spares overnight LLM quota. The decision is recorded
        # so the News pane shows why the event produced nothing.
        if self._enforce_market_hours and not is_market_open(datetime.now(UTC)):
            self.stats.events_off_session += 1
            self._record_recent_decision(
                event,
                TriageDecision(
                    passed=False,
                    reason="off-session: US equity market closed (market-hours gate)",
                    confidence=1.0,
                ),
            )
            _log.info(
                "reactive_event_off_session",
                event_id=event.id,
                symbol=(event.symbols[0] if event.symbols else None),
                headline=event.headline[:120],
            )
            return None

        # === Staleness gate ===
        # A backlog (worker restarted after ingestion kept publishing) is not
        # worth triaging: the reactive path's value is minutes-fresh news.
        if self._max_event_age is not None and event.ingested_at is not None:
            age = (datetime.now(UTC) - event.ingested_at).total_seconds()
            if age > self._max_event_age:
                self.stats.events_stale += 1
                self._record_recent_decision(
                    event,
                    TriageDecision(
                        passed=False,
                        reason=f"skipped: stale — waited {age / 60:.0f} min on the bus",
                        confidence=1.0,
                    ),
                )
                return None

        # === Triage ===
        try:
            decision = await triage(event, llm=self._triage_llm, model=self._triage_model)
        except ProviderUnavailable as e:
            self.stats.triage_failures += 1
            self.stats.last_error = f"triage: {e.reason.value}: {e}"
            if e.reason == ProviderUnavailableReason.RATE_LIMITED:
                # Every triage route is out of quota. Record a visible skip so
                # the News pane says *why* this event produced nothing, rather
                # than the event silently vanishing (the July 2026 failure).
                self.stats.events_skipped_quota += 1
                self._record_recent_decision(
                    event,
                    TriageDecision(
                        passed=False,
                        reason="skipped: all triage LLM routes rate-limited (free-tier quota)",
                        confidence=0.0,
                    ),
                )
            _log.warning("reactive_triage_failed", event_id=event.id, error=str(e))
            return None
        except Exception as e:
            self.stats.triage_failures += 1
            self.stats.last_error = f"triage: {type(e).__name__}: {e}"
            _log.warning("reactive_triage_failed", event_id=event.id, error=str(e))
            return None

        if not decision.passed:
            self._record_recent_decision(event, decision)
            return None

        self.stats.events_passed_triage += 1

        # === Thesis pacing ===
        if self._max_theses_per_day is not None:
            produced_today = await self._reactive_theses_today()
            if produced_today >= self._max_theses_per_day:
                self.stats.events_skipped_thesis_cap += 1
                self._record_recent_decision(
                    event,
                    decision.model_copy(
                        update={
                            "reason": (
                                f"passed triage, skipped: daily reactive thesis cap "
                                f"({self._max_theses_per_day}) reached — {decision.reason}"
                            )[:300]
                        }
                    ),
                )
                _log.info(
                    "reactive_thesis_cap_reached",
                    event_id=event.id,
                    cap=self._max_theses_per_day,
                )
                return None

        self._record_recent_decision(event, decision)

        # === Thesis ===
        # Synthetic events (POST /autonomous/inject) run the full funnel so the
        # reasoning stream can be demoed, but are a DRY RUN: never persisted,
        # traded, or counted in the eval — fake news must not become data.
        synthetic = bool(event.payload.get("synthetic"))
        prompt = _format_event_for_copilot(event, decision.reason)
        try:
            run = await self._copilot.generate(
                prompt,
                risk_budget_usd=self._risk_budget,
                source_bucket="reactive",
                record_attempt=not synthetic,
            )
        except CopilotError as e:
            self.stats.thesis_failures += 1
            self.stats.last_error = f"copilot: {e}"
            _log.warning("reactive_thesis_failed", event_id=event.id, error=str(e))
            return None
        except Exception as e:
            self.stats.thesis_failures += 1
            self.stats.last_error = f"copilot unexpected: {type(e).__name__}: {e}"
            _log.exception("reactive_thesis_unexpected", event_id=event.id)
            return None

        if synthetic:
            self.stats.synthetic_dry_runs += 1
            _log.info(
                "reactive_synthetic_dry_run",
                event_id=event.id,
                symbol=run.thesis.symbol,
                direction=run.thesis.direction,
            )
            return run

        # === Persist ===
        thesis_id: int | None = None
        try:
            thesis_id = await write_thesis(run.thesis)
        except Exception as e:
            self.stats.persistence_failures += 1
            self.stats.last_error = f"persist: {type(e).__name__}: {e}"
            _log.exception("reactive_persist_failed", event_id=event.id)
            # The thesis ran successfully — return it even if persistence failed.

        # === Shadow-trade evaluation ===
        if thesis_id is not None:
            try:
                from app.portfolio.engine import get_paper_engine

                await get_paper_engine().consider_thesis(thesis_id, run.thesis)
            except Exception:
                _log.exception(
                    "reactive_paper_engine_failed",
                    event_id=event.id,
                    thesis_id=thesis_id,
                )

        self.stats.theses_produced += 1
        self.stats.last_thesis_at = datetime.now(UTC)
        day = eastern_day_start(self.stats.last_thesis_at)
        self._theses_by_day = {day: self._theses_by_day.get(day, 0) + 1}
        _log.info(
            "reactive_thesis_produced",
            event_id=event.id,
            symbol=run.thesis.symbol,
            direction=run.thesis.direction,
            confidence=run.thesis.confidence,
            grounded=run.thesis.grounding_check_passed,
        )
        return run

    async def _reactive_theses_today(self) -> int:
        """Reactive theses generated since midnight ET. DB-backed when wired so
        the cap survives restarts; otherwise this process's counter."""
        since = eastern_day_start(datetime.now(UTC))
        if self._session_factory is not None:
            try:
                from sqlalchemy import func, select

                from app.eval.models import Thesis as ThesisRow

                async with self._session_factory() as session:  # type: ignore[operator]
                    stmt = select(func.count(ThesisRow.id)).where(
                        ThesisRow.source_bucket == "reactive",
                        ThesisRow.generated_at >= since,
                    )
                    return int((await session.execute(stmt)).scalar_one())
            except Exception as e:
                _log.warning("reactive_thesis_count_failed", error=str(e)[:200])
        return self._theses_by_day.get(since, 0)

    def _record_recent_decision(self, event: DiscoveryEvent, decision: TriageDecision) -> None:
        now = datetime.now(UTC)
        body_excerpt = (event.body.strip()[:300] if event.body else None) or None
        record: dict[str, object] = {
            "event_id": event.id,
            "symbol": (event.symbols[0] if event.symbols else None),
            "headline": event.headline[:200],
            "body_excerpt": body_excerpt,
            "url": event.url,
            "passed": decision.passed,
            "reason": decision.reason,
            "confidence": decision.confidence,
            "at": now.isoformat(),
            "source": event.source,
            "kind": event.kind,
        }
        self.stats.last_triage_decisions.append(record)
        if len(self.stats.last_triage_decisions) > self.MAX_RECENT_DECISIONS:
            self.stats.last_triage_decisions = self.stats.last_triage_decisions[
                -self.MAX_RECENT_DECISIONS :
            ]
        # Persist for the News pane (survives restart, queryable by symbol).
        # Synthetic events stay in-memory only: the decisions table is the
        # replay log, and fake headlines don't belong in it.
        if event.payload.get("synthetic"):
            return
        # Async fire-and-forget; failures must not abort the funnel. We hold
        # a reference in self._persist_tasks so the task isn't GC'd mid-flight
        # (RUF006).
        if self._session_factory is not None:
            task = asyncio.create_task(self._persist_decision(event, decision, now))
            self._persist_tasks.add(task)
            task.add_done_callback(self._persist_tasks.discard)

    async def _persist_decision(
        self, event: DiscoveryEvent, decision: TriageDecision, decided_at: datetime
    ) -> None:
        if self._session_factory is None or TriageDecisionRow is None:
            return
        factory = self._session_factory  # narrow for mypy
        body_excerpt = None
        if event.body:
            body_excerpt = event.body.strip()[:1500] or None
        # Postgres + SQLite both support ON CONFLICT DO UPDATE; use the
        # dialect-specific helper so re-triages of the same event_id
        # overwrite rather than collide.
        url = event.url
        payload = {
            "event_id": event.id,
            "symbol": (event.symbols[0].upper() if event.symbols else None),
            "headline": event.headline[:300],
            "body_excerpt": body_excerpt,
            "url": url,
            "source": event.source,
            "kind": event.kind,
            "passed": decision.passed,
            "reason": decision.reason,
            "confidence": decision.confidence,
            "decided_at": decided_at,
            "published_at": event.published_at,
        }
        try:
            async with factory() as session:  # type: ignore[operator]
                update_cols = {k: v for k, v in payload.items() if k != "event_id"}
                insert = dialect_insert(session)
                stmt = insert(TriageDecisionRow).values(**payload)
                stmt = stmt.on_conflict_do_update(index_elements=["event_id"], set_=update_cols)
                await session.execute(stmt)
                await session.commit()
        except Exception as e:
            self.stats.persistence_failures += 1
            _log.warning(
                "triage_decision_persist_failed",
                event_id=event.id,
                error=str(e),
            )
