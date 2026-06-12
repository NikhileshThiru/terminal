"""/eval — the eval harness's read API (DESIGN.md §8).

Aggregates the `theses` + `thesis_outcomes` tables into the per-bucket
metrics the dashboard renders. Honest about empty buckets: returns null
metrics rather than zeros when a bucket has zero resolved outcomes (a
0.0 Brier with N=0 is meaningless and misleading).

Pre-strictness theses (DESIGN.md §4.5 GOOG hallucination evidence row) are
excluded by default; pass `include_pre_strictness=true` to opt back in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from app.eval.models import SourceBucket, ThesisOutcome
from app.eval.models import Thesis as ThesisRow
from app.eval.persistence import get_session_factory
from app.eval.scoring import (
    CalibrationBucket,
    ConfidenceHitPair,
    brier_score,
    calibration_buckets,
    hit_rate,
)

router = APIRouter(prefix="/eval", tags=["eval"])

_BUCKETS: tuple[str, ...] = tuple(b.value for b in SourceBucket)


class BucketSummary(BaseModel):
    """Per-source-bucket aggregate. Nulls when there's not enough data yet."""

    bucket: Literal["manual", "reactive", "catalyst"]
    count_theses: int = Field(description="All theses in this bucket (including unresolved).")
    count_resolved: int = Field(description="Theses with an outcome row.")
    brier: float | None = Field(
        description=(
            "Brier score on resolved theses. Lower is better; 0.25 = always-50% baseline. "
            "Null when count_resolved is zero."
        )
    )
    hit_rate: float | None = Field(
        description="Fraction of resolved theses where direction matched. Null when N=0."
    )


class CalibrationPoint(BaseModel):
    """One bucket on the calibration plot."""

    bucket_lower: float
    bucket_upper: float
    count: int
    mean_confidence: float
    realized_hit_rate: float


class CalibrationResponse(BaseModel):
    bucket: Literal["manual", "reactive", "catalyst"]
    n_buckets: int
    points: list[CalibrationPoint]


class EvalSummaryResponse(BaseModel):
    buckets: list[BucketSummary]


def _calibration_to_point(b: CalibrationBucket) -> CalibrationPoint:
    return CalibrationPoint(
        bucket_lower=b.lower,
        bucket_upper=b.upper,
        count=b.count,
        mean_confidence=b.mean_confidence,
        realized_hit_rate=b.realized_hit_rate,
    )


@router.get("/summary", response_model=EvalSummaryResponse)
async def eval_summary(
    include_pre_strictness: bool = Query(default=False),
) -> EvalSummaryResponse:
    """Per-bucket Brier + hit rate + counts."""
    factory = get_session_factory()
    out: list[BucketSummary] = []
    async with factory() as session:
        for bucket in _BUCKETS:
            theses_q = select(ThesisRow).where(ThesisRow.source_bucket == bucket)
            if not include_pre_strictness:
                theses_q = theses_q.where(ThesisRow.pre_strictness.is_(False))
            theses = list((await session.execute(theses_q)).scalars().all())

            if not theses:
                out.append(
                    BucketSummary(
                        bucket=bucket,
                        count_theses=0,
                        count_resolved=0,
                        brier=None,
                        hit_rate=None,
                    )
                )
                continue

            # Join with outcomes via a single query per bucket.
            thesis_ids = [t.id for t in theses]
            outcomes = list(
                (
                    await session.execute(
                        select(ThesisOutcome).where(ThesisOutcome.thesis_id.in_(thesis_ids))
                    )
                )
                .scalars()
                .all()
            )
            outcome_by_id = {o.thesis_id: o for o in outcomes}

            pairs = [
                ConfidenceHitPair(confidence=t.confidence, hit=outcome_by_id[t.id].hit)
                for t in theses
                if t.id in outcome_by_id
            ]

            out.append(
                BucketSummary(
                    bucket=bucket,
                    count_theses=len(theses),
                    count_resolved=len(pairs),
                    brier=brier_score(pairs) if pairs else None,
                    hit_rate=hit_rate(pairs) if pairs else None,
                )
            )
    return EvalSummaryResponse(buckets=out)


@router.get("/calibration", response_model=CalibrationResponse)
async def eval_calibration(
    bucket: Literal["manual", "reactive", "catalyst"] = Query(default="manual"),
    n_buckets: int = Query(default=10, ge=2, le=20),
    include_pre_strictness: bool = Query(default=False),
) -> CalibrationResponse:
    """Calibration plot data for one source bucket.

    Returns one point per non-empty confidence bucket: mean stated confidence
    vs realized hit rate. A perfectly calibrated model has y == x on this plot.
    """
    factory = get_session_factory()
    async with factory() as session:
        theses_q = select(ThesisRow).where(ThesisRow.source_bucket == bucket)
        if not include_pre_strictness:
            theses_q = theses_q.where(ThesisRow.pre_strictness.is_(False))
        theses = list((await session.execute(theses_q)).scalars().all())

        outcomes = list(
            (
                await session.execute(
                    select(ThesisOutcome).where(ThesisOutcome.thesis_id.in_([t.id for t in theses]))
                )
            )
            .scalars()
            .all()
        )
        outcome_by_id = {o.thesis_id: o for o in outcomes}

        pairs = [
            ConfidenceHitPair(confidence=t.confidence, hit=outcome_by_id[t.id].hit)
            for t in theses
            if t.id in outcome_by_id
        ]
    points = [_calibration_to_point(b) for b in calibration_buckets(pairs, n_buckets=n_buckets)]
    return CalibrationResponse(bucket=bucket, n_buckets=n_buckets, points=points)


class OutcomeRow(BaseModel):
    """One resolved prediction — the eval page's 'what actually happened' feed."""

    thesis_id: int
    symbol: str
    source_bucket: Literal["manual", "reactive", "catalyst"]
    direction: str
    confidence: float
    generated_at: datetime
    evaluated_at: datetime
    realized_direction: str
    hit: bool
    pct_move: float
    underlying_price_at_thesis: float
    underlying_price_at_eval: float
    notes: str | None


@router.get("/outcomes", response_model=list[OutcomeRow])
async def eval_outcomes(
    bucket: Literal["manual", "reactive", "catalyst"] | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=200),
    include_pre_strictness: bool = Query(default=False),
) -> list[OutcomeRow]:
    """Recent resolved outcomes, newest first. Each row is one prediction
    graded against what the underlying actually did."""
    factory = get_session_factory()
    async with factory() as session:
        stmt = (
            select(ThesisOutcome, ThesisRow)
            .join(ThesisRow, ThesisOutcome.thesis_id == ThesisRow.id)
            .order_by(desc(ThesisOutcome.evaluated_at))
            .limit(limit)
        )
        if bucket:
            stmt = stmt.where(ThesisRow.source_bucket == bucket)
        if not include_pre_strictness:
            stmt = stmt.where(ThesisRow.pre_strictness.is_(False))
        rows = (await session.execute(stmt)).all()
    return [
        OutcomeRow(
            thesis_id=t.id,
            symbol=t.symbol,
            source_bucket=t.source_bucket,
            direction=t.direction,
            confidence=t.confidence,
            generated_at=t.generated_at,
            evaluated_at=o.evaluated_at,
            realized_direction=o.realized_direction,
            hit=o.hit,
            pct_move=o.pct_move,
            underlying_price_at_thesis=float(o.underlying_price_at_thesis),
            underlying_price_at_eval=float(o.underlying_price_at_eval),
            notes=o.notes,
        )
        for (o, t) in rows
    ]
