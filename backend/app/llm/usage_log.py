"""Durable per-call LLM usage log (`llm_calls` table).

One row per provider attempt the router makes — successes AND failures —
with step, latency, tokens, and the failure reason. Three jobs:

1. The header's requests-vs-budget chip counts *today's* calls from the DB,
   so a backend restart no longer zeroes it (the in-memory CostTracker did).
2. Quota forensics: "when did we hit the Gemini daily cap, and on which
   step?" is one GROUP BY instead of grepping container logs.
3. Latency budget evidence (CLAUDE.md §4) per provider/model.

Writes are fire-and-forget: a failed insert is logged and dropped, never
allowed to fail the LLM call it describes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Boolean, DateTime, Index, Integer, String, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from app.core.logging import get_logger
from app.eval.models import Base

_log = get_logger(__name__)

# Free-tier "days" are Pacific-time days for Gemini; using the same boundary
# for every provider keeps the daily chip honest against the binding quota.
_PACIFIC = ZoneInfo("America/Los_Angeles")


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    called_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    step: Mapped[str] = mapped_column(String(16), index=True)  # triage | thesis
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    method: Mapped[str] = mapped_column(String(24))  # complete | complete_structured | step_agent
    ok: Mapped[bool] = mapped_column(Boolean)
    error_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    correlation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (Index("idx_llm_calls_provider_model_at", "provider", "model", "called_at"),)


@dataclass(frozen=True)
class UsageRecord:
    step: str
    provider: str
    model: str
    method: str
    ok: bool
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    error_reason: str | None = None
    correlation_id: str | None = None
    called_at: datetime | None = None


class DbUsageSink:
    """Callable sink the router hands each UsageRecord to."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory
        self._tasks: set[asyncio.Task[None]] = set()

    def __call__(self, record: UsageRecord) -> None:
        try:
            task = asyncio.get_running_loop().create_task(self._write(record))
        except RuntimeError:  # pragma: no cover — no loop (sync caller)
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write(self, record: UsageRecord) -> None:
        try:
            async with self._factory() as session:
                session.add(
                    LLMCall(
                        called_at=record.called_at or datetime.now(UTC),
                        step=record.step,
                        provider=record.provider,
                        model=record.model,
                        method=record.method,
                        ok=record.ok,
                        error_reason=record.error_reason,
                        latency_ms=record.latency_ms,
                        input_tokens=record.input_tokens,
                        output_tokens=record.output_tokens,
                        correlation_id=record.correlation_id,
                    )
                )
                await session.commit()
        except Exception as e:
            _log.warning("llm_usage_log_write_failed", error=str(e)[:200])

    async def drain(self) -> None:
        """Await in-flight writes (tests + graceful shutdown)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


def pacific_day_start(now: datetime | None = None) -> datetime:
    """Start of the current Pacific-time day, as an aware UTC datetime."""
    local = (now or datetime.now(UTC)).astimezone(_PACIFIC)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC)


async def usage_since(session: AsyncSession, since: datetime) -> list[dict[str, Any]]:
    """Per (step, provider, model) aggregates since `since`."""
    stmt = (
        select(
            LLMCall.step,
            LLMCall.provider,
            LLMCall.model,
            func.count(LLMCall.id),
            func.sum(case((LLMCall.ok.is_(True), 1), else_=0)),
            func.sum(case((LLMCall.error_reason == "rate_limited", 1), else_=0)),
            func.coalesce(func.sum(LLMCall.input_tokens), 0),
            func.coalesce(func.sum(LLMCall.output_tokens), 0),
            func.avg(LLMCall.latency_ms),
        )
        .where(LLMCall.called_at >= since)
        .group_by(LLMCall.step, LLMCall.provider, LLMCall.model)
        .order_by(LLMCall.step, func.count(LLMCall.id).desc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "step": r[0],
            "provider": r[1],
            "model": r[2],
            "calls": int(r[3] or 0),
            "ok": int(r[4] or 0),
            "rate_limited": int(r[5] or 0),
            "input_tokens": int(r[6] or 0),
            "output_tokens": int(r[7] or 0),
            "avg_latency_ms": round(float(r[8]), 1) if r[8] is not None else None,
        }
        for r in rows
    ]


async def hourly_calls(session: AsyncSession, hours: int = 24) -> list[dict[str, Any]]:
    """Calls per hour over the last `hours` (for a sparkline). Bucketed in Python
    so the query stays portable across SQLite (tests) and Postgres."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    stmt = select(LLMCall.called_at, LLMCall.ok).where(LLMCall.called_at >= since)
    buckets: dict[datetime, list[int]] = {}
    for called_at, ok in (await session.execute(stmt)).all():
        ts = called_at if called_at.tzinfo else called_at.replace(tzinfo=UTC)
        hour = ts.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        b = buckets.setdefault(hour, [0, 0])
        b[0] += 1
        b[1] += 0 if ok else 1
    return [{"hour": h, "calls": v[0], "failures": v[1]} for h, v in sorted(buckets.items())]
