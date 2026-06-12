"""Triage gate — one cheap LLM call per discovery event (DESIGN.md §4 step 3).

The most-material filter in the pipeline. Most of the ~130 daily events drop
here so the expensive thesis step only runs on signal. Uses the config-driven
LLM_TRIAGE_PROVIDER/MODEL (defaults to Gemini Flash Lite — cheap, fast).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.discovery.types import DiscoveryEvent
from app.llm.interface import LLMMessage, LLMProvider

_log = get_logger(__name__)


class TriageDecision(BaseModel):
    """The triage gate's verdict."""

    passed: bool = Field(description="True if the event is worth a full thesis.")
    reason: str = Field(min_length=3, max_length=300)
    confidence: float = Field(ge=0.0, le=1.0)


_SYSTEM_PROMPT = """You are the triage gate for an AI trading-research terminal.

Your job: decide if a single news/filing event is MATERIAL enough to warrant a
full options-thesis workup. Most events should be dropped — be selective.

MATERIAL examples (pass):
- earnings surprise (actual vs estimate) or forward-guidance change
- M&A announcement, joint venture, major partnership
- FDA approval/rejection, regulatory action, lawsuit verdict
- product launch / customer win with quantified revenue impact
- significant insider activity (CEO/CFO Form 4 with notable size)
- restated financials, going-concern warnings
- analyst upgrade/downgrade with substantive rationale and a new price target

NOT material (drop):
- routine periodic filings (vanilla 10-K/10-Q with no surprise content)
- 13F snapshots (institutional ownership reports — backward-looking)
- minor staffing changes, board re-elections, generic press releases
- proxy materials, share-repurchase reauthorizations without size changes
- index reconstitutions, ratings agency reaffirmations

Return a calibrated confidence — not your enthusiasm. 0.5 = genuinely uncertain.
"""


def _format_event(event: DiscoveryEvent) -> str:
    body = (event.body or "").strip()
    body_excerpt = body[:500] + ("…" if len(body) > 500 else "")
    syms = ", ".join(event.symbols) if event.symbols else "(none)"
    parts = [
        f"Source: {event.source} ({event.kind})",
        f"Symbol(s): {syms}",
        f"Headline: {event.headline}",
    ]
    if body_excerpt:
        parts.append(f"Body excerpt: {body_excerpt}")
    if event.url:
        parts.append(f"URL: {event.url}")
    parts.append(f"Published: {event.published_at.isoformat()}")
    return "\n".join(parts)


async def triage(
    event: DiscoveryEvent,
    *,
    llm: LLMProvider,
    model: str,
) -> TriageDecision:
    """Run one triage call. Caller handles ProviderUnavailable."""
    user_content = _format_event(event)
    messages = [
        LLMMessage(role="system", content=_SYSTEM_PROMPT),
        LLMMessage(role="user", content=user_content),
    ]
    decision = await llm.complete_structured(
        messages=messages,
        model=model,
        schema=TriageDecision,
        max_tokens=400,
        temperature=0.0,
    )
    _log.info(
        "triage_decision",
        event_id=event.id,
        symbol=(event.symbols[0] if event.symbols else None),
        passed=decision.passed,
        confidence=decision.confidence,
        reason=decision.reason[:120],
    )
    return decision
