"""GroqProvider tests — respx-mocked against the OpenAI-compatible chat endpoint."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.data.types import ProviderUnavailable, ProviderUnavailableReason
from app.llm.groq import GroqProvider
from app.llm.interface import (
    AgentMessage,
    AgentToolCall,
    AgentToolDef,
    AgentToolResult,
    LLMMessage,
    LLMProvider,
)


def _make() -> GroqProvider:
    return GroqProvider(
        api_key="gsk_test",
        rate_per_minute=10000,
        max_retries=0,
        initial_backoff_seconds=0.0,
    )


# === Interface checks ===


def test_satisfies_interface() -> None:
    assert isinstance(_make(), LLMProvider)


def test_missing_key_raises() -> None:
    with pytest.raises(ProviderUnavailable) as exc:
        GroqProvider(api_key="")
    assert exc.value.reason == ProviderUnavailableReason.AUTH_MISSING


# === complete() ===


@pytest.mark.asyncio
@respx.mock
async def test_complete_returns_text_and_usage() -> None:
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "x",
                "model": "llama-3.3-70b-versatile",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hello terminal"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
            },
        )
    )
    p = _make()
    r = await p.complete(
        messages=[LLMMessage(role="user", content="hi")],
        model="llama-3.3-70b-versatile",
    )
    assert r.text == "hello terminal"
    assert r.model == "llama-3.3-70b-versatile"
    assert r.usage.input_tokens == 8
    assert r.usage.output_tokens == 3
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_401_propagates_as_auth_missing() -> None:
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": {"message": "Invalid API Key"}})
    )
    p = _make()
    with pytest.raises(ProviderUnavailable) as exc:
        await p.complete(
            messages=[LLMMessage(role="user", content="hi")],
            model="llama-3.3-70b-versatile",
        )
    assert exc.value.reason == ProviderUnavailableReason.AUTH_MISSING
    assert exc.value.retryable is False
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_429_after_retries_exhausted_propagates() -> None:
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(429)
    )
    p = _make()  # max_retries=0
    with pytest.raises(ProviderUnavailable) as exc:
        await p.complete(
            messages=[LLMMessage(role="user", content="hi")],
            model="llama-3.3-70b-versatile",
        )
    assert exc.value.reason == ProviderUnavailableReason.RATE_LIMITED
    await p.aclose()


# === complete_structured() ===


@pytest.mark.asyncio
@respx.mock
async def test_complete_structured_parses_json() -> None:
    from pydantic import BaseModel

    class Out(BaseModel):
        verdict: str

    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "x",
                "model": "llama-3.3-70b-versatile",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": '{"verdict": "bullish"}',
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )
    p = _make()
    result = await p.complete_structured(
        messages=[LLMMessage(role="user", content="judge AAPL")],
        model="llama-3.3-70b-versatile",
        schema=Out,
    )
    assert isinstance(result, Out)
    assert result.verdict == "bullish"
    await p.aclose()


# === step_agent() ===


@pytest.mark.asyncio
@respx.mock
async def test_step_agent_returns_tool_calls() -> None:
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "x",
                "model": "llama-3.3-70b-versatile",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_quote",
                                        "arguments": '{"symbol":"AAPL"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 40, "completion_tokens": 8},
            },
        )
    )
    p = _make()
    turn = await p.step_agent(
        conversation=[
            AgentMessage(role="system", content="You research stocks."),
            AgentMessage(role="user", content="quote AAPL?"),
        ],
        tools=[
            AgentToolDef(
                name="get_quote",
                description="latest price",
                parameters={
                    "type": "object",
                    "properties": {"symbol": {"type": "string"}},
                    "required": ["symbol"],
                },
            )
        ],
        model="llama-3.3-70b-versatile",
    )
    assert turn.text is None
    assert len(turn.tool_calls) == 1
    tc = turn.tool_calls[0]
    assert isinstance(tc, AgentToolCall)
    assert tc.name == "get_quote"
    assert tc.arguments == {"symbol": "AAPL"}
    assert tc.id == "call_1"
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_step_agent_returns_text_when_done() -> None:
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "x",
                "model": "llama-3.3-70b-versatile",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "I think AAPL is fine.",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 6},
            },
        )
    )
    p = _make()
    turn = await p.step_agent(
        conversation=[AgentMessage(role="user", content="thoughts?")],
        tools=[],
        model="llama-3.3-70b-versatile",
    )
    assert turn.text == "I think AAPL is fine."
    assert turn.tool_calls == []
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_step_agent_sends_tool_result_in_history() -> None:
    """Verify our AgentToolResult turn translates to OpenAI-format `role:tool`."""
    captured: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "x",
                "model": "llama-3.3-70b-versatile",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 30, "completion_tokens": 2},
            },
        )

    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(side_effect=_capture)

    p = _make()
    await p.step_agent(
        conversation=[
            AgentMessage(role="user", content="get AAPL"),
            AgentMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    AgentToolCall(id="call_1", name="get_quote", arguments={"symbol": "AAPL"})
                ],
            ),
            AgentMessage(
                role="tool",
                tool_result=AgentToolResult(
                    tool_call_id="call_1", name="get_quote", content={"last": "312.06"}
                ),
            ),
        ],
        tools=[],
        model="llama-3.3-70b-versatile",
    )
    msgs = captured["body"]["messages"]
    # Find the tool message
    tool_msg = next(m for m in msgs if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "call_1"
    assert tool_msg["name"] == "get_quote"
    assert "312.06" in tool_msg["content"]
    await p.aclose()
