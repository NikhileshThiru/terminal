"""Catalyst calendar tests — fetcher upsert idempotency + scheduler trigger logic."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.data.types import EarningsEvent, ProviderUnavailable, ProviderUnavailableReason
from app.discovery.catalyst_fetcher import CalendarFetcher
from app.discovery.catalyst_scheduler import CatalystScheduler, _build_catalyst_prompt
from app.discovery.models import (
    CatalystEvent,
    CatalystEventState,
    CatalystEventType,
)
from app.eval.models import Base
from app.eval.models import Thesis as ThesisRow


class FakeFinnhub:
    name = "fake-finnhub"

    def __init__(self) -> None:
        self.by_symbol: dict[str, list[EarningsEvent]] = {}
        self.fail_on: set[str] = set()
        self.calls: list[str] = []

    async def get_earnings_calendar(
        self,
        symbol: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[EarningsEvent]:
        assert symbol is not None
        self.calls.append(symbol)
        if symbol in self.fail_on:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.RATE_LIMITED,
                message="quota",
                provider="fake-finnhub",
            )
        return list(self.by_symbol.get(symbol, []))


@pytest.fixture
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _ev(symbol: str, days_out: int, *, eps: float | None = 1.50) -> EarningsEvent:
    return EarningsEvent(
        symbol=symbol,
        event_date=date.today() + timedelta(days=days_out),
        eps_estimate=eps,
        hour="amc",
    )


# === CalendarFetcher ===


@pytest.mark.asyncio
async def test_fetcher_inserts_new_events(db_factory) -> None:
    finnhub = FakeFinnhub()
    finnhub.by_symbol["AAPL"] = [_ev("AAPL", 30)]
    finnhub.by_symbol["MSFT"] = [_ev("MSFT", 45)]
    fetcher = CalendarFetcher(
        finnhub=finnhub, session_factory=db_factory, universe=["AAPL", "MSFT"]
    )
    result = await fetcher.run_once()
    assert result.upserted == 2
    assert result.skipped == 0
    assert result.errors == 0

    async with db_factory() as session:
        rows = list((await session.execute(select(CatalystEvent))).scalars().all())
    assert len(rows) == 2
    assert {r.symbol for r in rows} == {"AAPL", "MSFT"}
    assert all(r.state == CatalystEventState.SCHEDULED.value for r in rows)


@pytest.mark.asyncio
async def test_fetcher_idempotent_on_rerun(db_factory) -> None:
    """A second run with the same data shouldn't create duplicates."""
    finnhub = FakeFinnhub()
    finnhub.by_symbol["AAPL"] = [_ev("AAPL", 30)]
    fetcher = CalendarFetcher(finnhub=finnhub, session_factory=db_factory, universe=["AAPL"])
    await fetcher.run_once()
    await fetcher.run_once()
    async with db_factory() as session:
        rows = list((await session.execute(select(CatalystEvent))).scalars().all())
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_fetcher_refreshes_estimated_eps_on_rerun(db_factory) -> None:
    """Analyst estimates can change; we want the latest value in the row."""
    finnhub = FakeFinnhub()
    finnhub.by_symbol["AAPL"] = [_ev("AAPL", 30, eps=1.50)]
    fetcher = CalendarFetcher(finnhub=finnhub, session_factory=db_factory, universe=["AAPL"])
    await fetcher.run_once()
    finnhub.by_symbol["AAPL"] = [_ev("AAPL", 30, eps=1.93)]
    await fetcher.run_once()
    async with db_factory() as session:
        row = (await session.execute(select(CatalystEvent))).scalar_one()
    assert row.estimated_eps == Decimal("1.93")


@pytest.mark.asyncio
async def test_fetcher_skips_symbol_on_provider_unavailable(db_factory) -> None:
    """One bad symbol shouldn't kill the run for the others."""
    finnhub = FakeFinnhub()
    finnhub.by_symbol["AAPL"] = [_ev("AAPL", 30)]
    finnhub.by_symbol["MSFT"] = [_ev("MSFT", 45)]
    finnhub.fail_on = {"AAPL"}
    fetcher = CalendarFetcher(
        finnhub=finnhub, session_factory=db_factory, universe=["AAPL", "MSFT"]
    )
    result = await fetcher.run_once()
    assert result.upserted == 1
    assert result.skipped == 1
    async with db_factory() as session:
        rows = list((await session.execute(select(CatalystEvent))).scalars().all())
    assert {r.symbol for r in rows} == {"MSFT"}


@pytest.mark.asyncio
async def test_fetcher_expires_past_dates(db_factory) -> None:
    """Yesterday's scheduled events move to EXPIRED so they don't pollute UI."""
    # Seed a past-dated scheduled event directly.
    async with db_factory() as session:
        session.add(
            CatalystEvent(
                symbol="OLD",
                event_type=CatalystEventType.EARNINGS.value,
                event_date=date.today() - timedelta(days=5),
                state=CatalystEventState.SCHEDULED.value,
                scheduled_at=datetime.now(UTC),
            )
        )
        await session.commit()

    finnhub = FakeFinnhub()
    fetcher = CalendarFetcher(finnhub=finnhub, session_factory=db_factory, universe=[])
    await fetcher.run_once()
    async with db_factory() as session:
        row = (await session.execute(select(CatalystEvent))).scalar_one()
    assert row.state == CatalystEventState.EXPIRED.value


@pytest.mark.asyncio
async def test_fetcher_upcoming_returns_only_in_window(db_factory) -> None:
    finnhub = FakeFinnhub()
    finnhub.by_symbol["AAPL"] = [_ev("AAPL", 30)]  # within 14-day window? no (30 > 14)
    finnhub.by_symbol["MSFT"] = [_ev("MSFT", 5)]  # within
    fetcher = CalendarFetcher(
        finnhub=finnhub, session_factory=db_factory, universe=["AAPL", "MSFT"]
    )
    await fetcher.run_once()
    upcoming = await fetcher.upcoming(within_days=14)
    assert [c.symbol for c in upcoming] == ["MSFT"]


# === CatalystScheduler ===


class FakeCopilot:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_on_next: Exception | None = None

    async def generate(self, prompt: str, **kwargs: Any) -> Any:
        from app.agent.copilot import CopilotRun
        from app.agent.schemas import SuggestedContract, Thesis

        self.calls.append({"prompt": prompt, **kwargs})
        if self.raise_on_next is not None:
            exc, self.raise_on_next = self.raise_on_next, None
            raise exc
        contract = SuggestedContract(
            underlying="AAPL",
            occ_symbol="AAPL260821C00350000",
            option_type="call",
            strike=Decimal("350"),
            expiration=date.today() + timedelta(days=60),
            estimated_premium_per_contract=Decimal("3.88"),
            contracts=1,
            max_risk_usd=Decimal("388.00"),
        )
        thesis = Thesis(
            symbol="AAPL",
            direction="long",
            confidence=0.7,
            reasoning="test reasoning with at least twenty chars",
            prediction_window_days=14,
            suggested_contract=contract,
            what_must_happen="AAPL beats EPS estimate",
            correlation_id="cat-test",
            source_bucket=kwargs.get("source_bucket", "catalyst"),
            generated_at=datetime.now(UTC),
            grounding_check_passed=True,
            grounding_notes=None,
            llm_provider="fake",
            llm_model="fake",
            funnel_latency_ms=1000,
        )
        return CopilotRun(
            thesis=thesis, tool_results=[], grounding=_make_grounding(), iterations_used=1
        )


def _make_grounding() -> Any:
    from app.agent.grounding import GroundingResult

    return GroundingResult(
        passed=True,
        unverified_numbers=[],
        tools_used=[],
        extracted_count=0,
        notes=None,
    )


def _seed_thesis_table_init(session_factory: async_sessionmaker) -> None:
    """No-op; tables created via Base.metadata.create_all in the fixture."""
    # placeholder for symmetry with other test files
    _ = session_factory


async def _seed_catalyst(
    factory: async_sessionmaker,
    *,
    symbol: str,
    days_out: int,
    state: str = CatalystEventState.SCHEDULED.value,
    thesis_id: int | None = None,
) -> int:
    async with factory() as session:
        ev = CatalystEvent(
            symbol=symbol,
            event_type=CatalystEventType.EARNINGS.value,
            event_date=date.today() + timedelta(days=days_out),
            event_hour="amc",
            estimated_eps=Decimal("1.93"),
            state=state,
            thesis_id=thesis_id,
            scheduled_at=datetime.now(UTC),
        )
        session.add(ev)
        await session.commit()
        return int(ev.id)


@pytest.fixture
async def configured_persistence(db_factory, monkeypatch: pytest.MonkeyPatch):
    """Wire write_thesis() to use the test DB factory."""
    from app.eval import persistence

    persistence.get_engine.cache_clear()
    persistence.get_session_factory.cache_clear()
    monkeypatch.setattr(persistence, "get_session_factory", lambda: db_factory)
    yield db_factory


@pytest.mark.asyncio
async def test_scheduler_triggers_event_within_lead_window(configured_persistence) -> None:
    factory = configured_persistence
    await _seed_catalyst(factory, symbol="AAPL", days_out=1)
    scheduler = CatalystScheduler(copilot=FakeCopilot(), session_factory=factory, lead_days=2)
    result = await scheduler.run_once()
    assert result.triggered == 1
    assert result.candidates == 1
    async with factory() as session:
        row = (await session.execute(select(CatalystEvent))).scalar_one()
    assert row.state == CatalystEventState.TRIGGERED.value
    assert row.thesis_id is not None
    assert row.triggered_at is not None


@pytest.mark.asyncio
async def test_scheduler_skips_events_outside_lead_window(configured_persistence) -> None:
    """Events 10 days out shouldn't fire when lead_days=2."""
    factory = configured_persistence
    await _seed_catalyst(factory, symbol="AAPL", days_out=10)
    scheduler = CatalystScheduler(copilot=FakeCopilot(), session_factory=factory, lead_days=2)
    result = await scheduler.run_once()
    assert result.triggered == 0
    assert result.candidates == 0


@pytest.mark.asyncio
async def test_scheduler_skips_already_triggered(configured_persistence) -> None:
    factory = configured_persistence
    # Pre-fire — seed a thesis row so the FK is valid.
    async with factory() as session:
        thesis = ThesisRow(
            correlation_id="x" * 16,
            source_bucket="catalyst",
            symbol="AAPL",
            generated_at=datetime.now(UTC),
            direction="long",
            confidence=0.7,
            prediction_window_days=14,
            reasoning="test",
            suggested_contract={},
            grounding_check_passed=True,
            llm_provider="fake",
            llm_model="fake",
            funnel_latency_ms=1000,
        )
        session.add(thesis)
        await session.flush()
        await _seed_catalyst(
            factory,
            symbol="AAPL",
            days_out=1,
            state=CatalystEventState.TRIGGERED.value,
            thesis_id=thesis.id,
        )
    scheduler = CatalystScheduler(copilot=FakeCopilot(), session_factory=factory, lead_days=2)
    result = await scheduler.run_once()
    assert result.candidates == 0  # already-triggered filtered out at query level


@pytest.mark.asyncio
async def test_scheduler_recoverable_copilot_error_leaves_event_scheduled(
    configured_persistence,
) -> None:
    """If the copilot raises CopilotError, the row stays scheduled to retry."""
    from app.agent.copilot import CopilotError

    factory = configured_persistence
    await _seed_catalyst(factory, symbol="AAPL", days_out=1)
    copilot = FakeCopilot()
    copilot.raise_on_next = CopilotError("simulated")
    scheduler = CatalystScheduler(copilot=copilot, session_factory=factory, lead_days=2)
    result = await scheduler.run_once()
    assert result.triggered == 0
    assert result.skipped == 1
    async with factory() as session:
        row = (await session.execute(select(CatalystEvent))).scalar_one()
    assert row.state == CatalystEventState.SCHEDULED.value
    assert row.thesis_id is None


# === Prompt builder ===


def test_prompt_includes_event_metadata() -> None:
    ev = CatalystEvent(
        id=1,
        symbol="AAPL",
        event_type="earnings",
        event_date=date.today() + timedelta(days=2),
        event_hour="amc",
        estimated_eps=Decimal("1.93"),
        estimated_revenue_usd=Decimal("90000000000"),
        state="scheduled",
        scheduled_at=datetime.now(UTC),
    )
    prompt = _build_catalyst_prompt(ev)
    assert "AAPL" in prompt
    assert "1.93" in prompt
    assert "after market close" in prompt
    assert "$90,000,000,000" in prompt
