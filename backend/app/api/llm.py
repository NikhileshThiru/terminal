"""/llm — telemetry endpoints for the LLM layer.

- `/llm/cost-summary` feeds the header's requests-vs-budget chip. Counts come
  from the durable `llm_calls` table for the current Pacific-time day (the
  boundary Gemini's free quota resets on), so a restart doesn't zero them.
  Falls back to the in-process CostTracker if the DB is unreachable.
- `/llm/health` shows each step's ordered routes with live cooldown state,
  plus today's per-route usage — the "is the AI actually up?" panel.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.logging import get_logger
from app.llm.cost import compute_cost_usd, get_cost_tracker

router = APIRouter(prefix="/llm", tags=["llm"])
_log = get_logger(__name__)


class CostBreakdownOut(BaseModel):
    provider: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class CostSummaryOut(BaseModel):
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    since: datetime
    by_model: list[CostBreakdownOut]
    # Free-tier framing: what's scarce is requests/day, not dollars.
    daily_request_budget: int
    source: str  # "db" (today, Pacific) | "memory" (since process start)


async def _usage_today() -> tuple[datetime, list[dict[str, Any]]] | None:
    from app.eval.persistence import get_session_factory
    from app.llm.usage_log import pacific_day_start, usage_since

    since = pacific_day_start()
    try:
        async with get_session_factory()() as session:
            return since, await usage_since(session, since)
    except Exception as e:
        _log.warning("llm_usage_query_failed", error=str(e)[:200])
        return None


@router.get("/cost-summary", response_model=CostSummaryOut)
async def cost_summary() -> CostSummaryOut:
    budget = get_settings().llm_daily_request_budget
    today = await _usage_today()
    if today is not None:
        since, rows = today
        merged: dict[tuple[str, str], CostBreakdownOut] = {}
        for r in rows:
            key = (r["provider"], r["model"])
            b = merged.get(key) or CostBreakdownOut(
                provider=r["provider"],
                model=r["model"],
                calls=0,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
            )
            b.calls += r["calls"]
            b.input_tokens += r["input_tokens"]
            b.output_tokens += r["output_tokens"]
            b.cost_usd = compute_cost_usd(b.model, b.input_tokens, b.output_tokens)
            merged[key] = b
        by_model = sorted(merged.values(), key=lambda b: b.calls, reverse=True)
        return CostSummaryOut(
            calls=sum(b.calls for b in by_model),
            input_tokens=sum(b.input_tokens for b in by_model),
            output_tokens=sum(b.output_tokens for b in by_model),
            cost_usd=sum(b.cost_usd for b in by_model),
            since=since,
            by_model=by_model,
            daily_request_budget=budget,
            source="db",
        )

    s = get_cost_tracker().summary()
    return CostSummaryOut(
        calls=s.calls,
        input_tokens=s.input_tokens,
        output_tokens=s.output_tokens,
        cost_usd=s.cost_usd,
        since=s.since,
        by_model=[CostBreakdownOut(**b) for b in s.by_model],
        daily_request_budget=budget,
        source="memory",
    )


class RouteHealthOut(BaseModel):
    position: int
    provider: str
    model: str
    state: str  # ok | cooling
    cooldown_until: datetime | None
    calls: int
    successes: int
    failures: int
    rate_limited: int
    last_error: str | None
    last_success_at: datetime | None
    last_failure_at: datetime | None


class StepHealthOut(BaseModel):
    step: str
    routes: list[RouteHealthOut]
    # ok = primary serving; degraded = primary cooling, a fallback is up;
    # down = every route cooling; unconfigured = no usable route (no keys).
    status: str


class UsageRowOut(BaseModel):
    step: str
    provider: str
    model: str
    calls: int
    ok: int
    rate_limited: int
    input_tokens: int
    output_tokens: int
    avg_latency_ms: float | None


class LLMHealthOut(BaseModel):
    steps: list[StepHealthOut]
    usage_today: list[UsageRowOut]
    usage_since: datetime | None


def _step_status(routes: list[RouteHealthOut]) -> str:
    if not routes:
        return "unconfigured"
    if routes[0].state == "ok":
        return "ok"
    if any(r.state == "ok" for r in routes[1:]):
        return "degraded"
    return "down"


@router.get("/health", response_model=LLMHealthOut)
async def llm_health() -> LLMHealthOut:
    from app.data.types import ProviderUnavailable
    from app.llm.factory import get_router

    steps: list[StepHealthOut] = []
    for step in ("triage", "thesis"):
        try:
            routes = [RouteHealthOut.model_validate(r) for r in get_router(step).describe()]
        except ProviderUnavailable:
            routes = []
        steps.append(StepHealthOut(step=step, routes=routes, status=_step_status(routes)))

    today = await _usage_today()
    return LLMHealthOut(
        steps=steps,
        usage_today=[UsageRowOut(**r) for r in today[1]] if today else [],
        usage_since=today[0] if today else None,
    )
