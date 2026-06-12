"""Account seeding: idempotent + creates the two defaults on first run."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.eval.models import Base
from app.portfolio.models import AccountKind, PaperAccount
from app.portfolio.seed import seed_default_accounts


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_first_run_creates_both_accounts(session_factory) -> None:
    created = await seed_default_accounts(session_factory)
    assert sorted(created) == ["aggressive", "conservative"]

    async with session_factory() as session:
        kinds = (await session.execute(select(PaperAccount.kind))).scalars().all()
    assert sorted(kinds) == ["aggressive", "conservative"]


@pytest.mark.asyncio
async def test_second_run_is_noop(session_factory) -> None:
    await seed_default_accounts(session_factory)
    second = await seed_default_accounts(session_factory)
    assert second == []  # nothing new created

    async with session_factory() as session:
        kinds = (await session.execute(select(PaperAccount.kind))).scalars().all()
    assert sorted(kinds) == ["aggressive", "conservative"]


@pytest.mark.asyncio
async def test_partial_state_only_creates_missing(session_factory) -> None:
    """If conservative exists but aggressive doesn't, seed creates only aggressive."""
    from datetime import UTC, datetime
    from decimal import Decimal

    async with session_factory() as session:
        session.add(
            PaperAccount(
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
        )
        await session.commit()

    created = await seed_default_accounts(session_factory)
    assert created == ["aggressive"]


@pytest.mark.asyncio
async def test_default_account_risk_config_matches_claudemd(session_factory) -> None:
    """DESIGN.md §8: conservative = higher threshold + smaller; aggressive = lower + bigger."""
    await seed_default_accounts(session_factory)
    async with session_factory() as session:
        accounts = {
            a.kind: a for a in (await session.execute(select(PaperAccount))).scalars().all()
        }

    cons = accounts["conservative"]
    aggr = accounts["aggressive"]
    assert cons.min_confidence > aggr.min_confidence
    assert cons.max_trade_cost_usd < aggr.max_trade_cost_usd
    assert cons.max_trades_per_day < aggr.max_trades_per_day


@pytest.mark.asyncio
async def test_seed_reads_from_config(session_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Risk knobs flow from settings (DESIGN.md §2.7 config-driven)."""
    from app.core.config import get_settings

    monkeypatch.setenv("CONSERVATIVE_ACCOUNT_MIN_CONFIDENCE", "0.85")
    monkeypatch.setenv("CONSERVATIVE_ACCOUNT_MAX_TRADE_COST_USD", "250.0")
    monkeypatch.setenv("AGGRESSIVE_ACCOUNT_MAX_TRADES_PER_DAY", "12")
    get_settings.cache_clear()
    try:
        await seed_default_accounts(session_factory)
        async with session_factory() as session:
            accounts = {
                a.kind: a for a in (await session.execute(select(PaperAccount))).scalars().all()
            }
        assert accounts["conservative"].min_confidence == 0.85
        assert float(accounts["conservative"].max_trade_cost_usd) == 250.0
        assert accounts["aggressive"].max_trades_per_day == 12
    finally:
        get_settings.cache_clear()
