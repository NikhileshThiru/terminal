"""AutonomousWorker — singleton that owns the always-on agent pipeline.

Wraps:
- the event bus consumer side (in-memory queue, or a Redis Streams consumer
  group in the docker-compose stack — see app/discovery/bus.py)
- ingestion (EDGAR poller + Alpaca news WS), either embedded in this process
  or running as the separate `ingestion` service (INGESTION_MODE=external),
  in which case its health comes from a Redis heartbeat
- the reactive runner (staleness gate → triage → copilot → persist)
- reconciliation (mark-to-market, outcome resolver) and the catalyst calendar

Autostarts with the API (`autonomous_autostart`); can be toggled via the UI.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from typing import Any

from app.agent.build import build_copilot
from app.core.config import get_settings
from app.core.logging import get_logger
from app.data.finnhub import FinnhubProvider
from app.data.registry import get_finnhub
from app.data.types import ProviderUnavailable, ProviderUnavailableReason
from app.discovery.bus import EventBus, build_event_bus
from app.discovery.catalyst_fetcher import CalendarFetcher
from app.discovery.catalyst_jobs import CatalystJobs
from app.discovery.catalyst_scheduler import CatalystScheduler
from app.discovery.ingest import IngestionService, read_status
from app.discovery.reactive import ReactiveRunner
from app.discovery.types import DiscoveryEvent
from app.llm.factory import get_router
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
    # Topology: which bus, and whether ingestion is in-process or its own service.
    event_bus: str
    ingestion_mode: str
    # embedded: always "embedded". external: "up" while heartbeats arrive, else "down".
    ingestion_state: str
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
    events_stale: int
    events_skipped_quota: int
    events_skipped_thesis_cap: int
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
    # connected | reconnecting | auth_failed | stopped | dead | disabled
    news_stream_state: str
    news_stream_last_error: str | None
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


class AutonomousWorker:
    """Holds + controls the discovery pipeline. Singleton; access via `get_worker()`."""

    def __init__(self) -> None:
        self._state: WorkerState = WorkerState.STOPPED
        self._started_at: datetime | None = None
        self._stopped_at: datetime | None = None
        self._bus: EventBus | None = None
        # Embedded ingestion (INGESTION_MODE=embedded) ...
        self._ingestion: IngestionService | None = None
        # ... or the external service's last Redis heartbeat (external mode).
        self._ingest_snapshot: dict[str, Any] | None = None
        self._ingest_monitor: asyncio.Task[None] | None = None
        self._runner: ReactiveRunner | None = None
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
            self._bus = build_event_bus()
            self._triage_llm = get_router("triage")
            copilot = build_copilot()
            from app.eval.persistence import get_session_factory

            self._runner = ReactiveRunner(
                bus=self._bus,
                copilot=copilot,
                triage_llm=self._triage_llm,
                triage_model=settings.llm_triage_model,
                risk_budget_usd=settings.autonomous_risk_budget_usd,
                # Persist every triage decision to the DB so the News pane
                # survives backend restarts and so we can re-query by symbol.
                session_factory=get_session_factory(),
                enforce_market_hours=settings.market_hours_gate,
                max_theses_per_day=settings.autonomous_max_reactive_theses_per_day,
                max_event_age_seconds=settings.reactive_max_event_age_seconds,
            )
            if settings.ingestion_mode == "embedded":
                self._ingestion = IngestionService(self._bus)
                await self._ingestion.start()
            else:
                self._ingest_monitor = asyncio.create_task(self._watch_ingestion())
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
                self._finnhub = get_finnhub()
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
                        enforce_market_hours=settings.market_hours_gate,
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
                event_bus=settings.event_bus,
                ingestion_mode=settings.ingestion_mode,
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
        if self._ingestion is not None:
            try:
                await self._ingestion.stop()
            except Exception as e:  # pragma: no cover
                _log.exception("worker_ingestion_teardown_failed", error=str(e))
        if self._ingest_monitor is not None:
            self._ingest_monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._ingest_monitor
        # NB: providers come from the shared registry (app/data/registry.py);
        # closing them here would break the copilot's research tools.
        self._ingestion = None
        self._ingest_monitor = None
        self._ingest_snapshot = None
        self._runner = None
        self._bus = None
        self._triage_llm = None
        self._finnhub = None
        self._reconciliation = None
        self._catalyst = None

    async def _watch_ingestion(self) -> None:
        """External mode: mirror the ingestion service's Redis heartbeat."""
        from redis.asyncio import Redis

        redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
        try:
            while True:
                try:
                    self._ingest_snapshot = await read_status(redis)
                except Exception as e:
                    self._ingest_snapshot = None
                    _log.warning("ingestion_status_read_failed", error=str(e)[:200])
                await asyncio.sleep(5.0)
        finally:
            with contextlib.suppress(Exception):
                await redis.aclose()

    def _ingest_view(self) -> tuple[str, dict[str, Any]]:
        """(ingestion_state, snapshot) — the same shape in both modes."""
        if self._ingestion is not None:
            return "embedded", self._ingestion.snapshot()
        if get_settings().ingestion_mode == "external" and self.is_running:
            snap = self._ingest_snapshot
            return ("up", snap) if snap is not None else ("down", {})
        return "stopped", {}

    def status(self) -> WorkerStatus:
        settings = get_settings()
        runner_stats = self._runner.stats if self._runner is not None else None
        ingestion_state, ing = self._ingest_view()
        return WorkerStatus(
            state=self._state,
            event_bus=settings.event_bus,
            ingestion_mode=settings.ingestion_mode,
            ingestion_state=ingestion_state,
            started_at=self._started_at,
            stopped_at=self._stopped_at,
            watchlist=list(settings.watchlist),
            poll_interval_seconds=(
                ing.get("poll_interval_seconds") or settings.discovery_poll_interval_seconds
            ),
            triage_provider=settings.llm_triage_provider,
            triage_model=settings.llm_triage_model,
            thesis_provider=settings.llm_thesis_provider,
            thesis_model=settings.llm_thesis_model,
            polls_completed=int(ing.get("polls_completed") or 0),
            events_published=int(ing.get("events_published") or 0),
            events_consumed=runner_stats.events_consumed if runner_stats else 0,
            events_passed_triage=runner_stats.events_passed_triage if runner_stats else 0,
            events_stale=runner_stats.events_stale if runner_stats else 0,
            events_skipped_quota=runner_stats.events_skipped_quota if runner_stats else 0,
            events_skipped_thesis_cap=(
                runner_stats.events_skipped_thesis_cap if runner_stats else 0
            ),
            theses_produced=runner_stats.theses_produced if runner_stats else 0,
            triage_failures=runner_stats.triage_failures if runner_stats else 0,
            thesis_failures=runner_stats.thesis_failures if runner_stats else 0,
            persistence_failures=runner_stats.persistence_failures if runner_stats else 0,
            queue_depth=self._bus.qsize() if self._bus else 0,
            last_event_at=runner_stats.last_event_at if runner_stats else None,
            last_thesis_at=runner_stats.last_thesis_at if runner_stats else None,
            last_poll_at=_parse_ts(ing.get("last_poll_at")),
            last_error=(runner_stats.last_error if runner_stats else None)
            or ing.get("poller_last_error"),
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
            news_stream_connected=bool(ing.get("news_connected")),
            news_stream_state=str(ing.get("news_state") or "disabled"),
            news_stream_last_error=ing.get("news_last_error"),
            news_stream_connects=int(ing.get("news_connects") or 0),
            news_stream_messages=int(ing.get("news_messages") or 0),
            news_stream_events_published=int(ing.get("news_events_published") or 0),
            news_stream_duplicates_dropped=int(ing.get("news_duplicates_dropped") or 0),
            last_news_message_at=_parse_ts(ing.get("last_news_message_at")),
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


def _parse_ts(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


@lru_cache(maxsize=1)
def get_worker() -> AutonomousWorker:
    """Singleton accessor — same worker instance across requests."""
    return AutonomousWorker()
