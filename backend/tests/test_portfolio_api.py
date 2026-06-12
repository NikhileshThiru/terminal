"""Portfolio API integration tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.eval import persistence
from app.eval.models import Base
from app.eval.models import Thesis as ThesisRow
from app.main import create_app
from app.portfolio.models import AccountKind, PaperAccount, ShadowTrade


@pytest.fixture(autouse=True)
async def _isolated_db(monkeypatch: pytest.MonkeyPatch):
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


async def _seed_accounts() -> tuple[int, int]:
    factory = persistence.get_session_factory()
    async with factory() as session:
        c = PaperAccount(
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
        a = PaperAccount(
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
        session.add(c)
        session.add(a)
        await session.commit()
        return c.id, a.id


async def _seed_thesis() -> int:
    factory = persistence.get_session_factory()
    async with factory() as session:
        t = ThesisRow(
            correlation_id="x" * 16,
            source_bucket="reactive",
            symbol="AAPL",
            generated_at=datetime.now(UTC),
            direction="long",
            confidence=0.7,
            prediction_window_days=14,
            reasoning="test",
            suggested_contract={"occ_symbol": "AAPL301220C00150000"},
            grounding_check_passed=True,
            llm_provider="gemini",
            llm_model="gemini-2.5-flash",
            funnel_latency_ms=3000,
        )
        session.add(t)
        await session.commit()
        return t.id


async def _seed_shadow_trade(account_id: int, thesis_id: int, **overrides: Any) -> int:
    factory = persistence.get_session_factory()
    defaults = {
        "underlying": "AAPL",
        "occ_symbol": "AAPL301220C00150000",
        "option_type": "call",
        "strike": Decimal("150"),
        "expiration": date.today() + timedelta(days=30),
        "contracts": 1,
        "premium_per_contract_usd": Decimal("2.00"),
        "total_cost_usd": Decimal("200.00"),
        "status": "shadow_open",
        "risk_reason": "test reason",
    }
    defaults.update(overrides)
    async with factory() as session:
        t = ShadowTrade(
            account_id=account_id,
            thesis_id=thesis_id,
            opened_at=datetime.now(UTC),
            **defaults,
        )
        session.add(t)
        await session.commit()
        return t.id


def _client() -> TestClient:
    return TestClient(create_app())


@pytest.mark.asyncio
async def test_accounts_endpoint_returns_both_with_stats() -> None:
    c_id, a_id = await _seed_accounts()
    th_id = await _seed_thesis()
    await _seed_shadow_trade(c_id, th_id, total_cost_usd=Decimal("250"))
    await _seed_shadow_trade(a_id, th_id, total_cost_usd=Decimal("450"))
    await _seed_shadow_trade(a_id, th_id, total_cost_usd=Decimal("400"))

    r = _client().get("/portfolio/accounts")
    assert r.status_code == 200
    body = r.json()
    by_kind = {acc["kind"]: acc for acc in body}
    assert {"conservative", "aggressive"} <= set(by_kind)
    assert by_kind["conservative"]["shadow_trades_total"] == 1
    assert by_kind["aggressive"]["shadow_trades_total"] == 2
    assert by_kind["aggressive"]["min_confidence"] == 0.5
    assert by_kind["conservative"]["min_confidence"] == 0.7


@pytest.mark.asyncio
async def test_shadow_trades_endpoint_returns_recent() -> None:
    _, a_id = await _seed_accounts()
    th_id = await _seed_thesis()
    for _ in range(3):
        await _seed_shadow_trade(a_id, th_id)

    r = _client().get("/portfolio/shadow-trades?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 3
    assert body[0]["account_kind"] == "aggressive"
    assert body[0]["underlying"] == "AAPL"


@pytest.mark.asyncio
async def test_shadow_trades_filter_by_account_kind() -> None:
    c_id, a_id = await _seed_accounts()
    th_id = await _seed_thesis()
    await _seed_shadow_trade(c_id, th_id)
    await _seed_shadow_trade(a_id, th_id)
    await _seed_shadow_trade(a_id, th_id)

    r = _client().get("/portfolio/shadow-trades?account_kind=aggressive&limit=10")
    body = r.json()
    assert len(body) == 2
    assert all(t["account_kind"] == "aggressive" for t in body)


@pytest.mark.asyncio
async def test_accounts_endpoint_no_trades_yet() -> None:
    await _seed_accounts()
    r = _client().get("/portfolio/accounts")
    body = r.json()
    for acc in body:
        assert acc["shadow_trades_total"] == 0
        assert acc["open_shadow_positions"] == 0
