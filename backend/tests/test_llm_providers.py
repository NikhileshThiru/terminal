"""LLM provider tests.

GeminiProvider: real implementation, tested with the SDK's async client
patched at module level so no network is touched.

AnthropicProvider: still a stub (intentionally — DESIGN.md §3, free-forever
rule). Tested to confirm it satisfies the LLMProvider Protocol and fails in
the typed way.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.data.types import ProviderUnavailable, ProviderUnavailableReason
from app.llm.anthropic_stub import AnthropicProvider
from app.llm.gemini import GeminiProvider
from app.llm.interface import AgentMessage, AgentToolCall, AgentToolDef, LLMMessage, LLMProvider

# === Interface checks ===


def test_gemini_satisfies_interface() -> None:
    p = GeminiProvider(api_key="test")
    assert isinstance(p, LLMProvider)


def test_anthropic_stub_satisfies_interface() -> None:
    assert isinstance(AnthropicProvider(), LLMProvider)


def test_gemini_rejects_missing_key() -> None:
    with pytest.raises(ProviderUnavailable) as exc:
        GeminiProvider(api_key="")
    assert exc.value.reason == ProviderUnavailableReason.AUTH_MISSING


# === Anthropic stub continues to fail in typed way ===


@pytest.mark.asyncio
async def test_anthropic_stub_raises_typed_unavailable() -> None:
    with pytest.raises(ProviderUnavailable):
        await AnthropicProvider().complete(
            messages=[LLMMessage(role="user", content="hi")],
            model="claude-haiku-4-5",
        )


# === GeminiProvider with a fake SDK client ===


class _FakeUsage:
    def __init__(self, prompt: int = 5, candidates: int = 3) -> None:
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates


class _FakeResponse:
    def __init__(
        self,
        *,
        text: str = "",
        parsed: Any = None,
        candidates: list[Any] | None = None,
    ) -> None:
        self.text = text
        self.parsed = parsed
        self.candidates = candidates or []
        self.usage_metadata = _FakeUsage()


class _FakeAsyncModels:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        return self._response


class _FakeAio:
    def __init__(self, models: _FakeAsyncModels) -> None:
        self.models = models


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.aio = _FakeAio(_FakeAsyncModels(response))


def _patched_provider(response: _FakeResponse) -> GeminiProvider:
    p = GeminiProvider(
        api_key="test", rate_per_minute=10000, max_retries=0, initial_backoff_seconds=0.0
    )
    p._client = _FakeClient(response)  # type: ignore[assignment]
    return p


@pytest.mark.asyncio
async def test_gemini_complete_returns_text_and_usage() -> None:
    p = _patched_provider(_FakeResponse(text="hello terminal"))
    r = await p.complete(
        messages=[LLMMessage(role="user", content="hi")],
        model="gemini-2.5-flash",
    )
    assert r.text == "hello terminal"
    assert r.model == "gemini-2.5-flash"
    assert r.usage.input_tokens == 5
    assert r.usage.output_tokens == 3


@pytest.mark.asyncio
async def test_gemini_complete_translates_sdk_error_to_unavailable() -> None:
    p = GeminiProvider(
        api_key="test", rate_per_minute=10000, max_retries=0, initial_backoff_seconds=0.0
    )

    class _BrokenModels:
        async def generate_content(self, **_: Any) -> Any:
            raise RuntimeError("network down")

    p._client = SimpleNamespace(aio=SimpleNamespace(models=_BrokenModels()))  # type: ignore[assignment]

    with pytest.raises(ProviderUnavailable) as exc:
        await p.complete(
            messages=[LLMMessage(role="user", content="hi")],
            model="gemini-2.5-flash",
        )
    assert exc.value.reason == ProviderUnavailableReason.UPSTREAM_ERROR


@pytest.mark.asyncio
async def test_gemini_complete_structured_returns_parsed_model() -> None:
    from pydantic import BaseModel

    class Out(BaseModel):
        verdict: str

    parsed = Out(verdict="bullish")
    p = _patched_provider(_FakeResponse(parsed=parsed))

    result = await p.complete_structured(
        messages=[LLMMessage(role="user", content="hi")],
        model="gemini-2.5-flash",
        schema=Out,
    )
    assert isinstance(result, Out)
    assert result.verdict == "bullish"


@pytest.mark.asyncio
async def test_gemini_complete_structured_falls_back_to_text_json() -> None:
    """If .parsed is None (older SDK behavior), parse from .text."""
    from pydantic import BaseModel

    class Out(BaseModel):
        verdict: str

    p = _patched_provider(_FakeResponse(text='{"verdict": "bearish"}', parsed=None))

    result = await p.complete_structured(
        messages=[LLMMessage(role="user", content="hi")],
        model="gemini-2.5-flash",
        schema=Out,
    )
    assert result.verdict == "bearish"


@pytest.mark.asyncio
async def test_gemini_step_agent_returns_tool_calls() -> None:
    """Tool calls in the response should be lifted into provider-agnostic AgentToolCall."""

    fake_part = SimpleNamespace(
        function_call=SimpleNamespace(name="get_quote", args={"symbol": "AAPL"}),
        text=None,
    )
    fake_content = SimpleNamespace(parts=[fake_part])
    fake_cand = SimpleNamespace(content=fake_content, finish_reason="STOP")
    p = _patched_provider(_FakeResponse(candidates=[fake_cand]))

    result = await p.step_agent(
        conversation=[AgentMessage(role="user", content="quote for AAPL?")],
        tools=[
            AgentToolDef(
                name="get_quote",
                description="latest price",
                parameters={
                    "type": "object",
                    "properties": {"symbol": {"type": "string"}},
                },
            )
        ],
        model="gemini-2.5-flash",
    )
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert isinstance(tc, AgentToolCall)
    assert tc.name == "get_quote"
    assert tc.arguments == {"symbol": "AAPL"}


@pytest.mark.asyncio
async def test_gemini_step_agent_returns_text_when_no_tool_calls() -> None:
    fake_part = SimpleNamespace(function_call=None, text="final answer")
    fake_content = SimpleNamespace(parts=[fake_part])
    fake_cand = SimpleNamespace(content=fake_content, finish_reason="STOP")
    p = _patched_provider(_FakeResponse(candidates=[fake_cand]))

    result = await p.step_agent(
        conversation=[AgentMessage(role="user", content="hi")],
        tools=[],
        model="gemini-2.5-flash",
    )
    assert result.text == "final answer"
    assert result.tool_calls == []
