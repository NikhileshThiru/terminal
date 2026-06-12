"""LLM provider Protocol — provider-agnostic tool-calling (DESIGN.md §3).

The agent loop in app/agent/copilot.py speaks ONLY in the types below; the
concrete provider (Gemini, Groq, Ollama-local) translates to/from its
native shape. That's how the model-agnostic-by-config promise actually works.

Tool definitions cross the wire as plain JSON-schema dicts — the lowest
common denominator that both Gemini and OpenAI-compatible APIs accept.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


# === Generic types for the agent loop ===


class AgentToolCall(BaseModel):
    """A function call the model emitted."""

    id: str  # provider-assigned call id; opaque to us
    name: str
    arguments: dict[str, Any]


class AgentToolResult(BaseModel):
    """Our response after executing a tool call."""

    tool_call_id: str  # echoes the matching AgentToolCall.id
    name: str
    content: Any  # JSON-serializable result; providers stringify if needed


class AgentMessage(BaseModel):
    """One turn in a tool-calling conversation. Provider-neutral."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None  # user/assistant/system text
    tool_calls: list[AgentToolCall] = []  # assistant turn that wants to call tools
    tool_result: AgentToolResult | None = None  # tool turn carrying a result


class AgentToolDef(BaseModel):
    """A tool the model can call. JSON-schema parameters work across providers."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema


class AgentTurn(BaseModel):
    """One round-trip response from the model — text and/or tool calls."""

    text: str | None = None
    tool_calls: list[AgentToolCall] = []
    finish_reason: str | None = None
    usage: LLMUsage


class LLMMessage(BaseModel):
    """Simple text-only message used by complete() / complete_structured()."""

    role: Literal["system", "user", "assistant"]
    content: str


class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class LLMResponse(BaseModel):
    text: str
    model: str
    usage: LLMUsage


AgentTurn.model_rebuild()


@runtime_checkable
class LLMProvider(Protocol):
    """The pluggable LLM backend. Manual + autonomous funnels speak only this."""

    name: str  # "gemini" | "groq" | "anthropic" | "ollama"

    async def complete(
        self,
        messages: list[LLMMessage],
        model: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> LLMResponse: ...

    async def complete_structured(
        self,
        messages: list[LLMMessage],
        model: str,
        schema: type[T],
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> T: ...

    async def step_agent(
        self,
        conversation: list[AgentMessage],
        tools: list[AgentToolDef],
        model: str,
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> AgentTurn:
        """Single round of the tool-calling loop. Returns the model's next turn."""
        ...
