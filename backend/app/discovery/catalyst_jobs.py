"""CatalystJobs — periodic CalendarFetcher + CatalystScheduler loops.

Sibling of ReconciliationJobs: two periodic background loops behind a single
start/stop API, so AutonomousWorker composes them the same way it composes
the EdgarPoller and ReactiveRunner. Pulled out as its own class so tests can
stub it with a no-op (matches the existing pattern).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.core.logging import get_logger
from app.discovery.catalyst_fetcher import CalendarFetcher
from app.discovery.catalyst_scheduler import CatalystScheduler

_log = get_logger(__name__)


class CatalystJobs:
    """CalendarFetcher loop + CatalystScheduler loop, joined by one stop event."""

    def __init__(
        self,
        *,
        fetcher: CalendarFetcher,
        scheduler: CatalystScheduler,
        fetcher_interval_seconds: float,
        scheduler_interval_seconds: float,
    ) -> None:
        self._fetcher = fetcher
        self._scheduler = scheduler
        self._fetcher_interval = fetcher_interval_seconds
        self._scheduler_interval = scheduler_interval_seconds

        self._stop_event = asyncio.Event()
        self._fetcher_task: asyncio.Task[None] | None = None
        self._scheduler_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._stop_event.clear()
        self._fetcher_task = asyncio.create_task(
            self._run_periodic(
                name="catalyst-fetcher",
                job=self._tick_fetcher,
                interval_seconds=self._fetcher_interval,
            )
        )
        self._scheduler_task = asyncio.create_task(
            self._run_periodic(
                name="catalyst-scheduler",
                job=self._tick_scheduler,
                interval_seconds=self._scheduler_interval,
            )
        )
        _log.info(
            "catalyst_jobs_started",
            fetcher_interval=self._fetcher_interval,
            scheduler_interval=self._scheduler_interval,
        )

    async def stop(self) -> None:
        self._stop_event.set()
        for task in (self._fetcher_task, self._scheduler_task):
            if task is None:
                continue
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except TimeoutError:
                task.cancel()
            except asyncio.CancelledError:
                pass
        self._fetcher_task = None
        self._scheduler_task = None
        _log.info("catalyst_jobs_stopped")

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
                    _log.exception("catalyst_job_failed", job=name, error=str(e))
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=interval_seconds)
                except TimeoutError:
                    continue
                else:
                    break
        except asyncio.CancelledError:
            _log.info("catalyst_job_cancelled", job=name)

    async def _tick_fetcher(self) -> None:
        await self._fetcher.run_once()

    async def _tick_scheduler(self) -> None:
        await self._scheduler.run_once()

    # === Counters surfaced via worker.status() ===

    @property
    def fetcher_runs_completed(self) -> int:
        return self._fetcher.runs_completed

    @property
    def events_upserted_total(self) -> int:
        return self._fetcher.events_upserted_total

    @property
    def last_fetcher_at(self) -> datetime | None:
        return self._fetcher.last_run_at

    @property
    def scheduler_runs_completed(self) -> int:
        return self._scheduler.runs_completed

    @property
    def theses_triggered(self) -> int:
        return self._scheduler.theses_triggered

    @property
    def last_scheduler_at(self) -> datetime | None:
        return self._scheduler.last_run_at

    @property
    def last_trigger_at(self) -> datetime | None:
        return self._scheduler.last_trigger_at


__all__ = ["CatalystJobs", "_log"]


# Touch the typed datetime import — keeps `UTC` warning-free for some linters.
_ = UTC
