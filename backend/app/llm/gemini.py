"""Gemini LLM provider — production implementation (DESIGN.md §3).

Uses google-genai async client. Free-tier quotas are per model and change
often (verify in AI Studio); daily quotas reset at midnight US/Pacific.
Speaks the generic LLMProvider interface (app/llm/interface.py) — internal
google.genai types stay within this module.

429 handling is quota-aware (app/llm/quota.py): a short per-minute limit is
waited out inline; a per-day limit raises RATE_LIMITED immediately with a
retry-after hint so the LLM router can fail over instead of sleeping.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from collections.abc import Sequence
from typing import Any, TypeVar

from google import genai
from google.genai import types as gtypes
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
from app.llm.quota import (
    is_daily_quota,
    is_rate_limit,
    parse_retry_after,
    seconds_until_pacific_midnight,
)

_log = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


def _messages_to_gemini(
    messages: Sequence[LLMMessage],
) -> tuple[str | None, list[gtypes.Content]]:
    system_parts: list[str] = []
    contents: list[gtypes.Content] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
            continue
        role = "user" if m.role == "user" else "model"
        contents.append(gtypes.Content(role=role, parts=[gtypes.Part.from_text(text=m.content)]))
    system_instruction = "\n\n".join(system_parts) if system_parts else None
    return system_instruction, contents


# Gemini 3 rejects a function-call turn without the thought_signature it was
# issued with ("Function call is missing a thought_signature", HTTP 400).
# For calls that came from a *different* model (router failover mid-thesis),
# Google documents this sentinel to skip validation of the foreign trace.
_SKIP_SIGNATURE = b"skip_thought_signature_validator"


# Gemini 2.5/3 "think" before answering, and the thinking tokens count
# against max_output_tokens. A 2,048 budget let the thinking crowd out the
# answer: live theses failed with truncated JSON ("EOF while parsing a
# string"), and the router then burned a call on every route retrying it.
# Output tokens aren't the scarce resource on the free tier (requests are),
# so floor the budget well above any thesis we'd ever emit.
_MIN_OUTPUT_TOKENS = 8192


def _output_budget(requested: int) -> int:
    return max(requested, _MIN_OUTPUT_TOKENS)


def _requires_signature(model: str) -> bool:
    return model.startswith("gemini-3")


def _signature_for(tc: AgentToolCall, target_model: str) -> bytes | None:
    if tc.signature and tc.signature_model == target_model:
        return base64.b64decode(tc.signature)
    if _requires_signature(target_model):
        return _SKIP_SIGNATURE
    return None


def _agent_messages_to_gemini(
    conversation: Sequence[AgentMessage],
    target_model: str = "",
) -> tuple[str | None, list[gtypes.Content]]:
    """Translate generic AgentMessage list → (system_instruction, gemini contents).

    Gemini conventions:
    - system: separate field, not in contents
    - user text: Content(role="user", parts=[Part.from_text(...)])
    - assistant text: Content(role="model", parts=[Part.from_text(...)])
    - assistant tool calls: Content(role="model", parts=[Part.from_function_call(...)])
    - tool results: Content(role="user", parts=[Part.from_function_response(...)])
    """
    system_parts: list[str] = []
    contents: list[gtypes.Content] = []
    for m in conversation:
        if m.role == "system":
            if m.content:
                system_parts.append(m.content)
            continue
        if m.role == "tool":
            if m.tool_result is None:
                continue
            contents.append(
                gtypes.Content(
                    role="user",
                    parts=[
                        gtypes.Part.from_function_response(
                            name=m.tool_result.name,
                            response={"result": m.tool_result.content},
                        )
                    ],
                )
            )
            continue
        if m.role == "assistant":
            parts: list[gtypes.Part] = []
            if m.content:
                parts.append(gtypes.Part.from_text(text=m.content))
            for tc in m.tool_calls:
                parts.append(
                    gtypes.Part(
                        function_call=gtypes.FunctionCall(name=tc.name, args=tc.arguments),
                        thought_signature=_signature_for(tc, target_model),
                    )
                )
            if parts:
                contents.append(gtypes.Content(role="model", parts=parts))
            continue
        # role == "user"
        contents.append(
            gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=m.content or "")])
        )
    system_instruction = "\n\n".join(system_parts) if system_parts else None
    return system_instruction, contents


def _tools_to_gemini(tools: Sequence[AgentToolDef]) -> list[gtypes.Tool]:
    if not tools:
        return []
    declarations: list[gtypes.FunctionDeclaration] = []
    for t in tools:
        declarations.append(
            gtypes.FunctionDeclaration(
                name=t.name, description=t.description, parameters=t.parameters
            )
        )
    return [gtypes.Tool(function_declarations=declarations)]


def _extract_usage(response: Any) -> LLMUsage:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return LLMUsage()
    return LLMUsage(
        input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
        output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
    )


class GeminiProvider:
    """Production Gemini LLMProvider implementation."""

    name = "gemini"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        rate_per_minute: float = 5.0,
        burst: int = 3,
        max_retries: int = 3,
        initial_backoff_seconds: float = 8.0,
        max_inline_wait_seconds: float = 30.0,
        max_transient_retries: int = 1,
        transient_backoff_seconds: float = 3.0,
    ) -> None:
        resolved = api_key if api_key is not None else get_settings().gemini_api_key
        if not resolved:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.AUTH_MISSING,
                message="GEMINI_API_KEY is not set",
                provider=self.name,
                retryable=False,
            )
        self._client = genai.Client(api_key=resolved)
        self._rate_limiter = RateLimiter(rate_per_sec=rate_per_minute / 60.0, burst=burst)
        self._max_retries = max_retries
        self._initial_backoff = initial_backoff_seconds
        self._max_inline_wait = max_inline_wait_seconds
        # 503 "model is experiencing high demand": one quick retry, then hand
        # the call to the router, which fails over to the next Gemini model.
        # (The old 8+16+32 s backoff spent 56 s on one call — the whole
        # catalyst-path latency budget — while healthy models sat idle.)
        self._max_transient_retries = max_transient_retries
        self._transient_backoff = transient_backoff_seconds

    async def _generate(self, **kwargs: Any) -> Any:
        """Wrap generate_content with rate limit + quota-aware retry.

        Raises ProviderUnavailable (RATE_LIMITED / MODEL_NOT_FOUND) for the
        cases the router must see; other errors propagate for the caller to
        wrap as UPSTREAM_ERROR.
        """
        model = str(kwargs.get("model", ""))
        transient_retries = 0
        for attempt in range(self._max_retries + 1):
            await self._rate_limiter.acquire()
            try:
                return await self._client.aio.models.generate_content(**kwargs)
            except Exception as e:
                msg = str(e)
                if is_rate_limit(msg):
                    hint = parse_retry_after(msg)
                    if is_daily_quota(msg):
                        raise ProviderUnavailable(
                            reason=ProviderUnavailableReason.RATE_LIMITED,
                            message=f"Gemini daily quota exhausted for {model}",
                            provider=self.name,
                            retry_after_seconds=max(hint or 0.0, seconds_until_pacific_midnight()),
                        ) from e
                    wait = hint if hint is not None else self._initial_backoff * (2**attempt)
                    if attempt < self._max_retries and wait <= self._max_inline_wait:
                        _log.warning(
                            "gemini_rate_limit_retry",
                            model=model,
                            attempt=attempt + 1,
                            wait_seconds=wait,
                        )
                        await asyncio.sleep(wait)
                        continue
                    raise ProviderUnavailable(
                        reason=ProviderUnavailableReason.RATE_LIMITED,
                        message=f"Gemini rate-limited on {model}",
                        provider=self.name,
                        retry_after_seconds=hint,
                    ) from e
                if "404" in msg or "NOT_FOUND" in msg:
                    raise ProviderUnavailable(
                        reason=ProviderUnavailableReason.MODEL_NOT_FOUND,
                        message=f"Gemini model {model!r} not found",
                        provider=self.name,
                        retryable=False,
                    ) from e
                transient = "503" in msg or "UNAVAILABLE" in msg
                if transient and transient_retries < self._max_transient_retries:
                    transient_retries += 1
                    _log.warning(
                        "gemini_transient_retry",
                        model=model,
                        attempt=transient_retries,
                        backoff_seconds=self._transient_backoff,
                        error_class=type(e).__name__,
                    )
                    await asyncio.sleep(self._transient_backoff)
                    continue
                if transient:
                    raise ProviderUnavailable(
                        reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                        message=f"Gemini {model} overloaded (503) after retry",
                        provider=self.name,
                    ) from e
                raise
        raise RuntimeError("unreachable")  # pragma: no cover — loop always returns or raises

    async def complete(
        self,
        messages: list[LLMMessage],
        model: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> LLMResponse:
        system_instruction, contents = _messages_to_gemini(messages)
        config = gtypes.GenerateContentConfig(
            system_instruction=system_instruction,
            max_output_tokens=_output_budget(max_tokens),
            temperature=temperature,
        )
        try:
            response = await self._generate(model=model, contents=contents, config=config)
        except ProviderUnavailable:
            raise
        except Exception as e:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                message=f"Gemini request failed: {e}",
                provider=self.name,
            ) from e
        usage = _extract_usage(response)
        get_cost_tracker().record(
            provider=self.name,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
        return LLMResponse(text=response.text or "", model=model, usage=usage)

    async def complete_structured(
        self,
        messages: list[LLMMessage],
        model: str,
        schema: type[T],
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> T:
        system_instruction, contents = _messages_to_gemini(messages)
        config = gtypes.GenerateContentConfig(
            system_instruction=system_instruction,
            max_output_tokens=_output_budget(max_tokens),
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=schema,
        )
        try:
            response = await self._generate(model=model, contents=contents, config=config)
        except ProviderUnavailable:
            raise
        except Exception as e:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                message=f"Gemini structured request failed: {e}",
                provider=self.name,
            ) from e
        usage = _extract_usage(response)
        get_cost_tracker().record(
            provider=self.name,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, schema):
            return parsed
        try:
            return schema.model_validate_json(response.text or "")
        except Exception as e:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                message=f"Gemini structured response could not be parsed: {e}",
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
        system_instruction, contents = _agent_messages_to_gemini(conversation, model)
        gtools = _tools_to_gemini(tools)
        config = gtypes.GenerateContentConfig(
            system_instruction=system_instruction,
            max_output_tokens=_output_budget(max_tokens),
            temperature=temperature,
            tools=gtools,
        )
        try:
            response = await self._generate(model=model, contents=contents, config=config)
        except ProviderUnavailable:
            raise
        except Exception as e:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                message=f"Gemini agent step failed: {e}",
                provider=self.name,
            ) from e

        text_parts: list[str] = []
        tool_calls: list[AgentToolCall] = []
        finish_reason: str | None = None

        candidates = getattr(response, "candidates", None) or []
        for cand in candidates:
            finish_reason = getattr(cand, "finish_reason", None) or finish_reason
            content = getattr(cand, "content", None)
            if content is None:
                continue
            for part in content.parts or []:
                fc = getattr(part, "function_call", None)
                if fc is not None and getattr(fc, "name", None):
                    args = dict(fc.args) if fc.args else {}
                    sig = getattr(part, "thought_signature", None)
                    tool_calls.append(
                        AgentToolCall(
                            id=f"gem_{uuid.uuid4().hex[:12]}",
                            name=fc.name,
                            arguments=args,
                            signature=base64.b64encode(sig).decode() if sig else None,
                            signature_model=model if sig else None,
                        )
                    )
                    continue
                text = getattr(part, "text", None)
                if text:
                    text_parts.append(text)

        usage = _extract_usage(response)
        get_cost_tracker().record(
            provider=self.name,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
        return AgentTurn(
            text="".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            finish_reason=str(finish_reason) if finish_reason is not None else None,
            usage=usage,
            provider=self.name,
            model=model,
        )
