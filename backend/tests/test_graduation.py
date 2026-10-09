"""Shadow -> paper graduation gate (CLAUDE.md §8): all three criteria, on
autonomous theses only, with an honest grounding denominator."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.eval.graduation import graduation_report
from app.eval.models import Base, Thesis, ThesisAttempt, ThesisOutcome


@pytest.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(
    factory: async_sessionmaker,
    *,
    bucket: str,
    n: int,
    confidence: float,
    hit: bool | None,
    pre_strictness: bool = False,
) -> None:
    async with factory() as s:
        for _ in range(n):
            t = Thesis(
                correlation_id="c" * 16,
                source_bucket=bucket,
                symbol="AAPL",
                generated_at=datetime.now(UTC),
                direction="long",
                confidence=confidence,
                prediction_window_days=7,
                reasoning="r",
                grounding_check_passed=True,
                llm_provider="gemini",
                llm_model="gemini-2.5-flash",
                pre_strictness=pre_strictness,
            )
            s.add(t)
            await s.flush()
            if hit is not None:
                s.add(
                    ThesisOutcome(
                        thesis_id=t.id,
                        evaluated_at=datetime.now(UTC),
                        realized_direction="long" if hit else "short",
                        hit=hit,
                        underlying_price_at_thesis=Decimal(100),
                        underlying_price_at_eval=Decimal(101),
                        pct_move=1.0,
                    )
                )
        await s.commit()


async def _attempts(factory: async_sessionmaker, bucket: str, outcome: str, n: int) -> None:
    async with factory() as s:
        for _ in range(n):
            s.add(
                ThesisAttempt(
                    attempted_at=datetime.now(UTC),
                    source_bucket=bucket,
                    outcome=outcome,
                    correlation_id="c" * 16,
                    latency_ms=1000,
                )
            )
        await s.commit()


@pytest.mark.asyncio
async def test_not_ready_with_little_data_and_explains_why(factory) -> None:
    await _seed(factory, bucket="reactive", n=5, confidence=0.7, hit=True)
    async with factory() as s:
        r = await graduation_report(s)
    assert not r.ready
    by_key = {c.key: c for c in r.criteria}
    assert by_key["volume"].value == 5 and not by_key["volume"].passed
    assert by_key["grounding"].value is None and not by_key["grounding"].passed
    assert any("attempt tracking started" in n for n in r.notes)
    assert any("too few to trust" in n for n in r.notes)


@pytest.mark.asyncio
async def test_ready_when_all_three_criteria_hold(factory) -> None:
    # 50 autonomous theses; confidence 0.7 with 70% hits -> Brier 0.21 < 0.25.
    await _seed(factory, bucket="reactive", n=35, confidence=0.7, hit=True)
    await _seed(factory, bucket="catalyst", n=15, confidence=0.7, hit=False)
    await _attempts(factory, "reactive", "grounded", 48)
    await _attempts(factory, "catalyst", "grounding_rejected", 2)  # 96% pass
    await _attempts(factory, "reactive", "failed", 30)  # quota errors: not judged
    async with factory() as s:
        r = await graduation_report(s)
    assert r.ready, r.criteria
    by_key = {c.key: c for c in r.criteria}
    assert by_key["grounding"].value == pytest.approx(0.96)
    assert by_key["calibration"].value == pytest.approx(0.21)
    assert r.attempts == 50


@pytest.mark.asyncio
async def test_manual_and_pre_strictness_theses_do_not_count(factory) -> None:
    await _seed(factory, bucket="manual", n=60, confidence=0.9, hit=True)
    await _seed(factory, bucket="reactive", n=60, confidence=0.9, hit=True, pre_strictness=True)
    await _attempts(factory, "manual", "grounded", 60)
    async with factory() as s:
        r = await graduation_report(s)
    assert r.shadow_theses == 0
    assert r.attempts == 0


@pytest.mark.asyncio
async def test_overconfident_model_fails_calibration(factory) -> None:
    await _seed(factory, bucket="reactive", n=50, confidence=0.9, hit=False)
    await _attempts(factory, "reactive", "grounded", 50)
    async with factory() as s:
        r = await graduation_report(s)
    by_key = {c.key: c for c in r.criteria}
    assert by_key["volume"].passed and by_key["grounding"].passed
    assert by_key["calibration"].value == pytest.approx(0.81)
    assert not by_key["calibration"].passed
    assert not r.ready
