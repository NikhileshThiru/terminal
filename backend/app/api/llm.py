"""/llm — telemetry endpoints for the LLM layer.

Currently just `/llm/cost-summary`, which feeds the cost pill in the header.
Process-scoped accumulator (resets on restart); see `app/llm/cost.py`.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from app.llm.cost import get_cost_tracker

router = APIRouter(prefix="/llm", tags=["llm"])


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


@router.get("/cost-summary", response_model=CostSummaryOut)
async def cost_summary() -> CostSummaryOut:
    from app.core.config import get_settings

    s = get_cost_tracker().summary()
    return CostSummaryOut(
        calls=s.calls,
        input_tokens=s.input_tokens,
        output_tokens=s.output_tokens,
        cost_usd=s.cost_usd,
        since=s.since,
        by_model=[CostBreakdownOut(**b) for b in s.by_model],
        daily_request_budget=get_settings().llm_daily_request_budget,
    )
