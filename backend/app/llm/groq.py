"""Groq LLM provider (DESIGN.md §3).

Groq runs open-weights models (Llama, Mixtral, etc.) on custom LPU silicon —
inference is roughly an order of magnitude faster than typical GPU-hosted
APIs. Free tier: ~30 RPM, generous RPD, no card, no SSN. Fits the
free-forever rule cleanly.

The API is OpenAI-compatible (POST /v1/chat/completions). We use httpx
directly rather than the openai SDK to keep the dep list lean.

Speaks the generic LLMProvider interface — OpenAI-format internals stay
inside this module.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.logging import get_logger
from app.data.rate_limit import RateLimiter
from app.data.types import ProviderUnavailable, ProviderUnavailableReason
from app.llm.cost import get_cost_tracker
from app.llm.interface import (
    AgentMessage,
    AgentToolCall,
    AgentToolDef,
    AgentTurn,
    LLMMessage,
    LLMResponse,
    LLMUsage,
)

_log = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

_BASE_URL = "https://api.groq.com/openai/v1"


def _llm_messages_to_openai(messages: Sequence[LLMMessage]) -> list[dict[str, Any]]:
    return [{"role": m.role, "content": m.content} for m in messages]


def _agent_messages_to_openai(conversation: Sequence[AgentMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in conversation:
        if m.role == "system":
            out.append({"role": "system", "content": m.content or ""})
            continue
        if m.role == "user":
            out.append({"role": "user", "content": m.content or ""})
            continue
        if m.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": m.content}
            if m.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in m.tool_calls
                ]
            out.append(entry)
            continue
        if m.role == "tool" and m.tool_result is not None:
            content = m.tool_result.content
            if not isinstance(content, str):
                content = json.dumps(content, default=str)
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m.tool_result.tool_call_id,
                    "name": m.tool_result.name,
                    "content": content,
                }
            )
    return out


def _tools_to_openai(tools: Sequence[AgentToolDef]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


def _extract_usage(payload: dict[str, Any]) -> LLMUsage:
    usage = payload.get("usage") or {}
    return LLMUsage(
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
    )


class GroqProvider:
    """Groq LLMProvider — OpenAI-compatible chat completions."""

    name = "groq"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        rate_per_minute: float = 30.0,
        burst: int = 5,
        max_retries: int = 3,
        initial_backoff_seconds: float = 4.0,
        timeout_seconds: float = 60.0,
    ) -> None:
        resolved = api_key if api_key is not None else get_settings().groq_api_key
        if not resolved:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.AUTH_MISSING,
                message="GROQ_API_KEY is not set",
                provider=self.name,
                retryable=False,
            )
        self._api_key = resolved
        self._client = (
            client
            if client is not None
            else httpx.AsyncClient(
                base_url=_BASE_URL,
                timeout=timeout_seconds,
                headers={
                    "Authorization": f"Bearer {resolved}",
                    "Content-Type": "application/json",
                },
            )
        )
        self._rate_limiter = RateLimiter(rate_per_sec=rate_per_minute / 60.0, burst=burst)
        self._max_retries = max_retries
        self._initial_backoff = initial_backoff_seconds

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _chat(self, body: dict[str, Any]) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(self._max_retries + 1):
            await self._rate_limiter.acquire()
            try:
                resp = await self._client.post("/chat/completions", json=body)
            except Exception as e:
                last = e
                if attempt < self._max_retries:
                    backoff = self._initial_backoff * (2**attempt)
                    _log.warning(
                        "groq_network_retry",
                        attempt=attempt + 1,
                        backoff_seconds=backoff,
                        error_class=type(e).__name__,
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise

            if resp.status_code == 429 and attempt < self._max_retries:
                backoff = self._initial_backoff * (2**attempt)
                _log.warning("groq_429_retry", attempt=attempt + 1, backoff_seconds=backoff)
                await asyncio.sleep(backoff)
                continue
            if resp.status_code == 429:
                raise ProviderUnavailable(
                    reason=ProviderUnavailableReason.RATE_LIMITED,
                    message=f"Groq returned 429 after {self._max_retries} retries",
                    provider=self.name,
                )
            if resp.status_code in (401, 403):
                raise ProviderUnavailable(
                    reason=ProviderUnavailableReason.AUTH_MISSING,
                    message=f"Groq returned {resp.status_code} (auth)",
                    provider=self.name,
                    retryable=False,
                )
            if resp.status_code != 200:
                raise ProviderUnavailable(
                    reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                    message=f"Groq returned HTTP {resp.status_code}: {resp.text[:200]}",
                    provider=self.name,
                )
            return resp.json()  # type: ignore[no-any-return]

        if last is not None:
            raise last
        raise RuntimeError("unreachable")

    async def complete(
        self,
        messages: list[LLMMessage],
        model: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> LLMResponse:
        body = {
            "model": model,
            "messages": _llm_messages_to_openai(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        data = await self._chat(body)
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        usage = _extract_usage(data)
        get_cost_tracker().record(
            provider=self.name,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
        return LLMResponse(text=text, model=data.get("model", model), usage=usage)

    async def complete_structured(
        self,
        messages: list[LLMMessage],
        model: str,
        schema: type[T],
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> T:
        """Use response_format=json_object plus the schema inlined in the system prompt."""
        json_schema = schema.model_json_schema()
        schema_hint = (
            "Respond with ONLY a valid JSON object matching this JSON schema:\n"
            f"{json.dumps(json_schema)}\n"
            "No prose, no markdown fences — JSON only."
        )
        prepended: list[LLMMessage] = [LLMMessage(role="system", content=schema_hint)]
        prepended.extend(messages)
        body = {
            "model": model,
            "messages": _llm_messages_to_openai(prepended),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        data = await self._chat(body)
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        try:
            return schema.model_validate_json(text)
        except Exception as e:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                message=f"Groq structured response could not be parsed: {e}; raw: {text[:200]}",
                provider=self.name,
            ) from e

    async def step_agent(
        self,
        conversation: list[AgentMessage],
        tools: list[AgentToolDef],
        model: str,
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> AgentTurn:
        body: dict[str, Any] = {
            "model": model,
            "messages": _agent_messages_to_openai(conversation),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            body["tools"] = _tools_to_openai(tools)
            body["tool_choice"] = "auto"
        data = await self._chat(body)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content")
        finish_reason = choice.get("finish_reason")

        tool_calls: list[AgentToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            if not name:
                continue
            raw_args = fn.get("arguments")
            args: dict[str, Any] = {}
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args else {}
                except json.JSONDecodeError:
                    args = {}
            elif isinstance(raw_args, dict):
                args = raw_args
            tool_calls.append(
                AgentToolCall(id=tc.get("id") or f"groq_{name}", name=name, arguments=args)
            )

        usage = _extract_usage(data)
        get_cost_tracker().record(
            provider=self.name,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
        return AgentTurn(
            text=text or None,
            tool_calls=tool_calls,
            finish_reason=str(finish_reason) if finish_reason else None,
            usage=usage,
        )
