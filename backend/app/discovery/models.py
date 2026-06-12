"""ORM models for the discovery pipeline.

`SeenDiscoveryEvent` is the dedup table — process-local sets lose state on
restart and would silently swallow any filing that arrived during downtime
(an "always-on" pipeline cannot afford that). Storing dedup state in Postgres
means the EdgarPoller's bootstrap is a one-time-only event per source.

`CatalystEvent` is the catalyst calendar (DESIGN.md §8): scheduled events
(earnings; later: Fed/FDA) that the agent pre-positions on. The catalyst
bucket is the one where edge is plausibly measurable — deterministic timing
removes the latency disadvantage that kills the reactive bucket.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.eval.models import Base


class SeenDiscoveryEvent(Base):
    """One row per (source, external_id) ever observed by a discovery source."""

    __tablename__ = "seen_discovery_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_seen_discovery_source_id"),
    )


class CatalystEventType(StrEnum):
    """Kinds of pre-known events the catalyst scheduler tracks."""

    EARNINGS = "earnings"
    # Later additions: FED (FOMC dates), FDA (PDUFA dates), CPI, NFP, ECB.
    # Each gets its own data source; the table shape stays the same.


class CatalystEventState(StrEnum):
    """Lifecycle of one CatalystEvent row."""

    SCHEDULED = "scheduled"  # known but not yet triggered
    TRIGGERED = "triggered"  # thesis fired; thesis_id populated
    EXPIRED = "expired"  # event date passed without firing (e.g. quota out)


class CatalystEvent(Base):
    """One scheduled catalyst (e.g. AAPL earnings on 2026-07-29).

    Upsertable on (symbol, event_type, event_date) so daily refreshes from
    Finnhub are idempotent. State transitions are one-way:
    scheduled → triggered (on success) or scheduled → expired (date passed
    without trigger). The CatalystScheduler picks rows where state =
    SCHEDULED and event_date is inside the lead window.
    """

    __tablename__ = "catalyst_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    event_type: Mapped[str] = mapped_column(String(16), index=True)
    event_date: Mapped[date] = mapped_column(Date, index=True)
    event_hour: Mapped[str | None] = mapped_column(String(8), nullable=True)

    estimated_eps: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    estimated_revenue_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)

    state: Mapped[str] = mapped_column(
        String(16), default=CatalystEventState.SCHEDULED.value, index=True
    )
    thesis_id: Mapped[int | None] = mapped_column(
        ForeignKey("theses.id"), nullable=True, index=True
    )

    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Full raw payload for debugging / replay. JSON for portability.
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("symbol", "event_type", "event_date", name="uq_catalyst_symbol_type_date"),
    )


class TriageDecisionRow(Base):
    """One row per triage call. Persisted so the News pane survives backend
    restarts and so we can re-query by symbol for the TickerInfoPane.

    Idempotent on event_id: re-triaging the same event (e.g. retry after
    transient failure) overwrites the previous decision via upsert. We do
    NOT preserve historical re-evaluations; latest call wins."""

    __tablename__ = "triage_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    headline: Mapped[str] = mapped_column(String(300))
    body_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(16))

    passed: Mapped[bool] = mapped_column(Boolean, index=True)
    reason: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_triage_symbol_decided", "symbol", "decided_at"),
        Index("idx_triage_decided", "decided_at"),
    )
