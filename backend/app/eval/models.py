"""SQLAlchemy ORM models for the eval harness (DESIGN.md §8).

Tables:
- `theses` — every generated thesis (manual / reactive / catalyst-driven)
- `thesis_outcomes` — forward-test results, populated by the reconciliation job

Scoring (Brier, hit rate, calibration) is computed on the fly from these
tables in app/eval/scoring.py; no separate scores table needed.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class SourceBucket(StrEnum):
    """Three buckets scored separately (DESIGN.md §8: scoring per source).

    Mixing them hides whether the model has edge in any specific path.
    """

    MANUAL = "manual"
    REACTIVE = "reactive"
    CATALYST = "catalyst"


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"


class RealizedDirection(StrEnum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"  # move within noise — neither up nor down meaningfully


class Thesis(Base):
    __tablename__ = "theses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    correlation_id: Mapped[str] = mapped_column(String(32), index=True)
    source_bucket: Mapped[str] = mapped_column(String(16), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    direction: Mapped[str] = mapped_column(String(8))
    confidence: Mapped[float] = mapped_column(Float)  # in [0, 1]
    prediction_window_days: Mapped[int] = mapped_column(Integer)

    reasoning: Mapped[str] = mapped_column(Text)
    suggested_contract: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    grounding_check_passed: Mapped[bool] = mapped_column(Boolean, default=True)
    llm_provider: Mapped[str] = mapped_column(String(32))
    llm_model: Mapped[str] = mapped_column(String(64))
    funnel_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Theses produced before the Phase 4.5 strictness fixes (e.g., the GOOG
    # hallucination at id=1) are kept as regression-test evidence but excluded
    # from scoring + dashboards. After Phase 4.5 the orchestrator hard-rejects
    # grounding failures, so any new row should always have this False.
    pre_strictness: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    outcome: Mapped[ThesisOutcome | None] = relationship(back_populates="thesis", uselist=False)

    __table_args__ = (Index("idx_theses_bucket_generated", "source_bucket", "generated_at"),)


class ThesisOutcome(Base):
    __tablename__ = "thesis_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thesis_id: Mapped[int] = mapped_column(ForeignKey("theses.id"), unique=True, index=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    realized_direction: Mapped[str] = mapped_column(String(8))
    hit: Mapped[bool] = mapped_column(Boolean)
    underlying_price_at_thesis: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    underlying_price_at_eval: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    pct_move: Mapped[float] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    thesis: Mapped[Thesis] = relationship(back_populates="outcome")
