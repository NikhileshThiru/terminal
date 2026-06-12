"""/eval/{summary,calibration} integration tests with seeded outcomes."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.eval import persistence
from app.eval.models import Base, ThesisOutcome
from app.eval.models import Thesis as ThesisRow
from app.main import create_app


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
    yield factory
    await engine.dispose()


async def _seed_thesis_and_outcome(
    factory: async_sessionmaker,
    *,
    bucket: str,
    confidence: float,
    hit: bool,
    pre_strictness: bool = False,
    direction: str = "long",
) -> None:
    async with factory() as session:
        thesis = ThesisRow(
            correlation_id="x" * 16,
            source_bucket=bucket,
            symbol="AAPL",
            generated_at=datetime.now(UTC),
            direction=direction,
            confidence=confidence,
            prediction_window_days=14,
            reasoning="test",
            suggested_contract={},
            grounding_check_passed=True,
            llm_provider="gemini",
            llm_model="gemini-2.5-flash",
            funnel_latency_ms=5000,
            pre_strictness=pre_strictness,
        )
        session.add(thesis)
        await session.flush()
        outcome = ThesisOutcome(
            thesis_id=thesis.id,
            evaluated_at=datetime.now(UTC),
            realized_direction="long" if hit and direction == "long" else "short",
            hit=hit,
            underlying_price_at_thesis=Decimal("100"),
            underlying_price_at_eval=Decimal("110" if hit else "90"),
            pct_move=10.0 if hit else -10.0,
            notes="seed",
        )
        session.add(outcome)
        await session.commit()


async def _seed_thesis_only(factory: async_sessionmaker, *, bucket: str) -> None:
    """A thesis with no outcome — counts in count_theses, not count_resolved."""
    async with factory() as session:
        thesis = ThesisRow(
            correlation_id="x" * 16,
            source_bucket=bucket,
            symbol="AAPL",
            generated_at=datetime.now(UTC),
            direction="long",
            confidence=0.6,
            prediction_window_days=14,
            reasoning="test",
            suggested_contract={},
            grounding_check_passed=True,
            llm_provider="gemini",
            llm_model="gemini-2.5-flash",
            funnel_latency_ms=5000,
        )
        session.add(thesis)
        await session.commit()


def _client() -> TestClient:
    return TestClient(create_app())


# === /eval/summary ===


@pytest.mark.asyncio
async def test_summary_empty_db_returns_null_metrics(_isolated_db) -> None:
    r = _client().get("/eval/summary")
    assert r.status_code == 200
    body = r.json()
    assert len(body["buckets"]) == 3
    for b in body["buckets"]:
        assert b["count_theses"] == 0
        assert b["count_resolved"] == 0
        assert b["brier"] is None
        assert b["hit_rate"] is None


@pytest.mark.asyncio
async def test_summary_counts_theses_with_and_without_outcomes(_isolated_db) -> None:
    factory = _isolated_db
    await _seed_thesis_and_outcome(factory, bucket="manual", confidence=0.7, hit=True)
    await _seed_thesis_only(factory, bucket="manual")
    r = _client().get("/eval/summary")
    body = r.json()
    manual = next(b for b in body["buckets"] if b["bucket"] == "manual")
    assert manual["count_theses"] == 2
    assert manual["count_resolved"] == 1
    # One thesis, hit=True, confidence=0.7 → Brier = (0.7 - 1)^2 = 0.09
    assert abs(manual["brier"] - 0.09) < 1e-6
    assert manual["hit_rate"] == 1.0


@pytest.mark.asyncio
async def test_summary_excludes_pre_strictness_by_default(_isolated_db) -> None:
    factory = _isolated_db
    await _seed_thesis_and_outcome(
        factory, bucket="manual", confidence=0.8, hit=False, pre_strictness=True
    )
    await _seed_thesis_and_outcome(
        factory, bucket="manual", confidence=0.6, hit=True, pre_strictness=False
    )
    r = _client().get("/eval/summary")
    manual = next(b for b in r.json()["buckets"] if b["bucket"] == "manual")
    # Only the post-strictness thesis is counted.
    assert manual["count_theses"] == 1
    assert manual["count_resolved"] == 1


@pytest.mark.asyncio
async def test_summary_include_pre_strictness_flag(_isolated_db) -> None:
    factory = _isolated_db
    await _seed_thesis_and_outcome(
        factory, bucket="manual", confidence=0.8, hit=False, pre_strictness=True
    )
    r = _client().get("/eval/summary?include_pre_strictness=true")
    manual = next(b for b in r.json()["buckets"] if b["bucket"] == "manual")
    assert manual["count_theses"] == 1


@pytest.mark.asyncio
async def test_summary_separates_buckets(_isolated_db) -> None:
    factory = _isolated_db
    await _seed_thesis_and_outcome(factory, bucket="manual", confidence=0.7, hit=True)
    await _seed_thesis_and_outcome(factory, bucket="reactive", confidence=0.5, hit=False)
    r = _client().get("/eval/summary")
    by_b = {b["bucket"]: b for b in r.json()["buckets"]}
    assert by_b["manual"]["count_resolved"] == 1
    assert by_b["reactive"]["count_resolved"] == 1
    # Manual hit, reactive missed; the two should have different scores.
    assert by_b["manual"]["hit_rate"] != by_b["reactive"]["hit_rate"]


# === /eval/calibration ===


@pytest.mark.asyncio
async def test_calibration_empty_bucket_returns_no_points(_isolated_db) -> None:
    r = _client().get("/eval/calibration?bucket=manual")
    body = r.json()
    assert body["bucket"] == "manual"
    assert body["points"] == []


@pytest.mark.asyncio
async def test_calibration_returns_one_point_per_nonempty_confidence_bucket(_isolated_db) -> None:
    factory = _isolated_db
    # Two theses in the 0.7-0.8 bucket, both hit.
    await _seed_thesis_and_outcome(factory, bucket="manual", confidence=0.71, hit=True)
    await _seed_thesis_and_outcome(factory, bucket="manual", confidence=0.79, hit=True)
    # One thesis in 0.3-0.4 bucket, missed.
    await _seed_thesis_and_outcome(factory, bucket="manual", confidence=0.35, hit=False)
    r = _client().get("/eval/calibration?bucket=manual&n_buckets=10")
    points: list[dict[str, Any]] = r.json()["points"]
    assert len(points) == 2
    # The high-confidence bucket should have realized_hit_rate=1.0.
    high = next(p for p in points if p["bucket_lower"] >= 0.7)
    assert high["count"] == 2
    assert high["realized_hit_rate"] == 1.0
    low = next(p for p in points if p["bucket_lower"] < 0.4)
    assert low["count"] == 1
    assert low["realized_hit_rate"] == 0.0


@pytest.mark.asyncio
async def test_outcomes_endpoint_returns_resolved_rows(_isolated_db: Any) -> None:
    await _seed_thesis_and_outcome(_isolated_db, bucket="manual", confidence=0.7, hit=True)
    await _seed_thesis_and_outcome(_isolated_db, bucket="catalyst", confidence=0.6, hit=False)

    client = TestClient(create_app())
    r = client.get("/eval/outcomes")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    by_bucket = {row["source_bucket"]: row for row in rows}
    assert by_bucket["manual"]["hit"] is True
    assert by_bucket["manual"]["symbol"] == "AAPL"
    assert by_bucket["catalyst"]["hit"] is False
    assert by_bucket["catalyst"]["pct_move"] == -10.0


@pytest.mark.asyncio
async def test_outcomes_endpoint_filters_by_bucket_and_excludes_pre_strictness(
    _isolated_db: Any,
) -> None:
    await _seed_thesis_and_outcome(_isolated_db, bucket="manual", confidence=0.7, hit=True)
    await _seed_thesis_and_outcome(
        _isolated_db, bucket="manual", confidence=0.9, hit=False, pre_strictness=True
    )
    await _seed_thesis_and_outcome(_isolated_db, bucket="reactive", confidence=0.5, hit=True)

    client = TestClient(create_app())
    # Bucket filter.
    r = client.get("/eval/outcomes?bucket=manual")
    assert r.status_code == 200
    rows = r.json()
    # Pre-strictness row excluded by default.
    assert len(rows) == 1
    assert rows[0]["confidence"] == 0.7

    # Opt back in.
    r2 = client.get("/eval/outcomes?bucket=manual&include_pre_strictness=true")
    assert len(r2.json()) == 2
