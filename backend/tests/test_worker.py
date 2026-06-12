"""AutonomousWorker lifecycle tests with stubbed dependencies."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from app.discovery.worker import AutonomousWorker, WorkerState


class _NoopPoller:
    polls_completed = 0
    events_published = 0
    last_poll_at = None
    last_error = None
    poll_interval = 300.0

    def __init__(self, *_: Any, **__: Any) -> None: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class _NoopRunner:
    class _Stats:
        events_consumed = 0
        events_passed_triage = 0
        theses_produced = 0
        triage_failures = 0
        thesis_failures = 0
        persistence_failures = 0
        last_event_at = None
        last_thesis_at = None
        last_error = None
        last_triage_decisions: ClassVar[list[dict[str, Any]]] = []

    stats = _Stats()

    def __init__(self, *_: Any, **__: Any) -> None: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class _NoopLLM:
    name = "noop"

    async def complete(self, **_: Any) -> Any: ...
    async def complete_structured(self, **_: Any) -> Any: ...
    async def step_agent(self, **_: Any) -> Any: ...


class _NoopEdgar:
    name = "noop-edgar"

    async def get_recent_filings(self, *_: Any, **__: Any) -> list:
        return []

    async def get_filing_text(self, _: str) -> str:
        return ""

    async def aclose(self) -> None: ...


class _NoopCopilot:
    async def generate(self, *_: Any, **__: Any) -> Any: ...


class _NoopReconciliation:
    mtm_ticks_completed = 0
    mtm_marks_written = 0
    resolver_ticks_completed = 0
    resolver_outcomes_written = 0
    last_mtm_at = None
    last_resolver_at = None

    def __init__(self, *_: Any, **__: Any) -> None: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class _NoopNewsStream:
    connected = False
    connects = 0
    messages_received = 0
    events_published = 0
    duplicates_dropped = 0
    last_message_at = None

    def __init__(self, *_: Any, **__: Any) -> None: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class _NoopCatalystJobs:
    fetcher_runs_completed = 0
    events_upserted_total = 0
    scheduler_runs_completed = 0
    theses_triggered = 0
    last_fetcher_at = None
    last_scheduler_at = None
    last_trigger_at = None

    def __init__(self, *_: Any, **__: Any) -> None: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


@pytest.fixture(autouse=True)
def _stub_components(monkeypatch: pytest.MonkeyPatch):
    """Swap out the real provider/copilot/poller/runner factories with no-ops."""
    monkeypatch.setattr("app.discovery.worker.EdgarPoller", _NoopPoller)
    monkeypatch.setattr("app.discovery.worker.ReactiveRunner", _NoopRunner)
    monkeypatch.setattr("app.discovery.worker.EdgarFilingsProvider", lambda *_, **__: _NoopEdgar())
    monkeypatch.setattr("app.discovery.worker._build_triage_llm", lambda _: _NoopLLM())
    monkeypatch.setattr("app.discovery.worker.build_copilot", lambda: _NoopCopilot())
    monkeypatch.setattr("app.discovery.worker.ReconciliationJobs", _NoopReconciliation)
    monkeypatch.setattr("app.discovery.worker.get_mtm_service", lambda: None)
    monkeypatch.setattr("app.discovery.worker.get_outcome_resolver", lambda: None)
    monkeypatch.setattr("app.discovery.worker.AlpacaNewsStream", _NoopNewsStream)
    monkeypatch.setattr("app.discovery.worker.CatalystJobs", _NoopCatalystJobs)
    # FinnhubProvider() pulls FINNHUB_API_KEY; stub it to a no-op so the
    # catalyst block doesn't fail in tests that don't care about it.
    monkeypatch.setattr("app.discovery.worker.FinnhubProvider", lambda *_, **__: None)
    monkeypatch.setattr("app.discovery.worker.CalendarFetcher", lambda *_, **__: None)
    monkeypatch.setattr("app.discovery.worker.CatalystScheduler", lambda *_, **__: None)
    yield


@pytest.mark.asyncio
async def test_starts_stopped() -> None:
    w = AutonomousWorker()
    assert w.state == WorkerState.STOPPED
    assert not w.is_running


@pytest.mark.asyncio
async def test_start_transitions_to_running() -> None:
    w = AutonomousWorker()
    await w.start()
    assert w.state == WorkerState.RUNNING
    assert w.is_running
    await w.stop()


@pytest.mark.asyncio
async def test_double_start_is_idempotent() -> None:
    w = AutonomousWorker()
    await w.start()
    await w.start()
    assert w.state == WorkerState.RUNNING
    await w.stop()


@pytest.mark.asyncio
async def test_stop_returns_to_stopped() -> None:
    w = AutonomousWorker()
    await w.start()
    await w.stop()
    assert w.state == WorkerState.STOPPED
    assert not w.is_running


@pytest.mark.asyncio
async def test_status_includes_config_and_stats() -> None:
    w = AutonomousWorker()
    s = w.status()
    assert s.state == WorkerState.STOPPED
    assert s.triage_provider in ("gemini", "groq", "anthropic", "ollama")
    assert s.thesis_provider in ("gemini", "groq", "anthropic", "ollama")
    assert isinstance(s.watchlist, list)
    assert s.theses_produced == 0


@pytest.mark.asyncio
async def test_status_after_start_reports_running() -> None:
    w = AutonomousWorker()
    await w.start()
    s = w.status()
    assert s.state == WorkerState.RUNNING
    assert s.started_at is not None
    await w.stop()
