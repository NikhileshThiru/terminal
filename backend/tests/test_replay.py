"""Replay mode: rebuild events from the decision log, compare, summarize —
and never write anything the eval reads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.discovery import replay
from app.discovery.models import TriageDecisionRow
from app.discovery.triage import TriageDecision
from app.eval import persistence
from app.eval.models import Base
from app.eval.models import Thesis as ThesisRow


@pytest.fixture
async def factory(monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(persistence, "get_session_factory", lambda: f)
    yield f
    await engine.dispose()


async def _seed(f: async_sessionmaker, n: int, *, passed: bool, reason: str = "model call") -> None:
    now = datetime.now(UTC)
    async with f() as s:
        for i in range(n):
            s.add(
                TriageDecisionRow(
                    event_id=f"{'p' if passed else 'd'}-{reason[:3]}-{i}",
                    symbol="AAPL",
                    headline=f"headline {i}",
                    source="alpaca-news",
                    kind="news",
                    passed=passed,
                    reason=reason,
                    confidence=0.8,
                    decided_at=now - timedelta(minutes=i),
                    published_at=now - timedelta(minutes=i),
                )
            )
        await s.commit()


class _Flip:
    """Replay model that drops everything."""

    name = "stub"

    async def complete_structured(self, **_: Any) -> TriageDecision:
        return TriageDecision(passed=False, reason="now looks routine", confidence=0.7)


@pytest.mark.asyncio
async def test_balanced_sample_skips_non_model_decisions_and_scores_flips(factory) -> None:
    await _seed(factory, 5, passed=True)
    await _seed(factory, 5, passed=False)
    await _seed(factory, 5, passed=False, reason="off-session: market closed")
    await _seed(factory, 5, passed=False, reason="skipped: stale")

    pairs = await replay.load_from_db(since=None, limit=6, balanced=True)
    assert len(pairs) == 6
    assert sum(1 for _, o in pairs if o and o.passed) == 3  # half passes
    assert all(not o.reason.startswith(("off-session", "skipped:")) for _, o in pairs if o)

    rows = await replay.replay(pairs, _Flip())
    summary = replay.summarize(rows)
    assert summary["compared"] == 6
    assert summary["outcomes"] == {"pass->drop": 3, "agree": 3}
    assert summary["agreement"] == 0.5
    assert summary["replay_pass_rate"] == 0.0


@pytest.mark.asyncio
async def test_replay_writes_nothing_the_eval_reads(factory) -> None:
    await _seed(factory, 3, passed=True)
    before = await _counts(factory)
    await replay.replay(await replay.load_from_db(since=None, limit=3, balanced=False), _Flip())
    assert await _counts(factory) == before


async def _counts(f: async_sessionmaker) -> tuple[int, int, list[bool]]:
    async with f() as s:
        theses = (await s.execute(select(func.count(ThesisRow.id)))).scalar_one()
        decisions = (await s.execute(select(func.count(TriageDecisionRow.id)))).scalar_one()
        passed = list((await s.execute(select(TriageDecisionRow.passed))).scalars())
    return theses, decisions, passed
