"""Triage gate tests with a stubbed LLMProvider."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.discovery.triage import TriageDecision, triage
from app.discovery.types import DiscoveryEvent
from app.llm.interface import LLMMessage, LLMProvider


class _ScriptedLLM:
    name = "scripted"

    def __init__(self, decision: TriageDecision) -> None:
        self._decision = decision
        self.last_messages: list[LLMMessage] = []
        self.last_model: str | None = None

    async def complete(self, **_: Any) -> Any:
        raise NotImplementedError

    async def complete_structured(self, messages, model, schema, **_: Any):
        self.last_messages = list(messages)
        self.last_model = model
        return self._decision

    async def step_agent(self, **_: Any) -> Any:
        raise NotImplementedError


def _ev() -> DiscoveryEvent:
    return DiscoveryEvent(
        id="acc-1",
        source="edgar",
        kind="filing",
        symbols=["AAPL"],
        headline="AAPL 8-K: Q2 earnings beat by 12%",
        body="Quarterly EPS of $2.01 vs $1.79 estimate; revenue $90B vs $87B est.",
        url="https://example.com/8k",
        published_at=datetime.now(UTC),
    )


def test_satisfies_protocol() -> None:
    """The scripted LLM matches the LLMProvider Protocol (for type-safety in callers)."""
    s = _ScriptedLLM(TriageDecision(passed=True, reason="example reason", confidence=0.7))
    assert isinstance(s, LLMProvider)


@pytest.mark.asyncio
async def test_triage_returns_decision() -> None:
    llm = _ScriptedLLM(
        TriageDecision(passed=True, reason="Earnings beat with raised guidance", confidence=0.8)
    )
    d = await triage(_ev(), llm=llm, model="gemini-2.5-flash-lite")
    assert d.passed is True
    assert d.confidence == 0.8
    assert "earnings" in d.reason.lower()


@pytest.mark.asyncio
async def test_triage_passes_event_details_to_llm() -> None:
    llm = _ScriptedLLM(TriageDecision(passed=False, reason="routine", confidence=0.6))
    await triage(_ev(), llm=llm, model="gemini-2.5-flash-lite")
    assert llm.last_model == "gemini-2.5-flash-lite"
    # User content should include symbol and headline.
    user_msg = next(m for m in llm.last_messages if m.role == "user")
    assert "AAPL" in user_msg.content
    assert "Q2 earnings beat" in user_msg.content
    assert "edgar" in user_msg.content


def test_triage_decision_validates_confidence() -> None:
    with pytest.raises(ValueError):
        TriageDecision(passed=True, reason="x", confidence=1.5)
