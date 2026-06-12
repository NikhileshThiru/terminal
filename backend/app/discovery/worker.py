"""AutonomousWorker — singleton that owns the always-on discovery + reactive pipeline.

Wraps:
- the in-memory event bus
- the EDGAR poller (Phase 6 starts with EDGAR only; Alpaca news WebSocket
  is the next-up source)
- the reactive runner (triage → copilot → persist)

Default state: STOPPED. User explicitly toggles on via the UI / API.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from typing import Any

from app.agent.build import build_copilot
from app.core.config import get_settings
from app.core.logging import get_logger
from app.data.edgar import EdgarFilingsProvider
from app.data.finnhub import FinnhubProvider
from app.data.types import ProviderUnavailable, ProviderUnavailableReason
from app.discovery.alpaca_news_stream import AlpacaNewsStream
from app.discovery.bus import EventBus, InMemoryEventBus
from app.discovery.catalyst_fetcher import CalendarFetcher
from app.discovery.catalyst_jobs import CatalystJobs
from app.discovery.catalyst_scheduler import CatalystScheduler
from app.discovery.edgar_poller import EdgarPoller
from app.discovery.reactive import ReactiveRunner
from app.discovery.types import DiscoveryEvent
from app.llm.gemini import GeminiProvider
from app.llm.groq import GroqProvider
from app.llm.interface import LLMProvider
from app.portfolio.jobs import ReconciliationJobs
from app.portfolio.mtm import get_mtm_service
from app.portfolio.resolver import get_outcome_resolver

_log = get_logger(__name__)


class WorkerState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


@dataclass
class WorkerStatus:
    state: WorkerState
    started_at: datetime | None
    stopped_at: datetime | None
    watchlist: list[str]
    poll_interval_seconds: float
    triage_provider: str
    triage_model: str
    thesis_provider: str
    thesis_model: str
    polls_completed: int
    events_published: int
    events_consumed: int
    events_passed_triage: int
    theses_produced: int
    triage_failures: int
    thesis_failures: int
    persistence_failures: int
    queue_depth: int
    last_event_at: datetime | None
    last_thesis_at: datetime | None
    last_poll_at: datetime | None
    last_error: str | None
    recent_triage_decisions: list[dict[str, Any]]
    # Reconciliation (Phase 7+).
    mtm_ticks_completed: int
    mtm_marks_written: int
    resolver_ticks_completed: int
    resolver_outcomes_written: int
    last_mtm_at: datetime | None
    last_resolver_at: datetime | None
    # Alpaca news WS (Step 4 universe ingestion).
    news_stream_connected: bool
    news_stream_connects: int
    news_stream_messages: int
    news_stream_events_published: int
    news_stream_duplicates_dropped: int
    last_news_message_at: datetime | None
    # Catalyst calendar (Step 5, DESIGN.md §8).
    catalyst_fetcher_runs: int
    catalyst_events_upserted: int
    catalyst_scheduler_runs: int
    catalyst_theses_triggered: int
    last_catalyst_fetch_at: datetime | None
    last_catalyst_trigger_at: datetime | None


def _build_triage_llm(provider: str) -> LLMProvider:
    if provider == "gemini":
        return GeminiProvider()
    if provider == "groq":
        return GroqProvider()
    raise ProviderUnavailable(
        reason=ProviderUnavailableReason.NOT_IMPLEMENTED,
        message=f"Triage provider {provider!r} not supported (use 'gemini' or 'groq').",
        provider=provider,
        retryable=False,
    )


class AutonomousWorker:
    """Holds + controls the discovery pipeline. Singleton; access via `get_worker()`."""

    def __init__(self) -> None:
        self._state: WorkerState = WorkerState.STOPPED
        self._started_at: datetime | None = None
        self._stopped_at: datetime | None = None
        self._bus: EventBus | None = None
        self._poller: EdgarPoller | None = None
        self._news_stream: AlpacaNewsStream | None = None
        self._runner: ReactiveRunner | None = None
        self._edgar: EdgarFilingsProvider | None = None
        self._triage_llm: LLMProvider | None = None
        self._finnhub: FinnhubProvider | None = None
        # Reconciliation jobs (Phase 7+, DESIGN.md §8).
        self._reconciliation: ReconciliationJobs | None = None
        # Catalyst calendar jobs (Step 5, DESIGN.md §8).
        self._catalyst: CatalystJobs | None = None

    @property
    def state(self) -> WorkerState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state == WorkerState.RUNNING

    async def start(self) -> None:
        if self._state in (WorkerState.RUNNING, WorkerState.STARTING):
            return
        settings = get_settings()
        self._state = WorkerState.STARTING
        try:
            self._bus = InMemoryEventBus(maxsize=1000)
            self._edgar = EdgarFilingsProvider()
            self._triage_llm = _build_triage_llm(settings.llm_triage_provider)
            copilot = build_copilot()
            from app.eval.persistence import get_session_factory

            self._poller = EdgarPoller(
                filings=self._edgar,
                bus=self._bus,
                poll_interval_seconds=settings.discovery_poll_interval_seconds,
                # High-volume forms (Form 4 insider trades) are filtered to
                # the watchlist so the triage budget isn't eaten by routine
                # insider filings from no-name issuers.
                watchlist=settings.watchlist,
                session_factory=get_session_factory(),
            )
            # Universe-wide news. Skipped silently if Alpaca creds aren't set —
            # the poller alone is enough to keep the pipeline alive.
            try:
                self._news_stream = AlpacaNewsStream(
                    bus=self._bus,
                    session_factory=get_session_factory(),
                )
            except ProviderUnavailable as e:
                if e.reason == ProviderUnavailableReason.AUTH_MISSING:
                    _log.warning(
                        "alpaca_news_stream_disabled",
                        reason="ALPACA_API_KEY/SECRET not set",
                    )
                    self._news_stream = None
                else:
                    raise
            self._runner = ReactiveRunner(
                bus=self._bus,
                copilot=copilot,
                triage_llm=self._triage_llm,
                triage_model=settings.llm_triage_model,
                risk_budget_usd=settings.autonomous_risk_budget_usd,
                # Persist every triage decision to the DB so the News pane
                # survives backend restarts and so we can re-query by symbol.
                session_factory=get_session_factory(),
            )
            await self._poller.start()
            if self._news_stream is not None:
                await self._news_stream.start()
            await self._runner.start()

            # Reconciliation jobs — owned here so they share lifecycle with
            # the discovery pipeline. Both are idempotent + failure-isolated.
            self._reconciliation = ReconciliationJobs(
                mtm=get_mtm_service(),
                resolver=get_outcome_resolver(),
                mtm_interval_seconds=settings.mtm_interval_seconds,
                resolver_interval_seconds=settings.resolver_interval_seconds,
            )
            await self._reconciliation.start()

            # Catalyst calendar — fetches upcoming earnings + pre-positions
            # theses on them. Skipped silently if Finnhub auth isn't set so the
            # rest of the pipeline still works.
            try:
                self._finnhub = FinnhubProvider()
                self._catalyst = CatalystJobs(
                    fetcher=CalendarFetcher(
                        finnhub=self._finnhub,
                        session_factory=get_session_factory(),
                        universe=settings.watchlist,
                        horizon_days=settings.catalyst_horizon_days,
                    ),
                    scheduler=CatalystScheduler(
                        copilot=copilot,
                        session_factory=get_session_factory(),
                        lead_days=settings.catalyst_lead_days,
                        risk_budget_usd=settings.autonomous_risk_budget_usd,
                    ),
                    fetcher_interval_seconds=settings.catalyst_fetcher_interval_seconds,
                    scheduler_interval_seconds=settings.catalyst_scheduler_interval_seconds,
                )
                await self._catalyst.start()
            except ProviderUnavailable as e:
                if e.reason == ProviderUnavailableReason.AUTH_MISSING:
                    _log.warning(
                        "catalyst_jobs_disabled",
                        reason="FINNHUB_API_KEY not set",
                    )
                    self._finnhub = None
                    self._catalyst = None
                else:
                    raise

            self._started_at = datetime.now(UTC)
            self._stopped_at = None
            self._state = WorkerState.RUNNING
            _log.info(
                "autonomous_worker_started",
                watchlist=settings.watchlist,
                triage_provider=settings.llm_triage_provider,
                thesis_provider=settings.llm_thesis_provider,
                mtm_interval=settings.mtm_interval_seconds,
                resolver_interval=settings.resolver_interval_seconds,
            )
        except Exception:
            self._state = WorkerState.STOPPED
            await self._teardown()
            raise

    async def stop(self) -> None:
        if self._state in (WorkerState.STOPPED, WorkerState.STOPPING):
            return
        self._state = WorkerState.STOPPING
        await self._teardown()
        self._stopped_at = datetime.now(UTC)
        self._state = WorkerState.STOPPED
        _log.info("autonomous_worker_stopped")

    async def inject(self, event: DiscoveryEvent) -> None:
        """Push a synthetic event onto the bus. Used by the admin /autonomous/inject
        endpoint so the demo + tests can fire a fake earnings beat / 8-K without
        waiting for a real one to arrive. Worker must be running."""
        if self._bus is None or not self.is_running:
            raise RuntimeError("autonomous worker is not running; start it before injecting")
        await self._bus.publish(event)
        _log.info(
            "synthetic_event_injected",
            event_id=event.id,
            source=event.source,
            symbols=event.symbols,
        )

    async def _teardown(self) -> None:
        if self._catalyst is not None:
            try:
                await self._catalyst.stop()
            except Exception as e:  # pragma: no cover
                _log.exception("worker_catalyst_teardown_failed", error=str(e))
        if self._reconciliation is not None:
            try:
                await self._reconciliation.stop()
            except Exception as e:  # pragma: no cover
                _log.exception("worker_reconciliation_teardown_failed", error=str(e))
        if self._runner is not None:
            try:
                await self._runner.stop()
            except Exception as e:  # pragma: no cover
                _log.exception("worker_runner_teardown_failed", error=str(e))
        if self._news_stream is not None:
            try:
                await self._news_stream.stop()
            except Exception as e:  # pragma: no cover
                _log.exception("worker_news_stream_teardown_failed", error=str(e))
        if self._poller is not None:
            try:
                await self._poller.stop()
            except Exception as e:  # pragma: no cover
                _log.exception("worker_poller_teardown_failed", error=str(e))
        if self._edgar is not None:
            with contextlib.suppress(Exception):  # pragma: no cover
                await self._edgar.aclose()
        self._poller = None
        self._news_stream = None
        self._runner = None
        self._edgar = None
        self._bus = None
        self._triage_llm = None
        self._finnhub = None
        self._reconciliation = None
        self._catalyst = None

    def status(self) -> WorkerStatus:
        settings = get_settings()
        runner_stats = self._runner.stats if self._runner is not None else None
        return WorkerStatus(
            state=self._state,
            started_at=self._started_at,
            stopped_at=self._stopped_at,
            watchlist=list(settings.watchlist),
            poll_interval_seconds=(
                self._poller.poll_interval
                if self._poller
                else settings.discovery_poll_interval_seconds
            ),
            triage_provider=settings.llm_triage_provider,
            triage_model=settings.llm_triage_model,
            thesis_provider=settings.llm_thesis_provider,
            thesis_model=settings.llm_thesis_model,
            polls_completed=self._poller.polls_completed if self._poller else 0,
            events_published=self._poller.events_published if self._poller else 0,
            events_consumed=runner_stats.events_consumed if runner_stats else 0,
            events_passed_triage=runner_stats.events_passed_triage if runner_stats else 0,
            theses_produced=runner_stats.theses_produced if runner_stats else 0,
            triage_failures=runner_stats.triage_failures if runner_stats else 0,
            thesis_failures=runner_stats.thesis_failures if runner_stats else 0,
            persistence_failures=runner_stats.persistence_failures if runner_stats else 0,
            queue_depth=self._bus.qsize() if self._bus else 0,
            last_event_at=runner_stats.last_event_at if runner_stats else None,
            last_thesis_at=runner_stats.last_thesis_at if runner_stats else None,
            last_poll_at=self._poller.last_poll_at if self._poller else None,
            last_error=(runner_stats.last_error if runner_stats else None)
            or (self._poller.last_error if self._poller else None),
            recent_triage_decisions=(
                list(runner_stats.last_triage_decisions) if runner_stats else []
            ),
            mtm_ticks_completed=(
                self._reconciliation.mtm_ticks_completed if self._reconciliation else 0
            ),
            mtm_marks_written=(
                self._reconciliation.mtm_marks_written if self._reconciliation else 0
            ),
            resolver_ticks_completed=(
                self._reconciliation.resolver_ticks_completed if self._reconciliation else 0
            ),
            resolver_outcomes_written=(
                self._reconciliation.resolver_outcomes_written if self._reconciliation else 0
            ),
            last_mtm_at=(self._reconciliation.last_mtm_at if self._reconciliation else None),
            last_resolver_at=(
                self._reconciliation.last_resolver_at if self._reconciliation else None
            ),
            news_stream_connected=(self._news_stream.connected if self._news_stream else False),
            news_stream_connects=(self._news_stream.connects if self._news_stream else 0),
            news_stream_messages=(self._news_stream.messages_received if self._news_stream else 0),
            news_stream_events_published=(
                self._news_stream.events_published if self._news_stream else 0
            ),
            news_stream_duplicates_dropped=(
                self._news_stream.duplicates_dropped if self._news_stream else 0
            ),
            last_news_message_at=(self._news_stream.last_message_at if self._news_stream else None),
            catalyst_fetcher_runs=(self._catalyst.fetcher_runs_completed if self._catalyst else 0),
            catalyst_events_upserted=(
                self._catalyst.events_upserted_total if self._catalyst else 0
            ),
            catalyst_scheduler_runs=(
                self._catalyst.scheduler_runs_completed if self._catalyst else 0
            ),
            catalyst_theses_triggered=(self._catalyst.theses_triggered if self._catalyst else 0),
            last_catalyst_fetch_at=(self._catalyst.last_fetcher_at if self._catalyst else None),
            last_catalyst_trigger_at=(self._catalyst.last_trigger_at if self._catalyst else None),
        )


@lru_cache(maxsize=1)
def get_worker() -> AutonomousWorker:
    """Singleton accessor — same worker instance across requests."""
    return AutonomousWorker()
