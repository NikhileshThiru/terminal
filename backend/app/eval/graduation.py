"""Shadow -> paper graduation gate (CLAUDE.md §8).

Before the autonomous agent may place paper trades (rather than only
logging shadow trades), all three must hold on the *autonomous* theses
(reactive + catalyst buckets — manual copilot theses are prompted by a human
and don't count):

1. Volume:       at least 50 shadow theses logged.
2. Grounding:    pass rate >= 95%, measured over thesis *attempts*
                 (rejected theses never reach the `theses` table, so the
                 denominator comes from `thesis_attempts`).
3. Calibration:  Brier score on resolved shadow theses no worse than the
                 always-50% baseline (0.25).

This module only *reports*. Nothing flips automatically: graduating is a
config change a human makes after reading this — consistent with the
permanent human-in-the-loop rule (CLAUDE.md §2.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.eval.models import Thesis, ThesisAttempt, ThesisOutcome
from app.eval.scoring import ConfidenceHitPair, brier_score, hit_rate

SHADOW_BUCKETS = ("reactive", "catalyst")
MIN_SHADOW_THESES = 50
MIN_GROUNDING_PASS_RATE = 0.95
MAX_BRIER = 0.25


@dataclass
class Criterion:
    key: str
    label: str
    value: float | None
    threshold: float
    comparator: str  # ">=" | "<="
    passed: bool
    detail: str


@dataclass
class GraduationReport:
    ready: bool
    criteria: list[Criterion]
    shadow_theses: int
    resolved: int
    attempts: int
    grounding_rejected: int
    hit_rate: float | None
    attempts_tracked_since: datetime | None
    notes: list[str] = field(default_factory=list)


async def graduation_report(session: AsyncSession) -> GraduationReport:
    theses = list(
        (
            await session.execute(
                select(Thesis)
                .where(Thesis.source_bucket.in_(SHADOW_BUCKETS))
                .where(Thesis.pre_strictness.is_(False))
            )
        )
        .scalars()
        .all()
    )
    outcomes = {
        o.thesis_id: o
        for o in (
            await session.execute(
                select(ThesisOutcome).where(ThesisOutcome.thesis_id.in_([t.id for t in theses]))
            )
        )
        .scalars()
        .all()
    }
    pairs = [
        ConfidenceHitPair(confidence=t.confidence, hit=outcomes[t.id].hit)
        for t in theses
        if t.id in outcomes
    ]

    grounded = await _count_attempts(session, "grounded")
    rejected = await _count_attempts(session, "grounding_rejected")
    since = await session.scalar(
        select(func.min(ThesisAttempt.attempted_at)).where(
            ThesisAttempt.source_bucket.in_(SHADOW_BUCKETS)
        )
    )
    judged = grounded + rejected
    pass_rate = grounded / judged if judged else None
    brier = brier_score(pairs) if pairs else None

    notes: list[str] = []
    if judged == 0:
        notes.append(
            "Grounding pass rate needs thesis attempts; attempt tracking started "
            "2026-10-09, so older theses (which all passed — rejects weren't logged) "
            "aren't in the denominator."
        )
    if pairs and len(pairs) < 20:
        notes.append(
            f"Brier is over only {len(pairs)} resolved theses — too few to trust either way."
        )

    criteria = [
        Criterion(
            key="volume",
            label="Shadow theses logged",
            value=float(len(theses)),
            threshold=MIN_SHADOW_THESES,
            comparator=">=",
            passed=len(theses) >= MIN_SHADOW_THESES,
            detail=f"{len(theses)} of {MIN_SHADOW_THESES} autonomous theses",
        ),
        Criterion(
            key="grounding",
            label="Grounding pass rate",
            value=pass_rate,
            threshold=MIN_GROUNDING_PASS_RATE,
            comparator=">=",
            passed=pass_rate is not None and pass_rate >= MIN_GROUNDING_PASS_RATE,
            detail=(
                f"{grounded} grounded / {judged} judged attempts"
                if judged
                else "no attempts recorded yet"
            ),
        ),
        Criterion(
            key="calibration",
            label="Brier score (resolved shadow theses)",
            value=brier,
            threshold=MAX_BRIER,
            comparator="<=",
            passed=brier is not None and brier <= MAX_BRIER,
            detail=(
                f"over {len(pairs)} resolved; 0.25 = always guessing 50%"
                if pairs
                else "no resolved theses yet"
            ),
        ),
    ]
    return GraduationReport(
        ready=all(c.passed for c in criteria),
        criteria=criteria,
        shadow_theses=len(theses),
        resolved=len(pairs),
        attempts=judged,
        grounding_rejected=rejected,
        hit_rate=hit_rate(pairs) if pairs else None,
        attempts_tracked_since=since,
        notes=notes,
    )


async def _count_attempts(session: AsyncSession, outcome: str) -> int:
    return int(
        await session.scalar(
            select(func.count(ThesisAttempt.id))
            .where(ThesisAttempt.source_bucket.in_(SHADOW_BUCKETS))
            .where(ThesisAttempt.outcome == outcome)
        )
        or 0
    )
