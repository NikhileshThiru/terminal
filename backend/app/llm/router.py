"""LLMRouter — ordered provider:model fallback behind the LLMProvider Protocol.

Each pipeline step gets one router (built in app/llm/factory.py from config):

    triage: groq:openai/gpt-oss-20b -> groq:openai/gpt-oss-120b -> gemini:gemini-3.1-flash-lite
    thesis: gemini:gemini-2.5-flash -> 3.5-flash -> 3.7-flash -> 3.6-flash -> 3-flash-preview
            -> 3.8-flash -> 2.5-flash-lite

Per call, the router walks the routes in order, skipping any route the shared
health registry has in cooldown, and returns the first success. Every
attempt — success or failure — is timed and handed to the usage sink
(`llm_calls` table). When every route is cooling down or fails, it raises a
single RATE_LIMITED/UPSTREAM_ERROR ProviderUnavailable naming the earliest
recovery time, so callers degrade the same way they always have.

Callers still pass `model=` (the Protocol requires it) but the router ignores
it: each route carries its own model, because a fallback to a different
provider is meaningless with the primary's model id.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import BaseModel

from app.core.correlation import get_correlation_id
from app.core.logging import get_logger
from app.data.types import ProviderUnavailable, ProviderUnavailableReason
from app.llm.cost import last_usage, reset_last_usage
from app.llm.health import LLMHealthRegistry, get_llm_health
from app.llm.interface import (
    AgentMessage,
    AgentToolDef,
    AgentTurn,
    LLMMessage,
    LLMProvider,
    LLMResponse,
)
from app.llm.usage_log import UsageRecord

_log = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")

UsageSink = Callable[[UsageRecord], None]


@dataclass(frozen=True)
class Route:
    provider: LLMProvider
    model: str

    @property
    def label(self) -> str:
        return f"{self.provider.name}:{self.model}"


class LLMRouter:
    """An LLMProvider that fails over across routes. See module docstring."""

    def __init__(
        self,
        *,
        step: str,
        routes: list[Route],
        health: LLMHealthRegistry | None = None,
        usage_sink: UsageSink | None = None,
    ) -> None:
        if not routes:
            raise ValueError("LLMRouter needs at least one route")
        self.step = step
        self.routes = routes
        self._health = health
        self._usage_sink = usage_sink
        # Display name = the primary provider; per-turn provenance comes from
        # AgentTurn.provider/model, which the providers stamp.
        self.name = routes[0].provider.name

    @property
    def health(self) -> LLMHealthRegistry:
        return self._health if self._health is not None else get_llm_health()

    @property
    def primary_model(self) -> str:
        return self.routes[0].model

    def describe(self) -> list[dict[str, object]]:
        """Routes in order with their live health — for /llm/health."""
        out: list[dict[str, object]] = []
        for i, r in enumerate(self.routes):
            entry = self.health.get(r.provider.name, r.model).to_dict()
            entry["position"] = i
            out.append(entry)
        return out

    async def _route_call(self, method: str, invoke: Callable[[Route], Awaitable[R]]) -> R:
        now = datetime.now(UTC)
        errors: list[str] = []
        earliest_recovery: datetime | None = None
        all_rate_limited = True

        for route in self.routes:
            h = self.health.get(route.provider.name, route.model)
            if h.cooling(now):
                errors.append(f"{route.label} cooling until {h.cooldown_until:%H:%M:%S}Z")
                if h.cooldown_until is not None and (
                    earliest_recovery is None or h.cooldown_until < earliest_recovery
                ):
                    earliest_recovery = h.cooldown_until
                continue

            reset_last_usage()
            started = time.monotonic()
            try:
                result = await invoke(route)
            except Exception as e:
                latency_ms = int((time.monotonic() - started) * 1000)
                reason = (
                    e.reason.value
                    if isinstance(e, ProviderUnavailable)
                    else ProviderUnavailableReason.UPSTREAM_ERROR.value
                )
                if reason != ProviderUnavailableReason.RATE_LIMITED.value:
                    all_rate_limited = False
                cooldown = self.health.record_failure(route.provider.name, route.model, e)
                self._emit(route, method, ok=False, latency_ms=latency_ms, error_reason=reason)
                errors.append(f"{route.label}: {reason}")
                log = _log.error if reason == "model_not_found" else _log.warning
                log(
                    "llm_route_failed",
                    step=self.step,
                    route=route.label,
                    method=method,
                    reason=reason,
                    cooldown_seconds=cooldown,
                    error=str(e)[:200],
                )
                continue

            latency_ms = int((time.monotonic() - started) * 1000)
            self.health.record_success(route.provider.name, route.model)
            tokens = last_usage() or (0, 0)
            self._emit(
                route,
                method,
                ok=True,
                latency_ms=latency_ms,
                input_tokens=tokens[0],
                output_tokens=tokens[1],
            )
            if route is not self.routes[0]:
                _log.info("llm_route_fallback_served", step=self.step, route=route.label)
            return result

        detail = "; ".join(errors)
        if all_rate_limited:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.RATE_LIMITED,
                message=f"all {self.step} LLM routes rate-limited/cooling ({detail})",
                provider="router",
                retry_after_seconds=(
                    (earliest_recovery - now).total_seconds() if earliest_recovery else None
                ),
            )
        raise ProviderUnavailable(
            reason=ProviderUnavailableReason.UPSTREAM_ERROR,
            message=f"all {self.step} LLM routes failed ({detail})",
            provider="router",
        )

    def _emit(
        self,
        route: Route,
        method: str,
        *,
        ok: bool,
        latency_ms: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error_reason: str | None = None,
    ) -> None:
        if self._usage_sink is None:
            return
        try:
            self._usage_sink(
                UsageRecord(
                    step=self.step,
                    provider=route.provider.name,
                    model=route.model,
                    method=method,
                    ok=ok,
                    latency_ms=latency_ms,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    error_reason=error_reason,
                    correlation_id=get_correlation_id(),
                )
            )
        except Exception as e:  # pragma: no cover — sink must never break a call
            _log.warning("llm_usage_sink_failed", error=str(e)[:200])

    # === LLMProvider Protocol ===

    async def complete(
        self,
        messages: list[LLMMessage],
        model: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> LLMResponse:
        del model  # routes carry their own models

        async def invoke(r: Route) -> LLMResponse:
            return await r.provider.complete(
                messages, model=r.model, max_tokens=max_tokens, temperature=temperature
            )

        return await self._route_call("complete", invoke)

    async def complete_structured(
        self,
        messages: list[LLMMessage],
        model: str,
        schema: type[T],
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> T:
        del model

        async def invoke(r: Route) -> T:
            return await r.provider.complete_structured(
                messages,
                model=r.model,
                schema=schema,
                max_tokens=max_tokens,
                temperature=temperature,
            )

        return await self._route_call("complete_structured", invoke)

    async def step_agent(
        self,
        conversation: list[AgentMessage],
        tools: list[AgentToolDef],
        model: str,
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> AgentTurn:
        del model

        async def invoke(r: Route) -> AgentTurn:
            turn = await r.provider.step_agent(
                conversation,
                tools,
                model=r.model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            # Providers stamp provenance; fill it in for any that don't.
            updates: dict[str, Any] = {}
            if turn.provider is None:
                updates["provider"] = r.provider.name
            if turn.model is None:
                updates["model"] = r.model
            return turn.model_copy(update=updates) if updates else turn

        return await self._route_call("step_agent", invoke)
