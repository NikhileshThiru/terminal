"""ReconciliationJobs — owns the periodic MTM + outcome-resolver loops.

Sits next to the EdgarPoller as a "long-running background sub-component" of
the AutonomousWorker. Same start/stop shape so worker.py composes both the
same way. Pulled out as its own class (rather than inline in worker.py) so
tests can stub it cleanly with a no-op, matching how EdgarPoller and
ReactiveRunner are stubbed today.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.core.logging import get_logger
from app.portfolio.mtm import MtMService
from app.portfolio.resolver import OutcomeResolver

_log = get_logger(__name__)


class ReconciliationJobs:
    """Two periodic background loops: mark-to-market + outcome resolution."""

    def __init__(
        self,
        *,
        mtm: MtMService,
        resolver: OutcomeResolver,
        mtm_interval_seconds: float,
        resolver_interval_seconds: float,
    ) -> None:
        self._mtm = mtm
        self._resolver = resolver
        self._mtm_interval = mtm_interval_seconds
        self._resolver_interval = resolver_interval_seconds

        self._stop_event = asyncio.Event()
        self._mtm_task: asyncio.Task[None] | None = None
        self._resolver_task: asyncio.Task[None] | None = None

        # Counters surfaced via worker.status().
        self.mtm_ticks_completed = 0
        self.mtm_marks_written = 0
        self.resolver_ticks_completed = 0
        self.resolver_outcomes_written = 0
        self.last_mtm_at: datetime | None = None
        self.last_resolver_at: datetime | None = None

    async def start(self) -> None:
        self._stop_event.clear()
        self._mtm_task = asyncio.create_task(
            self._run_periodic(name="mtm", job=self._tick_mtm, interval_seconds=self._mtm_interval)
        )
        self._resolver_task = asyncio.create_task(
            self._run_periodic(
                name="resolver",
                job=self._tick_resolver,
                interval_seconds=self._resolver_interval,
            )
        )
        _log.info(
            "reconciliation_jobs_started",
            mtm_interval=self._mtm_interval,
            resolver_interval=self._resolver_interval,
        )

    async def stop(self) -> None:
        self._stop_event.set()
        for task in (self._mtm_task, self._resolver_task):
            if task is None:
                continue
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except TimeoutError:
                task.cancel()
            except asyncio.CancelledError:
                pass
        self._mtm_task = None
        self._resolver_task = None
        _log.info("reconciliation_jobs_stopped")

    async def _run_periodic(
        self,
        *,
        name: str,
        job: Callable[[], Awaitable[None]],
        interval_seconds: float,
    ) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    await job()
                except Exception as e:
                    _log.exception("reconciliation_job_failed", job=name, error=str(e))
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=interval_seconds)
                except TimeoutError:
                    continue
                else:
                    break
        except asyncio.CancelledError:
            # Loop is being torn down. Don't propagate — stop() is the API.
            _log.info("reconciliation_job_cancelled", job=name)

    async def _tick_mtm(self) -> None:
        result = await self._mtm.mark_all_open()
        self.mtm_ticks_completed += 1
        self.mtm_marks_written += result.marks_written
        self.last_mtm_at = datetime.now(UTC)

    async def _tick_resolver(self) -> None:
        result = await self._resolver.resolve_all()
        self.resolver_ticks_completed += 1
        self.resolver_outcomes_written += result.outcomes_written
        self.last_resolver_at = datetime.now(UTC)
