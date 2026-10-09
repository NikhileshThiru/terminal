"""ReactiveRunner tests with stubbed bus, triage LLM, copilot, and persistence."""

from __future__ import annotations

import asyncio
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
        **_: Any,
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


@pytest.mark.asyncio
async def test_off_session_event_dropped_before_triage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Market-hours gate (CLAUDE.md §7): an off-session event must not reach
    triage (overnight LLM quota) or the copilot (theses graded against stale
    quotes), and the drop must be visible as a recorded decision."""
    from app.discovery import reactive as reactive_mod

    monkeypatch.setattr(reactive_mod, "is_market_open", lambda _now: False)
    triage_llm = _StubTriageLLM(
        [TriageDecision(passed=True, reason="would have passed", confidence=0.9)]
    )
    copilot = _StubCopilot(error=RuntimeError("copilot must not be called off-session"))
    runner = ReactiveRunner(
        bus=InMemoryEventBus(),
        copilot=copilot,
        triage_llm=triage_llm,
        triage_model="m",
        enforce_market_hours=True,
    )
    result = await runner._process_one(_ev(9))
    assert result is None
    assert runner.stats.events_off_session == 1
    assert triage_llm.calls == 0
    assert copilot.calls == []
    last = runner.stats.last_triage_decisions[-1]
    assert last["passed"] is False
    assert "off-session" in str(last["reason"])


@pytest.mark.asyncio
async def test_quota_exhausted_triage_records_visible_skip() -> None:
    """All triage routes rate-limited: the event must show up in the News pane
    as an explicit skip, not vanish (July 2026 incident)."""
    from app.data.types import ProviderUnavailable, ProviderUnavailableReason

    class _ExhaustedLLM(_StubTriageLLM):
        async def complete_structured(self, **_: Any) -> Any:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.RATE_LIMITED,
                message="all triage LLM routes rate-limited/cooling",
                provider="router",
            )

    runner = ReactiveRunner(
        bus=InMemoryEventBus(),
        copilot=_StubCopilot(error=RuntimeError("must not be called")),
        triage_llm=_ExhaustedLLM(),
        triage_model="m",
    )
    assert await runner._process_one(_ev(1)) is None
    assert runner.stats.triage_failures == 1
    assert runner.stats.events_skipped_quota == 1
    decision = runner.stats.last_triage_decisions[-1]
    assert decision["passed"] is False
    assert str(decision["reason"]).startswith("skipped:")


def _passing_run() -> CopilotRun:
    return CopilotRun(
        thesis=_sample_thesis(),
        tool_results=[],
        grounding=GroundingResult(
            passed=True, unverified_numbers=[], tools_used=[], extracted_count=0
        ),
        iterations_used=3,
    )


@pytest.mark.asyncio
async def test_daily_thesis_cap_skips_after_limit_and_counts_from_db() -> None:
    """Thesis pacing: once today's reactive theses reach the cap, further
    triage passes are recorded as skipped instead of spending thesis quota.
    The count comes from the DB so a restart can't reset it."""
    factory = persistence.get_session_factory()
    triage_llm = _StubTriageLLM(
        [TriageDecision(passed=True, reason="material", confidence=0.9) for _ in range(3)]
    )
    copilot = _StubCopilot(run=_passing_run())
    runner = ReactiveRunner(
        bus=InMemoryEventBus(),
        copilot=copilot,
        triage_llm=triage_llm,
        triage_model="m",
        session_factory=factory,
        max_theses_per_day=2,
    )
    assert await runner._process_one(_ev(1)) is not None
    assert await runner._process_one(_ev(2)) is not None
    assert await runner._process_one(_ev(3)) is None
    assert len(copilot.calls) == 2
    assert runner.stats.events_skipped_thesis_cap == 1
    assert "daily reactive thesis cap (2)" in str(runner.stats.last_triage_decisions[-1]["reason"])

    # A fresh runner (= backend restart) still sees today's two theses.
    restarted = ReactiveRunner(
        bus=InMemoryEventBus(),
        copilot=_StubCopilot(error=RuntimeError("cap must hold across restart")),
        triage_llm=_StubTriageLLM([TriageDecision(passed=True, reason="material", confidence=0.9)]),
        triage_model="m",
        session_factory=factory,
        max_theses_per_day=2,
    )
    assert await restarted._process_one(_ev(4)) is None
    assert restarted.stats.events_skipped_thesis_cap == 1


@pytest.mark.asyncio
async def test_synthetic_event_is_a_dry_run_never_persisted() -> None:
    """POST /autonomous/inject events run the funnel (demo) but must never
    become theses, trades, triage-log rows, or grounding attempts."""
    from sqlalchemy import func, select

    from app.discovery.models import TriageDecisionRow
    from app.eval.models import Thesis as ThesisRow

    factory = persistence.get_session_factory()
    calls: list[dict[str, Any]] = []

    class _RecordingCopilot(_StubCopilot):
        async def generate(self, user_thesis: str, **kwargs: Any) -> CopilotRun:
            calls.append(kwargs)
            return _passing_run()

    runner = ReactiveRunner(
        bus=InMemoryEventBus(),
        copilot=_RecordingCopilot(),
        triage_llm=_StubTriageLLM([TriageDecision(passed=True, reason="material", confidence=0.9)]),
        triage_model="m",
        session_factory=factory,
    )
    event = _ev(9).model_copy(update={"payload": {"synthetic": True}})
    run = await runner._process_one(event)
    assert run is not None  # the demo still sees the thesis
    assert calls[0]["record_attempt"] is False
    assert runner.stats.synthetic_dry_runs == 1
    assert runner.stats.theses_produced == 0
    await asyncio.gather(*list(runner._persist_tasks))
    async with factory() as session:
        assert (await session.execute(select(func.count(ThesisRow.id)))).scalar_one() == 0
        assert (await session.execute(select(func.count(TriageDecisionRow.id)))).scalar_one() == 0
