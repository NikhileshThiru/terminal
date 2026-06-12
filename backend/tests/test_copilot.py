"""Copilot orchestrator tests with a scripted LLMProvider.

The mock provider speaks the same LLMProvider Protocol the real Gemini and
Groq providers implement, so these tests exercise the orchestrator's logic
end-to-end without touching any network.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.agent.copilot import Copilot, CopilotError
from app.agent.tools import ToolRegistry
from app.data.types import (
    AnalystRating,
    EarningsEvent,
    EarningsSurprise,
    OHLCBar,
    OptionContract,
    Quote,
)
from app.llm.interface import (
    AgentMessage,
    AgentToolCall,
    AgentToolDef,
    AgentTurn,
    LLMMessage,
    LLMResponse,
    LLMUsage,
)
from tests.test_tools import (
    FakeFilingsProvider,
    FakeFinnhub,
    FakeOptionsProvider,
    FakePriceProvider,
)

# === Scripted LLMProvider that satisfies the Protocol ===


class ScriptedLLM:
    """A fake LLMProvider whose step_agent() pops pre-scripted AgentTurns.

    Each item in `turns` is a dict with:
      {"tool_calls": [(name, args), ...]}  → emits those calls
      {"text": "..."}                       → emits final text
    """

    name = "scripted"

    def __init__(
        self,
        turns: list[dict[str, Any]],
        *,
        fallback_draft: dict[str, Any] | None = None,
    ) -> None:
        self.turns = list(turns)
        self.call_count = 0
        self._fallback_draft = fallback_draft

    async def step_agent(
        self,
        conversation: list[AgentMessage],
        tools: list[AgentToolDef],
        model: str,
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> AgentTurn:
        if not self.turns:
            raise RuntimeError("ScriptedLLM ran out of turns")
        spec = self.turns.pop(0)
        self.call_count += 1
        tool_calls = [
            AgentToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=name, arguments=args)
            for (name, args) in spec.get("tool_calls", [])
        ]
        return AgentTurn(
            text=spec.get("text"),
            tool_calls=tool_calls,
            finish_reason=spec.get("finish_reason", "stop"),
            usage=LLMUsage(input_tokens=10, output_tokens=20),
        )

    async def complete(self, messages: list[LLMMessage], model: str, **_: Any) -> LLMResponse:
        raise NotImplementedError("not used in copilot tests")

    async def complete_structured(
        self, messages: list[LLMMessage], model: str, schema: Any, **_: Any
    ) -> Any:
        if self._fallback_draft is None:
            raise RuntimeError("ScriptedLLM has no fallback_draft configured")
        return schema.model_validate(self._fallback_draft)


# === Fixtures ===


_NEAR_EXPIRY = date.today() + timedelta(days=7)
_NEAR_OCC = _NEAR_EXPIRY.strftime("AAPL%y%m%dC00315000")


def _make_registry() -> ToolRegistry:
    return ToolRegistry(
        price_provider=FakePriceProvider(
            quote=Quote(symbol="AAPL", last=Decimal("312.06"), timestamp=datetime.now(UTC)),
            bars=[
                OHLCBar(
                    symbol="AAPL",
                    timestamp=datetime(2026, 5, 20, tzinfo=UTC),
                    open=Decimal("298.18"),
                    high=Decimal("302.8"),
                    low=Decimal("298.08"),
                    close=Decimal("302.25"),
                    volume=38392499,
                )
            ],
        ),
        options_provider=FakeOptionsProvider(
            expirations=[_NEAR_EXPIRY],
            chain=[
                OptionContract(
                    symbol="AAPL",
                    occ_symbol=_NEAR_OCC,
                    expiration=_NEAR_EXPIRY,
                    strike=Decimal("315"),
                    option_type="call",
                    bid=Decimal("4.50"),
                    ask=Decimal("4.70"),
                )
            ],
        ),
        filings_provider=FakeFilingsProvider(),
        finnhub=FakeFinnhub(
            ratings=[
                AnalystRating(
                    symbol="AAPL",
                    period=date(2026, 5, 1),
                    strong_buy=12,
                    buy=24,
                    hold=8,
                    sell=1,
                    strong_sell=0,
                )
            ],
            calendar=[
                EarningsEvent(
                    symbol="AAPL",
                    event_date=date(2026, 7, 25),
                    eps_estimate=1.5,
                    hour="amc",
                )
            ],
            surprises=[
                EarningsSurprise(
                    symbol="AAPL",
                    period=date(2026, 3, 31),
                    eps_actual=1.65,
                    eps_estimate=1.5,
                    surprise=0.15,
                    surprise_pct=10.0,
                )
            ],
        ),
    )


def _good_final_args() -> dict[str, Any]:
    # Reasoning numbers all trace to get_quote + get_options_chain so every
    # test script that fetches these two tools will ground successfully.
    return {
        "symbol": "AAPL",
        "direction": "long",
        "confidence": 0.65,
        "reasoning": (
            "AAPL is trading at $312.06. The 315 strike call has bid 4.50 and "
            "ask 4.70 — a bullish play with premium near 4.60."
        ),
        "prediction_window_days": 14,
        "suggested_contract": {
            "underlying": "AAPL",
            "occ_symbol": _NEAR_OCC,
            "option_type": "call",
            "strike": "315",
            "expiration": _NEAR_EXPIRY.isoformat(),
            "estimated_premium_per_contract": "4.60",
            "contracts": 1,
            "max_risk_usd": "460",
            "exit_if_underlying_below": "305",
            "close_n_days_before_expiry": 2,
        },
        "what_must_happen": (f"AAPL closes above $315 by {_NEAR_EXPIRY.isoformat()}."),
    }


def _copilot(scripted: ScriptedLLM) -> Copilot:
    return Copilot(
        llm=scripted,
        registry=_make_registry(),
        model="llama-3.3-70b-versatile",
        max_iterations=10,
    )


# === Happy path ===


@pytest.mark.asyncio
async def test_happy_path_two_tools_then_final() -> None:
    scripted = ScriptedLLM(
        [
            {"tool_calls": [("get_quote", {"symbol": "AAPL"})]},
            {"tool_calls": [("get_analyst_ratings", {"symbol": "AAPL"})]},
            {"tool_calls": [("get_options_chain", {"symbol": "AAPL"})]},
            {"tool_calls": [("emit_final_thesis", _good_final_args())]},
        ]
    )
    cop = _copilot(scripted)
    run = await cop.generate("AAPL looks strong, $500 to risk", risk_budget_usd=500)
    assert run.thesis.symbol == "AAPL"
    assert run.thesis.direction == "long"
    assert run.thesis.suggested_contract.occ_symbol == _NEAR_OCC
    assert run.thesis.grounding_check_passed is True
    assert run.thesis.llm_provider == "scripted"


# === Strictness: must call data tools before emit_final_thesis ===


@pytest.mark.asyncio
async def test_emit_final_without_tools_is_rejected_then_retries() -> None:
    """The GOOG bug. Model tries emit_final_thesis on turn 1 → rejected;
    model is expected to call data tools on subsequent turns."""
    scripted = ScriptedLLM(
        [
            # Turn 1: model immediately tries emit_final_thesis. Rejected.
            {"tool_calls": [("emit_final_thesis", _good_final_args())]},
            # Turns 2-4: model gathers data correctly.
            {"tool_calls": [("get_quote", {"symbol": "AAPL"})]},
            {"tool_calls": [("get_options_chain", {"symbol": "AAPL"})]},
            # Turn 5: retry emit_final_thesis with data this time.
            {"tool_calls": [("emit_final_thesis", _good_final_args())]},
        ]
    )
    cop = _copilot(scripted)
    run = await cop.generate("AAPL strong", risk_budget_usd=500)
    assert run.thesis.symbol == "AAPL"
    assert run.iterations_used >= 4


# === Strictness: contract must be in fetched chain ===


@pytest.mark.asyncio
async def test_emit_final_with_unfetched_contract_is_rejected() -> None:
    """If the model proposes a contract not in any chain we fetched, reject and retry."""
    bad_args = _good_final_args()
    bad_args["suggested_contract"]["occ_symbol"] = "AAPL999999C99999999"

    scripted = ScriptedLLM(
        [
            {"tool_calls": [("get_quote", {"symbol": "AAPL"})]},
            {"tool_calls": [("get_options_chain", {"symbol": "AAPL"})]},
            # First emit_final_thesis: wrong OCC → rejected.
            {"tool_calls": [("emit_final_thesis", bad_args)]},
            # Retry with the right OCC.
            {"tool_calls": [("emit_final_thesis", _good_final_args())]},
        ]
    )
    cop = _copilot(scripted)
    run = await cop.generate("AAPL setup", risk_budget_usd=500)
    assert run.thesis.suggested_contract.occ_symbol == _NEAR_OCC


# === Strictness: max_risk_usd cap ===


@pytest.mark.asyncio
async def test_emit_final_over_budget_is_rejected() -> None:
    over_budget = _good_final_args()
    over_budget["suggested_contract"]["contracts"] = 10
    over_budget["suggested_contract"]["max_risk_usd"] = "4600"  # exceeds $500 cap

    scripted = ScriptedLLM(
        [
            {"tool_calls": [("get_quote", {"symbol": "AAPL"})]},
            {"tool_calls": [("get_options_chain", {"symbol": "AAPL"})]},
            {"tool_calls": [("emit_final_thesis", over_budget)]},
            {"tool_calls": [("emit_final_thesis", _good_final_args())]},
        ]
    )
    cop = _copilot(scripted)
    run = await cop.generate("AAPL", risk_budget_usd=500)
    assert run.thesis.suggested_contract.max_risk_usd == Decimal("460")


# === Schema-level validation flows back to the model ===


@pytest.mark.asyncio
async def test_invalid_confidence_is_rejected_then_corrected() -> None:
    bad_args = _good_final_args()
    bad_args["confidence"] = 2.5  # out of range

    scripted = ScriptedLLM(
        [
            {"tool_calls": [("get_quote", {"symbol": "AAPL"})]},
            {"tool_calls": [("get_options_chain", {"symbol": "AAPL"})]},
            {"tool_calls": [("emit_final_thesis", bad_args)]},
            {"tool_calls": [("emit_final_thesis", _good_final_args())]},
        ]
    )
    cop = _copilot(scripted)
    run = await cop.generate("AAPL", risk_budget_usd=500)
    assert run.thesis.confidence == 0.65


# === Max iterations ===


@pytest.mark.asyncio
async def test_max_iterations_raises() -> None:
    looping = [{"tool_calls": [("get_quote", {"symbol": "AAPL"})]}] * 20
    scripted = ScriptedLLM(looping)
    cop = Copilot(
        llm=scripted,
        registry=_make_registry(),
        model="llama-3.3-70b-versatile",
        max_iterations=3,
    )
    with pytest.raises(CopilotError) as exc:
        await cop.generate("AAPL", risk_budget_usd=500)
    assert "iterations" in str(exc.value).lower()


# === Grounding failure: retry once, then reject ===


@pytest.mark.asyncio
async def test_grounding_failure_triggers_retry() -> None:
    """First emit has hallucinated numbers; orchestrator surfaces them, model retries with valid."""
    hallucinated = _good_final_args()
    hallucinated["reasoning"] = (
        "AAPL at $999.99 surged with 88% earnings surprise — completely made up."
    )

    scripted = ScriptedLLM(
        [
            {"tool_calls": [("get_quote", {"symbol": "AAPL"})]},
            {"tool_calls": [("get_options_chain", {"symbol": "AAPL"})]},
            # First emit_final_thesis: passes preconditions but grounding will fail.
            {"tool_calls": [("emit_final_thesis", hallucinated)]},
            # Retry chunk:
            {"tool_calls": [("get_analyst_ratings", {"symbol": "AAPL"})]},
            {"tool_calls": [("emit_final_thesis", _good_final_args())]},
        ]
    )
    cop = _copilot(scripted)
    run = await cop.generate("AAPL", risk_budget_usd=500)
    assert run.thesis.grounding_check_passed is True
    assert "999.99" not in run.thesis.reasoning


@pytest.mark.asyncio
async def test_grounding_failure_after_retries_raises() -> None:
    hallucinated = _good_final_args()
    hallucinated["reasoning"] = (
        "AAPL at $999.99 surged with 88% earnings surprise — completely made up."
    )

    scripted = ScriptedLLM(
        [
            {"tool_calls": [("get_quote", {"symbol": "AAPL"})]},
            {"tool_calls": [("get_options_chain", {"symbol": "AAPL"})]},
            {"tool_calls": [("emit_final_thesis", hallucinated)]},
            # Second chance also hallucinates:
            {"tool_calls": [("emit_final_thesis", hallucinated)]},
        ]
    )
    cop = _copilot(scripted)
    with pytest.raises(CopilotError) as exc:
        await cop.generate("AAPL", risk_budget_usd=500)
    assert "grounding" in str(exc.value).lower()


# === No-tool-call fallback ===


@pytest.mark.asyncio
async def test_text_only_response_uses_structured_fallback() -> None:
    scripted = ScriptedLLM(
        [{"text": "I think AAPL is fine."}],
        fallback_draft=_good_final_args(),
    )
    cop = _copilot(scripted)
    # Tool count is 0 from the fallback path — grounding will fail.
    with pytest.raises(CopilotError):
        await cop.generate("AAPL", risk_budget_usd=500)
