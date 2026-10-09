"""Build the per-step LLM routers from config.

One provider *instance* per provider name (cached), shared by every router —
the provider owns the client-side rate limiter, and two instances would each
think they had the whole free-tier RPM to themselves (the same bug the data
layer's shared registry fixed for Alpaca/Finnhub in July 2026).

Routes whose provider can't be constructed (key missing) are skipped with a
warning; a step with zero usable routes raises AUTH_MISSING, which the
callers already handle.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from app.core.config import get_settings
from app.core.logging import get_logger
from app.data.types import ProviderUnavailable, ProviderUnavailableReason
from app.llm.interface import LLMProvider
from app.llm.router import LLMRouter, Route

_log = get_logger(__name__)

Step = Literal["triage", "thesis"]


@lru_cache(maxsize=4)
def get_provider(name: str) -> LLMProvider:
    if name == "gemini":
        from app.llm.gemini import GeminiProvider

        return GeminiProvider()
    if name == "groq":
        from app.llm.groq import GroqProvider

        return GroqProvider()
    raise ProviderUnavailable(
        reason=ProviderUnavailableReason.NOT_IMPLEMENTED,
        message=f"LLM provider {name!r} is not wired. Supported: 'gemini', 'groq'.",
        provider=name,
        retryable=False,
    )


def parse_route_spec(spec: str) -> tuple[str, str]:
    """'groq:openai/gpt-oss-20b' -> ('groq', 'openai/gpt-oss-20b')."""
    provider, sep, model = spec.strip().partition(":")
    if not sep or not provider or not model:
        raise ValueError(f"LLM route {spec!r} must look like 'provider:model'")
    return provider.strip().lower(), model.strip()


def route_specs(step: Step) -> list[tuple[str, str]]:
    """Primary + fallbacks for a step, de-duplicated, in order."""
    s = get_settings()
    if step == "triage":
        primary = (s.llm_triage_provider, s.llm_triage_model)
        fallbacks = s.llm_triage_fallbacks
    else:
        primary = (s.llm_thesis_provider, s.llm_thesis_model)
        fallbacks = s.llm_thesis_fallbacks
    specs: list[tuple[str, str]] = [primary]
    for raw in fallbacks:
        parsed = parse_route_spec(raw)
        if parsed not in specs:
            specs.append(parsed)
    return specs


def build_router(step: Step, *, with_usage_log: bool = True) -> LLMRouter:
    routes: list[Route] = []
    for provider_name, model in route_specs(step):
        try:
            routes.append(Route(provider=get_provider(provider_name), model=model))
        except ProviderUnavailable as e:
            _log.warning(
                "llm_route_skipped",
                step=step,
                route=f"{provider_name}:{model}",
                reason=e.reason.value,
            )
    if not routes:
        raise ProviderUnavailable(
            reason=ProviderUnavailableReason.AUTH_MISSING,
            message=f"No usable LLM route for {step} (check GEMINI_API_KEY / GROQ_API_KEY)",
            provider="router",
            retryable=False,
        )
    sink = None
    if with_usage_log:
        from app.eval.persistence import get_session_factory
        from app.llm.usage_log import DbUsageSink

        sink = DbUsageSink(get_session_factory())
    _log.info("llm_router_built", step=step, routes=[r.label for r in routes])
    return LLMRouter(step=step, routes=routes, usage_sink=sink)


@lru_cache(maxsize=2)
def get_router(step: Step) -> LLMRouter:
    """Process-wide router per step (shared by worker, copilot, catalyst, API)."""
    return build_router(step)
