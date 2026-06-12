"""POST /autonomous/{start,stop} + GET /autonomous/{status,theses} integration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.discovery import worker as worker_module
from app.eval import persistence
from app.eval.models import Base
from app.eval.models import Thesis as ThesisRow
from app.main import create_app

# === Stubs that replace the real components inside the worker ===


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
async def _stub_everything(monkeypatch: pytest.MonkeyPatch):
    """Isolate worker dependencies + DB across tests."""
    # Reset the singleton so each test gets a fresh worker.
    worker_module.get_worker.cache_clear()
    monkeypatch.setattr(worker_module, "EdgarPoller", _NoopPoller)
    monkeypatch.setattr(worker_module, "ReactiveRunner", _NoopRunner)
    monkeypatch.setattr(worker_module, "EdgarFilingsProvider", lambda *_, **__: _NoopEdgar())
    monkeypatch.setattr(worker_module, "_build_triage_llm", lambda _: _NoopLLM())
    monkeypatch.setattr(worker_module, "build_copilot", lambda: _NoopCopilot())
    monkeypatch.setattr(worker_module, "ReconciliationJobs", _NoopReconciliation)
    monkeypatch.setattr(worker_module, "get_mtm_service", lambda: None)
    monkeypatch.setattr(worker_module, "get_outcome_resolver", lambda: None)
    monkeypatch.setattr(worker_module, "AlpacaNewsStream", _NoopNewsStream)
    monkeypatch.setattr(worker_module, "CatalystJobs", _NoopCatalystJobs)
    monkeypatch.setattr(worker_module, "FinnhubProvider", lambda *_, **__: None)
    monkeypatch.setattr(worker_module, "CalendarFetcher", lambda *_, **__: None)
    monkeypatch.setattr(worker_module, "CatalystScheduler", lambda *_, **__: None)

    # Isolated DB.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    persistence.get_engine.cache_clear()
    persistence.get_session_factory.cache_clear()
    monkeypatch.setattr(persistence, "get_engine", lambda: engine)
    monkeypatch.setattr(persistence, "get_session_factory", lambda: factory)

    yield

    # Stop worker between tests if it was started.
    w = worker_module.get_worker()
    if w.is_running:
        await w.stop()
    worker_module.get_worker.cache_clear()
    await engine.dispose()


def _client() -> TestClient:
    return TestClient(create_app())


def test_initial_status_is_stopped() -> None:
    client = _client()
    r = client.get("/autonomous/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "stopped"
    assert body["theses_produced"] == 0


def test_start_then_status_reports_running() -> None:
    client = _client()
    r = client.post("/autonomous/start")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "running"
    assert body["started_at"] is not None

    r2 = client.get("/autonomous/status")
    assert r2.status_code == 200
    assert r2.json()["state"] == "running"


def test_start_then_stop_returns_to_stopped() -> None:
    client = _client()
    client.post("/autonomous/start")
    r = client.post("/autonomous/stop")
    assert r.status_code == 200
    assert r.json()["state"] == "stopped"


def test_double_start_idempotent() -> None:
    client = _client()
    a = client.post("/autonomous/start")
    b = client.post("/autonomous/start")
    assert a.status_code == 200
    assert b.status_code == 200
    assert b.json()["state"] == "running"


async def _seed_thesis(factory, **overrides: Any) -> None:
    defaults = {
        "correlation_id": "x" * 16,
        "source_bucket": "reactive",
        "symbol": "AAPL",
        "generated_at": datetime.now(UTC),
        "direction": "long",
        "confidence": 0.6,
        "prediction_window_days": 14,
        "reasoning": "test reasoning",
        "suggested_contract": {"occ_symbol": "AAPL301220C00150000"},
        "grounding_check_passed": True,
        "llm_provider": "gemini",
        "llm_model": "gemini-2.5-flash",
        "funnel_latency_ms": 3000,
    }
    defaults.update(overrides)
    async with factory() as session:
        row = ThesisRow(**defaults)
        session.add(row)
        await session.commit()


@pytest.mark.asyncio
async def test_theses_endpoint_filters_by_source_bucket() -> None:
    factory = persistence.get_session_factory()
    await _seed_thesis(factory, source_bucket="reactive", symbol="AAPL")
    await _seed_thesis(factory, source_bucket="manual", symbol="MSFT")
    await _seed_thesis(factory, source_bucket="reactive", symbol="NVDA")

    client = _client()
    # Default: ALL buckets — this is the Live Theses feed; rows carry their
    # source_bucket so the UI badges them.
    r = client.get("/autonomous/theses")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 3
    assert {row["symbol"] for row in body} == {"AAPL", "MSFT", "NVDA"}

    # Explicit filter for one bucket.
    r2 = client.get("/autonomous/theses?source_bucket=manual")
    assert r2.status_code == 200
    assert [row["symbol"] for row in r2.json()] == ["MSFT"]

    r3 = client.get("/autonomous/theses?source_bucket=reactive")
    assert r3.status_code == 200
    assert {row["symbol"] for row in r3.json()} == {"AAPL", "NVDA"}


@pytest.mark.asyncio
async def test_theses_endpoint_honors_limit() -> None:
    factory = persistence.get_session_factory()
    for i in range(5):
        await _seed_thesis(factory, symbol=f"S{i}")
    client = _client()
    r = client.get("/autonomous/theses?limit=3")
    assert len(r.json()) == 3


@pytest.mark.asyncio
async def test_catalysts_endpoint_returns_upcoming_in_window() -> None:
    """/autonomous/catalysts returns scheduled events inside the requested window."""
    from datetime import date as _date
    from datetime import timedelta as _td
    from decimal import Decimal

    from app.discovery.models import CatalystEvent, CatalystEventState, CatalystEventType

    factory = persistence.get_session_factory()
    async with factory() as session:
        # Three events: one near, one far, one past.
        session.add_all(
            [
                CatalystEvent(
                    symbol="AAPL",
                    event_type=CatalystEventType.EARNINGS.value,
                    event_date=_date.today() + _td(days=2),
                    estimated_eps=Decimal("1.93"),
                    state=CatalystEventState.SCHEDULED.value,
                    scheduled_at=datetime.now(UTC),
                ),
                CatalystEvent(
                    symbol="MSFT",
                    event_type=CatalystEventType.EARNINGS.value,
                    event_date=_date.today() + _td(days=30),
                    estimated_eps=Decimal("2.50"),
                    state=CatalystEventState.SCHEDULED.value,
                    scheduled_at=datetime.now(UTC),
                ),
                CatalystEvent(
                    symbol="OLD",
                    event_type=CatalystEventType.EARNINGS.value,
                    event_date=_date.today() - _td(days=5),
                    state=CatalystEventState.EXPIRED.value,
                    scheduled_at=datetime.now(UTC),
                ),
            ]
        )
        await session.commit()

    client = _client()
    # Default 14-day window: AAPL only.
    r = client.get("/autonomous/catalysts")
    assert r.status_code == 200
    symbols = [row["symbol"] for row in r.json()]
    assert symbols == ["AAPL"]
    # Wider window includes MSFT but not OLD (which is past + expired).
    r2 = client.get("/autonomous/catalysts?within_days=60")
    assert [row["symbol"] for row in r2.json()] == ["AAPL", "MSFT"]
