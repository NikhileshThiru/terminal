"""Thesis persistence — uses an isolated in-memory SQLite for each test."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.schemas import SuggestedContract
from app.agent.schemas import Thesis as ThesisDTO
from app.eval.models import Base
from app.eval.models import Thesis as ThesisRow


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _make_thesis() -> ThesisDTO:
    return ThesisDTO(
        symbol="AAPL",
        direction="long",
        confidence=0.65,
        reasoning="AAPL traded at $312.06 with 24 buy ratings — bullish setup.",
        prediction_window_days=14,
        suggested_contract=SuggestedContract(
            underlying="AAPL",
            occ_symbol="AAPL260620C00315000",
            option_type="call",
            strike=Decimal("315"),
            expiration=date(2026, 6, 20),
            estimated_premium_per_contract=Decimal("4.60"),
            contracts=1,
            max_risk_usd=Decimal("460"),
        ),
        what_must_happen="AAPL closes above $315 by expiry.",
        correlation_id="test-corr-id",
        source_bucket="manual",
        generated_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        grounding_check_passed=True,
        llm_provider="gemini",
        llm_model="gemini-2.5-flash",
        funnel_latency_ms=4500,
    )


@pytest.mark.asyncio
async def test_thesis_roundtrips_through_db(session_factory) -> None:
    thesis = _make_thesis()
    async with session_factory() as session:
        row = ThesisRow(**thesis.to_orm_kwargs())
        session.add(row)
        await session.commit()
        thesis_id = row.id

    async with session_factory() as session:
        fetched = (
            await session.execute(select(ThesisRow).where(ThesisRow.id == thesis_id))
        ).scalar_one()
        assert fetched.symbol == "AAPL"
        assert fetched.direction == "long"
        assert fetched.confidence == 0.65
        assert fetched.suggested_contract["occ_symbol"] == "AAPL260620C00315000"
        assert fetched.correlation_id == "test-corr-id"
        assert fetched.source_bucket == "manual"
        assert fetched.grounding_check_passed is True
        assert fetched.llm_provider == "gemini"
        assert fetched.funnel_latency_ms == 4500
