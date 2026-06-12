"""ReactiveRunner tests with stubbed bus, triage LLM, copilot, and persistence."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.copilot import CopilotError, CopilotRun
from app.agent.grounding import GroundingResult
from app.agent.schemas import SuggestedContract, Thesis
from app.discovery.bus import InMemoryEventBus
from app.discovery.reactive import ReactiveRunner, _format_event_for_copilot
from app.discovery.triage import TriageDecision
from app.discovery.types import DiscoveryEvent
from app.eval import persistence
from app.eval.models import Base


@pytest.fixture(autouse=True)
async def _isolated_db(monkeypatch: pytest.MonkeyPatch):
    """Each test gets a fresh in-memory SQLite."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    persistence.get_engine.cache_clear()
    persistence.get_session_factory.cache_clear()
    monkeypatch.setattr(persistence, "get_engine", lambda: engine)
    monkeypatch.setattr(persistence, "get_session_factory", lambda: factory)
    yield
    await engine.dispose()


class _StubTriageLLM:
    name = "stub"

    def __init__(self, decisions: list[TriageDecision] | None = None) -> None:
        self._queue = list(decisions or [])
        self.calls = 0

    async def complete(self, **_: Any) -> Any:
        raise NotImplementedError

    async def complete_structured(self, **_: Any) -> Any:
        self.calls += 1
        if not self._queue:
            return TriageDecision(passed=False, reason="default drop", confidence=0.5)
        return self._queue.pop(0)

    async def step_agent(self, **_: Any) -> Any:
        raise NotImplementedError


def _sample_thesis() -> Thesis:
    exp = date.today() + timedelta(days=14)
    return Thesis(
        symbol="AAPL",
        direction="long",
        confidence=0.65,
        reasoning="AAPL at $312.06; the 315 call at 4.50/4.70 is the play.",
        prediction_window_days=14,
        suggested_contract=SuggestedContract(
            underlying="AAPL",
            occ_symbol=exp.strftime("AAPL%y%m%dC00315000"),
            option_type="call",
            strike=Decimal("315"),
            expiration=exp,
            estimated_premium_per_contract=Decimal("4.60"),
            contracts=1,
            max_risk_usd=Decimal("460"),
        ),
        what_must_happen="AAPL above $315 by expiry.",
        correlation_id="rt-1",
        source_bucket="reactive",
        generated_at=datetime.now(UTC),
        grounding_check_passed=True,
        llm_provider="gemini",
        llm_model="gemini-2.5-flash",
        funnel_latency_ms=4500,
    )


class _StubCopilot:
    """Returns a pre-baked CopilotRun or raises a pre-set error."""

    def __init__(
        self,
        run: CopilotRun | None = None,
        error: Exception | None = None,
    ) -> None:
        self._run = run
        self._error = error
        self.calls: list[tuple[str, float | None, str]] = []

    async def generate(
        self,
        user_thesis: str,
        *,
        risk_budget_usd: float | None = None,
        source_bucket: str = "manual",
    ) -> CopilotRun:
        self.calls.append((user_thesis, risk_budget_usd, source_bucket))
        if self._error is not None:
            raise self._error
        assert self._run is not None
        return self._run


def _ev(idx: int = 0, headline: str = "AAPL 8-K: earnings beat") -> DiscoveryEvent:
    return DiscoveryEvent(
        id=f"acc-{idx}",
        source="edgar",
        kind="filing",
        symbols=["AAPL"],
        headline=headline,
        body="EPS $2.01 vs $1.79 estimate; revenue $90B vs $87B.",
        url="https://example.com/8k",
        published_at=datetime.now(UTC),
    )


def test_format_event_includes_symbol_and_triage_reason() -> None:
    s = _format_event_for_copilot(_ev(1), "earnings beat — material")
    assert "AAPL" in s
    assert "earnings beat — material" in s
    assert "AUTONOMOUS TRIGGER" in s


# === Pipeline behavior ===


@pytest.mark.asyncio
async def test_passing_triage_triggers_thesis_and_persists() -> None:
    bus = InMemoryEventBus()
    await bus.publish(_ev(1))

    triage_llm = _StubTriageLLM(
        [TriageDecision(passed=True, reason="material earnings", confidence=0.85)]
    )
    run = CopilotRun(
        thesis=_sample_thesis(),
        tool_results=[],
        grounding=GroundingResult(
            passed=True, unverified_numbers=[], tools_used=[], extracted_count=0
        ),
        iterations_used=4,
    )
    copilot = _StubCopilot(run=run)
    runner = ReactiveRunner(bus=bus, copilot=copilot, triage_llm=triage_llm, triage_model="m")
    # Bypass the queue loop and call directly so we don't need start/stop.
    # Re-publish so _process_one can consume from event.
    result = await runner._process_one(_ev(2))
    assert result is not None
    assert result.thesis.symbol == "AAPL"
    assert runner.stats.events_consumed == 1
    assert runner.stats.events_passed_triage == 1
    assert runner.stats.theses_produced == 1
    assert len(copilot.calls) == 1
    user_input, budget, bucket = copilot.calls[0]
    assert bucket == "reactive"
    assert budget == 500.0
    assert "AAPL" in user_input


@pytest.mark.asyncio
async def test_failing_triage_skips_thesis() -> None:
    bus = InMemoryEventBus()
    triage_llm = _StubTriageLLM(
        [TriageDecision(passed=False, reason="routine filing", confidence=0.6)]
    )
    copilot = _StubCopilot(run=None)
    runner = ReactiveRunner(bus=bus, copilot=copilot, triage_llm=triage_llm, triage_model="m")
    result = await runner._process_one(_ev(1))
    assert result is None
    assert runner.stats.events_consumed == 1
    assert runner.stats.events_passed_triage == 0
    assert runner.stats.theses_produced == 0
    assert len(copilot.calls) == 0
    # Triage decision recorded for visibility.
    assert len(runner.stats.last_triage_decisions) == 1
    assert runner.stats.last_triage_decisions[0]["passed"] is False


@pytest.mark.asyncio
async def test_copilot_failure_does_not_crash_runner() -> None:
    bus = InMemoryEventBus()
    triage_llm = _StubTriageLLM([TriageDecision(passed=True, reason="material", confidence=0.8)])
    copilot = _StubCopilot(error=CopilotError("model exhausted iterations"))
    runner = ReactiveRunner(bus=bus, copilot=copilot, triage_llm=triage_llm, triage_model="m")
    result = await runner._process_one(_ev(1))
    assert result is None
    assert runner.stats.thesis_failures == 1
    assert runner.stats.theses_produced == 0


@pytest.mark.asyncio
async def test_triage_provider_failure_does_not_crash_runner() -> None:
    bus = InMemoryEventBus()

    class _BrokenLLM:
        name = "broken"

        async def complete_structured(self, **_: Any) -> Any:
            raise RuntimeError("network down")

        async def complete(self, **_: Any) -> Any:
            raise NotImplementedError

        async def step_agent(self, **_: Any) -> Any:
            raise NotImplementedError

    runner = ReactiveRunner(
        bus=bus,
        copilot=_StubCopilot(),
        triage_llm=_BrokenLLM(),
        triage_model="m",
    )
    result = await runner._process_one(_ev(1))
    assert result is None
    assert runner.stats.triage_failures == 1
    assert "network down" in (runner.stats.last_error or "")


@pytest.mark.asyncio
async def test_start_stop_lifecycle() -> None:
    """Runner.start spawns the loop; stop cleanly cancels it."""
    bus = InMemoryEventBus()
    triage_llm = _StubTriageLLM([TriageDecision(passed=False, reason="drop", confidence=0.5)])
    copilot = _StubCopilot()
    runner = ReactiveRunner(bus=bus, copilot=copilot, triage_llm=triage_llm, triage_model="m")
    await runner.start()
    await bus.publish(_ev(1))
    # Give the loop a beat to consume.
    import asyncio

    await asyncio.sleep(0.05)
    await runner.stop()
    assert runner.stats.events_consumed >= 1
