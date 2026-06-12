"""Tool-calling orchestrator for the manual copilot (DESIGN.md §4).

Provider-agnostic: speaks the generic LLMProvider interface. Works with
Gemini, Groq, or any future provider that satisfies the Protocol.

Flow:
1. Build the conversation (system prompt + user thesis idea).
2. Loop (bounded iterations):
   - Ask the provider for the next agent turn (text or tool calls).
   - For each tool call:
     - If `emit_final_thesis`: validate preconditions; if good, exit loop.
     - Otherwise: execute the data tool; append the tool result.
   - If no tool calls and no final thesis: force structured fallback.
3. Run the grounding check; if it fails, surface the error to the model and
   allow ONE retry before bailing.
4. Wrap the draft into a full `Thesis` with bookkeeping.

Strictness baked in (post-GOOG-hallucination):
- Reject emit_final_thesis if zero data tools have been called.
- Reject if suggested_contract.occ_symbol isn't in any fetched chain.
- Reject if max_risk_usd > risk_budget_usd.
- One auto-retry on grounding failure.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import ValidationError

from app.agent.grounding import GroundingResult, check_grounding
from app.agent.schemas import SourceBucket, Thesis, ThesisDraft
from app.agent.tools import ToolRegistry, ToolResult
from app.agent.tools import _strip_pydantic_schema_keys as _strip_schema
from app.core.correlation import get_correlation_id, new_correlation_id
from app.core.logging import get_logger
from app.llm.interface import (
    AgentMessage,
    AgentToolDef,
    AgentToolResult,
    LLMProvider,
)

_log = get_logger(__name__)

_FINAL_TOOL_NAME = "emit_final_thesis"
_MIN_DATA_TOOL_CALLS_BEFORE_FINAL = 2


def _inline_refs(schema: dict[str, Any], defs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Walk a JSON schema and inline $ref → $defs lookups."""
    if defs is None:
        defs = schema.get("$defs", {})
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref.startswith("#/$defs/"):
            name = ref.split("/")[-1]
            return _inline_refs(defs.get(name, {}), defs)
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k == "$defs":
            continue
        if isinstance(v, dict):
            out[k] = _inline_refs(v, defs)
        elif isinstance(v, list):
            out[k] = [_inline_refs(item, defs) if isinstance(item, dict) else item for item in v]
        else:
            out[k] = v
    return out


def _final_thesis_tool_def() -> AgentToolDef:
    raw = ThesisDraft.model_json_schema()
    inlined = _inline_refs(raw)
    cleaned = _strip_schema(inlined)
    return AgentToolDef(
        name=_FINAL_TOOL_NAME,
        description=(
            "Commit your final thesis. Call this exactly once, AFTER gathering data via "
            "the other tools. Every number in your reasoning must come from a real tool "
            "result. The contract you propose MUST be one that appears in an option "
            "chain you fetched."
        ),
        parameters=cleaned,
    )


SYSTEM_PROMPT_TEMPLATE = """You are a trading research assistant for a paper-only research terminal.

WORKFLOW (mandatory):
1. Call AT LEAST {min_tool_calls} data tools first. ALWAYS start with `get_quote` to
   anchor the current price.
2. If the user's thesis involves earnings, call `get_earnings_context`. If it
   involves options pricing, call `get_options_chain` BEFORE picking a contract.
3. Only after gathering data, call `emit_final_thesis` exactly once with a
   structured answer.

HARD RULES — violating these makes me reject your thesis:
1. Every number in your `reasoning` MUST come from a tool result. Do NOT invent figures.
2. The `suggested_contract` MUST be a real contract from a `get_options_chain` you fetched.
   Use the exact OCC symbol from that chain. Strike, expiration, and premium MUST match.
3. The contract's `expiration` MUST be in the future (today is {today}).
4. `max_risk_usd` = estimated_premium_per_contract x contracts x 100, EXACTLY.
5. `max_risk_usd` MUST NOT exceed the user's risk budget: {risk_budget_text}
6. `confidence` is calibrated, not optimistic. 0.5 means coin-flip.
7. `prediction_window_days` aligns with the catalyst's time horizon.
8. `what_must_happen` is specific and falsifiable (e.g. "AAPL closes above $315 by 2026-07-25").
9. Provide `exit_if_underlying_below` (for calls) or `exit_if_underlying_above` (for puts)
   as your stop-loss trigger, and `close_n_days_before_expiry` (typically 5-7) for theta exit.
"""


def _build_system_prompt(risk_budget_usd: float | None) -> str:
    if risk_budget_usd is None:
        risk_text = "no explicit limit; keep max_risk_usd around $500 by default."
    else:
        risk_text = f"${risk_budget_usd:.2f} (HARD CAP — do not exceed)."
    today = datetime.now(UTC).date().isoformat()
    return SYSTEM_PROMPT_TEMPLATE.format(
        min_tool_calls=_MIN_DATA_TOOL_CALLS_BEFORE_FINAL,
        today=today,
        risk_budget_text=risk_text,
    )


@dataclass
class CopilotRun:
    thesis: Thesis
    tool_results: list[ToolResult]
    grounding: GroundingResult
    iterations_used: int


CopilotEventKind = Literal[
    "started",
    "thinking",
    "tool_call",
    "tool_result",
    "thesis_validating",
    "thesis_rejected",
    "thesis_accepted",
    "grounding_check",
    "grounding_retry",
    "fallback_forced",
    "done",
    "error",
]


@dataclass(frozen=True)
class CopilotEvent:
    """One observable moment in the agent loop. Surfaced over SSE so the
    frontend can render the tool-calling stream live, and so server-side
    consumers (tests, Langfuse, logs) can attach without coupling to the
    orchestrator internals."""

    kind: CopilotEventKind
    payload: dict[str, Any] = field(default_factory=dict)


EventSink = Callable[[CopilotEvent], Awaitable[None]]


async def _emit(sink: EventSink | None, kind: CopilotEventKind, payload: dict[str, Any]) -> None:
    """Emit one event. Always fans out to the global broadcaster so anyone
    listening on /agent/events/stream sees every run (manual, reactive,
    catalyst); optionally also calls a per-run sink for the request that
    initiated the run."""
    event = CopilotEvent(kind=kind, payload=payload)
    # Global fan-out — local import to avoid a circular dependency between
    # copilot.py and broadcaster.py (which imports CopilotEvent from here).
    try:
        from app.agent.broadcaster import broadcast_event

        await broadcast_event(event)
    except Exception:
        _log.exception("copilot_event_broadcast_failed", kind=kind)
    if sink is None:
        return
    try:
        await sink(event)
    except Exception:
        # A per-run sink should never abort the run. Log; carry on.
        _log.exception("copilot_event_sink_raised", kind=kind)


class CopilotError(Exception):
    """Raised when the orchestrator cannot produce a valid thesis."""


class Copilot:
    def __init__(
        self,
        *,
        llm: LLMProvider,
        registry: ToolRegistry,
        model: str,
        max_iterations: int = 10,
        temperature: float = 0.2,
        max_grounding_retries: int = 1,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.model = model
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_grounding_retries = max_grounding_retries

    async def generate(
        self,
        user_thesis: str,
        *,
        risk_budget_usd: float | None = None,
        source_bucket: SourceBucket = "manual",
        event_sink: EventSink | None = None,
    ) -> CopilotRun:
        correlation_id = get_correlation_id() or new_correlation_id()
        started = time.monotonic()
        tool_results: list[ToolResult] = []
        data_tool_call_count = 0
        chain_fetches: dict[str, set[str]] = {}  # underlying → set of OCC symbols fetched

        all_tools: list[AgentToolDef] = self.registry.agent_defs()
        all_tools.append(_final_thesis_tool_def())

        system_prompt = _build_system_prompt(risk_budget_usd)
        conversation: list[AgentMessage] = [
            AgentMessage(role="system", content=system_prompt),
            AgentMessage(role="user", content=user_thesis.strip()),
        ]

        await _emit(
            event_sink,
            "started",
            {
                "user_thesis": user_thesis.strip(),
                "risk_budget_usd": risk_budget_usd,
                "model": self.model,
                "provider": self.llm.name,
                "correlation_id": correlation_id,
            },
        )

        draft: ThesisDraft | None = None
        grounding_retries_used = 0
        iterations = 0

        for iterations in range(1, self.max_iterations + 1):
            try:
                turn = await self.llm.step_agent(
                    conversation=conversation,
                    tools=all_tools,
                    model=self.model,
                    temperature=self.temperature,
                )
            except Exception as e:
                raise CopilotError(f"LLM call failed at iteration {iterations}: {e}") from e

            # Record the assistant's turn in the conversation history.
            conversation.append(
                AgentMessage(
                    role="assistant",
                    content=turn.text,
                    tool_calls=turn.tool_calls,
                )
            )

            if turn.text and turn.text.strip():
                await _emit(
                    event_sink, "thinking", {"text": turn.text.strip(), "iteration": iterations}
                )

            if not turn.tool_calls:
                # Model emitted text but no tool calls. Force structured fallback.
                _log.warning(
                    "copilot_no_tool_calls_emitted",
                    correlation_id=correlation_id,
                    iteration=iterations,
                )
                await _emit(event_sink, "fallback_forced", {"iteration": iterations})
                draft = await self._force_final_thesis(conversation)
                break

            committed_draft: ThesisDraft | None = None
            for tc in turn.tool_calls:
                if tc.name == _FINAL_TOOL_NAME:
                    await _emit(
                        event_sink,
                        "thesis_validating",
                        {"args_keys": sorted(tc.arguments.keys())},
                    )
                    response = self._validate_final_thesis(
                        tc.arguments,
                        data_tool_call_count=data_tool_call_count,
                        chain_fetches=chain_fetches,
                        risk_budget_usd=risk_budget_usd,
                    )
                    if response.ok:
                        committed_draft = response.draft
                        await _emit(event_sink, "thesis_accepted", {})
                        conversation.append(
                            AgentMessage(
                                role="tool",
                                tool_result=AgentToolResult(
                                    tool_call_id=tc.id,
                                    name=tc.name,
                                    content={"ok": True, "accepted": True},
                                ),
                            )
                        )
                        break
                    await _emit(event_sink, "thesis_rejected", {"error": response.error})
                    conversation.append(
                        AgentMessage(
                            role="tool",
                            tool_result=AgentToolResult(
                                tool_call_id=tc.id,
                                name=tc.name,
                                content={"ok": False, "error": response.error},
                            ),
                        )
                    )
                    continue

                # Regular data tool — execute it.
                await _emit(event_sink, "tool_call", {"name": tc.name, "arguments": tc.arguments})
                tr = await self.registry.execute(tc.name, tc.arguments)
                tool_results.append(tr)
                if tr.success:
                    data_tool_call_count += 1
                    if tc.name == "get_options_chain":
                        _record_chain_fetch(tr, chain_fetches)
                await _emit(
                    event_sink,
                    "tool_result",
                    {
                        "name": tc.name,
                        "success": tr.success,
                        "summary": _summarise_tool_result(tr),
                        "error": tr.error if not tr.success else None,
                    },
                )
                conversation.append(
                    AgentMessage(
                        role="tool",
                        tool_result=AgentToolResult(
                            tool_call_id=tc.id,
                            name=tc.name,
                            content=_tool_result_payload(tr),
                        ),
                    )
                )

            if committed_draft is not None:
                draft = committed_draft
                break

        if draft is None:
            raise CopilotError(
                f"Copilot exhausted {self.max_iterations} iterations without a final thesis. "
                f"Tools executed: {[t.tool_name for t in tool_results]}"
            )

        grounding = check_grounding(draft.reasoning, tool_results)
        await _emit(
            event_sink,
            "grounding_check",
            {"passed": grounding.passed, "unverified": grounding.unverified_numbers[:8]},
        )
        # If grounding fails, try once more with feedback to the model.
        while not grounding.passed and grounding_retries_used < self.max_grounding_retries:
            grounding_retries_used += 1
            _log.warning(
                "grounding_failed_retrying",
                correlation_id=correlation_id,
                unverified=grounding.unverified_numbers[:5],
            )
            await _emit(
                event_sink,
                "grounding_retry",
                {"attempt": grounding_retries_used},
            )
            conversation.append(
                AgentMessage(
                    role="user",
                    content=(
                        "Your thesis was rejected because these numbers in your reasoning "
                        f"do not appear in any tool result: {grounding.unverified_numbers[:8]}. "
                        "Re-call data tools to verify those numbers (or remove them), then "
                        "call emit_final_thesis again."
                    ),
                )
            )
            # One more bounded loop.
            retry_draft = await self._second_chance(
                conversation,
                all_tools,
                tool_results,
                chain_fetches,
                risk_budget_usd,
                data_tool_call_count,
                event_sink=event_sink,
            )
            if retry_draft is not None:
                draft = retry_draft
                grounding = check_grounding(draft.reasoning, tool_results)
                await _emit(
                    event_sink,
                    "grounding_check",
                    {"passed": grounding.passed, "unverified": grounding.unverified_numbers[:8]},
                )
                iterations += 1

        elapsed_ms = int((time.monotonic() - started) * 1000)
        thesis = Thesis(
            **draft.model_dump(),
            correlation_id=correlation_id,
            source_bucket=source_bucket,
            generated_at=datetime.now(UTC),
            grounding_check_passed=grounding.passed,
            grounding_notes=grounding.notes,
            llm_provider=self.llm.name,
            llm_model=self.model,
            funnel_latency_ms=elapsed_ms,
        )
        if not grounding.passed:
            raise CopilotError(
                f"Grounding failed after {grounding_retries_used} retries. "
                f"Unverified numbers: {grounding.unverified_numbers[:8]}. "
                f"Notes: {grounding.notes}"
            )
        return CopilotRun(
            thesis=thesis,
            tool_results=tool_results,
            grounding=grounding,
            iterations_used=iterations,
        )

    def _validate_final_thesis(
        self,
        args: dict[str, Any],
        *,
        data_tool_call_count: int,
        chain_fetches: dict[str, set[str]],
        risk_budget_usd: float | None,
    ) -> _FinalThesisCheck:
        if data_tool_call_count < _MIN_DATA_TOOL_CALLS_BEFORE_FINAL:
            return _FinalThesisCheck.fail(
                f"You called emit_final_thesis after only {data_tool_call_count} data tools. "
                f"Call at least {_MIN_DATA_TOOL_CALLS_BEFORE_FINAL} (start with get_quote and "
                "get_options_chain) before committing a thesis."
            )

        try:
            draft = ThesisDraft.model_validate(args)
        except ValidationError as e:
            return _FinalThesisCheck.fail(
                f"Schema validation failed: {e.errors()[:3]}. "
                "Common issues: past-dated expiration, max_risk_usd not equal to "
                "premium x contracts x 100, confidence outside [0,1]."
            )

        contract = draft.suggested_contract
        underlying_chains = chain_fetches.get(contract.underlying.upper(), set())
        if contract.occ_symbol not in underlying_chains:
            available_sample = sorted(underlying_chains)[:5]
            return _FinalThesisCheck.fail(
                f"OCC symbol {contract.occ_symbol} is not in any options chain you fetched "
                f"for {contract.underlying}. Either pick a contract from a chain you fetched, "
                f"or call get_options_chain first. "
                f"Examples from your fetched chain: {available_sample}"
            )

        if risk_budget_usd is not None and float(contract.max_risk_usd) > risk_budget_usd:
            return _FinalThesisCheck.fail(
                f"max_risk_usd ${contract.max_risk_usd} exceeds the user's budget "
                f"${risk_budget_usd:.2f}. Reduce `contracts` or pick a cheaper strike."
            )

        return _FinalThesisCheck.ok_with(draft)

    async def _force_final_thesis(self, conversation: list[AgentMessage]) -> ThesisDraft:
        """If the model emits text without a tool call, ask for a JSON thesis directly."""
        from app.llm.interface import LLMMessage

        # Reduce conversation to a plain text exchange for the structured call.
        flat_text: list[str] = []
        for m in conversation:
            if m.role == "system" and m.content:
                flat_text.append(f"SYSTEM: {m.content}")
            elif m.role == "user" and m.content:
                flat_text.append(f"USER: {m.content}")
            elif m.role == "assistant" and m.content:
                flat_text.append(f"ASSISTANT: {m.content}")
        flat_text.append(
            "USER: Emit the final structured thesis now as JSON matching the "
            "ThesisDraft schema. No prose; JSON only."
        )
        try:
            return await self.llm.complete_structured(
                messages=[LLMMessage(role="user", content="\n\n".join(flat_text))],
                model=self.model,
                schema=ThesisDraft,
                temperature=0.0,
            )
        except Exception as e:
            raise CopilotError(f"Structured fallback failed: {e}") from e

    async def _second_chance(
        self,
        conversation: list[AgentMessage],
        all_tools: list[AgentToolDef],
        tool_results: list[ToolResult],
        chain_fetches: dict[str, set[str]],
        risk_budget_usd: float | None,
        data_tool_call_count: int,
        *,
        event_sink: EventSink | None = None,
    ) -> ThesisDraft | None:
        """Run a bounded follow-up after grounding rejection."""
        budget = 4
        for _ in range(budget):
            try:
                turn = await self.llm.step_agent(
                    conversation=conversation,
                    tools=all_tools,
                    model=self.model,
                    temperature=self.temperature,
                )
            except Exception:
                return None
            conversation.append(
                AgentMessage(role="assistant", content=turn.text, tool_calls=turn.tool_calls)
            )
            if turn.text and turn.text.strip():
                await _emit(event_sink, "thinking", {"text": turn.text.strip(), "retry": True})
            if not turn.tool_calls:
                return None
            for tc in turn.tool_calls:
                if tc.name == _FINAL_TOOL_NAME:
                    await _emit(
                        event_sink,
                        "thesis_validating",
                        {"args_keys": sorted(tc.arguments.keys()), "retry": True},
                    )
                    chk = self._validate_final_thesis(
                        tc.arguments,
                        data_tool_call_count=data_tool_call_count,
                        chain_fetches=chain_fetches,
                        risk_budget_usd=risk_budget_usd,
                    )
                    if chk.ok:
                        await _emit(event_sink, "thesis_accepted", {"retry": True})
                        return chk.draft
                    await _emit(
                        event_sink,
                        "thesis_rejected",
                        {"error": chk.error, "retry": True},
                    )
                    conversation.append(
                        AgentMessage(
                            role="tool",
                            tool_result=AgentToolResult(
                                tool_call_id=tc.id,
                                name=tc.name,
                                content={"ok": False, "error": chk.error},
                            ),
                        )
                    )
                    continue
                await _emit(
                    event_sink,
                    "tool_call",
                    {"name": tc.name, "arguments": tc.arguments, "retry": True},
                )
                tr = await self.registry.execute(tc.name, tc.arguments)
                tool_results.append(tr)
                if tr.success:
                    data_tool_call_count += 1
                    if tc.name == "get_options_chain":
                        _record_chain_fetch(tr, chain_fetches)
                await _emit(
                    event_sink,
                    "tool_result",
                    {
                        "name": tc.name,
                        "success": tr.success,
                        "summary": _summarise_tool_result(tr),
                        "error": tr.error if not tr.success else None,
                        "retry": True,
                    },
                )
                conversation.append(
                    AgentMessage(
                        role="tool",
                        tool_result=AgentToolResult(
                            tool_call_id=tc.id,
                            name=tc.name,
                            content=_tool_result_payload(tr),
                        ),
                    )
                )
        return None


@dataclass(frozen=True)
class _FinalThesisCheck:
    ok: bool
    draft: ThesisDraft | None = None
    error: str | None = None

    @classmethod
    def ok_with(cls, draft: ThesisDraft) -> _FinalThesisCheck:
        return cls(ok=True, draft=draft, error=None)

    @classmethod
    def fail(cls, error: str) -> _FinalThesisCheck:
        return cls(ok=False, draft=None, error=error)


def _tool_result_payload(tr: ToolResult) -> dict[str, Any]:
    if tr.success:
        return {"ok": True, "data": _json_safe(tr.data)}
    return {"ok": False, "error": tr.error or "unknown error"}


def _summarise_tool_result(tr: ToolResult) -> str:
    """One-line summary of a tool result for the event stream. The frontend
    shows this verbatim, so it must be short, human-readable, and never leak
    raw decimals. Full payload still lands in the model's conversation; the
    summary is purely UI candy."""
    if not tr.success:
        return f"failed: {tr.error or 'unknown'}"
    data = tr.data
    if isinstance(data, dict):
        # Hand-pick fields per tool that read well in the stream. Falls back
        # to a generic "N keys" if nothing matches.
        if "last" in data and "symbol" in data:
            return f"{data['symbol']} = ${data.get('last') or data.get('mid') or '—'}"
        if "contracts" in data and isinstance(data["contracts"], list):
            return f"{len(data['contracts'])} contracts"
        if "filings" in data and isinstance(data["filings"], list):
            return f"{len(data['filings'])} filings"
        if "ratings" in data and isinstance(data["ratings"], list):
            return f"{len(data['ratings'])} analyst ratings"
        if "events" in data and isinstance(data["events"], list):
            return f"{len(data['events'])} events"
        return f"{len(data)} keys"
    if isinstance(data, list):
        return f"{len(data)} items"
    return "ok"


def _record_chain_fetch(tr: ToolResult, chain_fetches: dict[str, set[str]]) -> None:
    """Track which OCC symbols we've fetched, so we can verify the final pick is real."""
    data = tr.data
    if not isinstance(data, dict):
        return
    symbol = str(data.get("symbol", "")).upper()
    if not symbol:
        return
    contracts = data.get("contracts") or []
    for c in contracts:
        occ = str(c.get("occ_symbol", "")).upper()
        if occ:
            chain_fetches.setdefault(symbol, set()).add(occ)


def _json_safe(obj: Any) -> Any:
    if obj is None or isinstance(obj, bool | int | float | str):
        return obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    try:
        import json

        return json.loads(json.dumps(obj, default=str))
    except Exception:
        return str(obj)
