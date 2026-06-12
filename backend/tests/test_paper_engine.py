"""PaperEngine: thesis → per-account verdicts → ShadowTrade rows."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.schemas import SuggestedContract, Thesis
from app.eval.models import Base
from app.eval.models import Thesis as ThesisRow
from app.portfolio.engine import PaperEngine
from app.portfolio.models import AccountKind, PaperAccount, ShadowTrade


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_accounts(session_factory) -> tuple[int, int]:
    """Seed conservative + aggressive accounts; return their ids."""
    async with session_factory() as session:
        conservative = PaperAccount(
            kind=AccountKind.CONSERVATIVE.value,
            name="Conservative",
            starting_balance_usd=Decimal("100000"),
            equity_usd=Decimal("100000"),
            min_confidence=0.7,
            max_trade_cost_usd=Decimal("300"),
            max_trades_per_day=3,
            max_concurrent_positions=5,
            kill_switch=False,
            created_at=datetime.now(UTC),
        )
        aggressive = PaperAccount(
            kind=AccountKind.AGGRESSIVE.value,
            name="Aggressive",
            starting_balance_usd=Decimal("100000"),
            equity_usd=Decimal("100000"),
            min_confidence=0.5,
            max_trade_cost_usd=Decimal("500"),
            max_trades_per_day=8,
            max_concurrent_positions=10,
            kill_switch=False,
            created_at=datetime.now(UTC),
        )
        session.add(conservative)
        session.add(aggressive)
        await session.commit()
        return conservative.id, aggressive.id


async def _seed_thesis_row(session_factory) -> int:
    async with session_factory() as session:
        th = ThesisRow(
            correlation_id="x" * 16,
            source_bucket="reactive",
            symbol="AAPL",
            generated_at=datetime.now(UTC),
            direction="long",
            confidence=0.65,
            prediction_window_days=14,
            reasoning="test",
            suggested_contract={"occ_symbol": "AAPL301220C00150000"},
            grounding_check_passed=True,
            llm_provider="gemini",
            llm_model="gemini-2.5-flash",
            funnel_latency_ms=3000,
        )
        session.add(th)
        await session.commit()
        return th.id


def _thesis_dto(
    *, confidence: float = 0.65, premium: Decimal = Decimal("2.00"), contracts: int = 1
) -> Thesis:
    exp = date.today() + timedelta(days=30)
    return Thesis(
        symbol="AAPL",
        direction="long",
        confidence=confidence,
        reasoning="x" * 30,
        prediction_window_days=14,
        suggested_contract=SuggestedContract(
            underlying="AAPL",
            occ_symbol="AAPL301220C00150000",
            option_type="call",
            strike=Decimal("150"),
            expiration=exp,
            estimated_premium_per_contract=premium,
            contracts=contracts,
            max_risk_usd=premium * contracts * Decimal(100),
        ),
        what_must_happen="closes above strike",
        correlation_id="x" * 16,
        source_bucket="reactive",
        generated_at=datetime.now(UTC),
        grounding_check_passed=True,
        llm_provider="gemini",
        llm_model="gemini-2.5-flash",
        funnel_latency_ms=3000,
    )


# === Tests ===


@pytest.mark.asyncio
async def test_aggressive_accepts_when_conservative_rejects(session_factory) -> None:
    """The conservative/aggressive contrast IS the feature."""
    await _seed_accounts(session_factory)
    thesis_id = await _seed_thesis_row(session_factory)

    engine = PaperEngine(session_factory)
    # 0.65 is below conservative threshold (0.7) but above aggressive (0.5)
    decisions = await engine.consider_thesis(thesis_id, _thesis_dto(confidence=0.65))
    by_kind = {d.account_kind: d for d in decisions}
    assert by_kind["conservative"].decision.approved is False
    assert by_kind["aggressive"].decision.approved is True
    assert by_kind["aggressive"].trade is not None
    assert by_kind["conservative"].trade is None

    # Only one ShadowTrade row exists.
    async with session_factory() as session:
        count = await session.scalar(select(func.count(ShadowTrade.id)))
        assert count == 1


@pytest.mark.asyncio
async def test_both_approve_high_confidence_thesis(session_factory) -> None:
    await _seed_accounts(session_factory)
    thesis_id = await _seed_thesis_row(session_factory)

    engine = PaperEngine(session_factory)
    decisions = await engine.consider_thesis(thesis_id, _thesis_dto(confidence=0.85))
    assert all(d.decision.approved for d in decisions)
    assert all(d.trade is not None for d in decisions)

    async with session_factory() as session:
        count = await session.scalar(select(func.count(ShadowTrade.id)))
        assert count == 2


@pytest.mark.asyncio
async def test_daily_cap_blocks_further_trades(session_factory) -> None:
    """After max_trades_per_day, additional theses get rejected for that account."""
    await _seed_accounts(session_factory)
    engine = PaperEngine(session_factory)

    # Conservative cap is 3; submit 4 strong theses.
    for _ in range(4):
        thesis_id = await _seed_thesis_row(session_factory)
        await engine.consider_thesis(thesis_id, _thesis_dto(confidence=0.85))

    async with session_factory() as session:
        conservative_count = await session.scalar(
            select(func.count(ShadowTrade.id))
            .join(PaperAccount, PaperAccount.id == ShadowTrade.account_id)
            .where(PaperAccount.kind == "conservative")
        )
        aggressive_count = await session.scalar(
            select(func.count(ShadowTrade.id))
            .join(PaperAccount, PaperAccount.id == ShadowTrade.account_id)
            .where(PaperAccount.kind == "aggressive")
        )
    assert conservative_count == 3
    assert aggressive_count == 4


@pytest.mark.asyncio
async def test_persisted_trade_carries_full_snapshot(session_factory) -> None:
    await _seed_accounts(session_factory)
    thesis_id = await _seed_thesis_row(session_factory)
    engine = PaperEngine(session_factory)

    decisions = await engine.consider_thesis(
        thesis_id, _thesis_dto(confidence=0.85, premium=Decimal("3.00"))
    )
    approved = [d for d in decisions if d.trade is not None]
    assert approved
    trade = approved[0].trade
    assert trade.underlying == "AAPL"
    assert trade.occ_symbol == "AAPL301220C00150000"
    assert trade.option_type == "call"
    assert trade.contracts >= 1
    assert trade.total_cost_usd > 0
    assert trade.risk_reason  # populated for audit
