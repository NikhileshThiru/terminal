"""Portfolio model roundtrip + FK constraint tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.eval.models import Base
from app.eval.models import Thesis as ThesisRow
from app.portfolio.models import AccountKind, PaperAccount, ShadowTrade, ShadowTradeStatus


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        # SQLite needs FK enforcement enabled per-connection.
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _account(kind: AccountKind = AccountKind.CONSERVATIVE) -> PaperAccount:
    return PaperAccount(
        kind=kind.value,
        name=f"{kind.value.title()} paper account",
        starting_balance_usd=Decimal("100000.00"),
        equity_usd=Decimal("100000.00"),
        min_confidence=0.7 if kind == AccountKind.CONSERVATIVE else 0.5,
        max_trade_cost_usd=Decimal("300.00")
        if kind == AccountKind.CONSERVATIVE
        else Decimal("500.00"),
        max_trades_per_day=3 if kind == AccountKind.CONSERVATIVE else 8,
        max_concurrent_positions=5,
        kill_switch=False,
        created_at=datetime.now(UTC),
    )


def _thesis() -> ThesisRow:
    return ThesisRow(
        correlation_id="abc1234567890123",
        source_bucket="reactive",
        symbol="AAPL",
        generated_at=datetime.now(UTC),
        direction="long",
        confidence=0.65,
        prediction_window_days=14,
        reasoning="AAPL at $312 with bullish setup.",
        suggested_contract={"occ_symbol": "AAPL301220C00315000"},
        grounding_check_passed=True,
        llm_provider="gemini",
        llm_model="gemini-2.5-flash",
        funnel_latency_ms=3000,
    )


@pytest.mark.asyncio
async def test_account_roundtrips(session_factory) -> None:
    async with session_factory() as session:
        acc = _account()
        session.add(acc)
        await session.commit()
        acc_id = acc.id

    async with session_factory() as session:
        fetched = (
            await session.execute(select(PaperAccount).where(PaperAccount.id == acc_id))
        ).scalar_one()
        assert fetched.kind == "conservative"
        assert fetched.starting_balance_usd == Decimal("100000.00")
        assert fetched.min_confidence == 0.7
        assert fetched.max_trades_per_day == 3
        assert fetched.kill_switch is False


@pytest.mark.asyncio
async def test_account_kind_must_be_unique(session_factory) -> None:
    async with session_factory() as session:
        session.add(_account(AccountKind.CONSERVATIVE))
        await session.commit()
    async with session_factory() as session:
        session.add(_account(AccountKind.CONSERVATIVE))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_shadow_trade_roundtrips(session_factory) -> None:
    async with session_factory() as session:
        acc = _account()
        th = _thesis()
        session.add(acc)
        session.add(th)
        await session.commit()
        await session.refresh(acc)
        await session.refresh(th)

        trade = ShadowTrade(
            account_id=acc.id,
            thesis_id=th.id,
            opened_at=datetime.now(UTC),
            underlying="AAPL",
            occ_symbol="AAPL301220C00315000",
            option_type="call",
            strike=Decimal("315"),
            expiration=date.today() + timedelta(days=30),
            contracts=1,
            premium_per_contract_usd=Decimal("4.60"),
            total_cost_usd=Decimal("460.00"),
            status=ShadowTradeStatus.SHADOW_OPEN.value,
            risk_reason="Confidence 0.65 ≥ threshold 0.50; cost $460 ≤ cap $500.",
        )
        session.add(trade)
        await session.commit()
        trade_id = trade.id

    async with session_factory() as session:
        fetched = (
            await session.execute(select(ShadowTrade).where(ShadowTrade.id == trade_id))
        ).scalar_one()
        assert fetched.underlying == "AAPL"
        assert fetched.contracts == 1
        assert fetched.total_cost_usd == Decimal("460.00")
        assert fetched.status == "shadow_open"
        assert "Confidence 0.65" in fetched.risk_reason


@pytest.mark.asyncio
async def test_shadow_trade_requires_existing_thesis(session_factory) -> None:
    """FK enforcement: shadow trade can't reference a non-existent thesis."""
    async with session_factory() as session:
        acc = _account()
        session.add(acc)
        await session.commit()
        await session.refresh(acc)

        trade = ShadowTrade(
            account_id=acc.id,
            thesis_id=99999,  # doesn't exist
            opened_at=datetime.now(UTC),
            underlying="AAPL",
            occ_symbol="AAPL301220C00315000",
            option_type="call",
            strike=Decimal("315"),
            expiration=date.today() + timedelta(days=30),
            contracts=1,
            premium_per_contract_usd=Decimal("4.60"),
            total_cost_usd=Decimal("460.00"),
            risk_reason="test",
        )
        session.add(trade)
        with pytest.raises(IntegrityError):
            await session.commit()
