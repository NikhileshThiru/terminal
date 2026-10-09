"""LLM cost telemetry.

In-memory rolling tracker for token usage and notional dollar cost across
providers/models. The numbers are notional because the project runs on
free tiers — the chip in the header is about *instrumentation*, not
billing. Pricing constants are kept in this file so swapping providers
or tuning model selection has one place to update.

Resets on process restart. Durable per-call accounting (survives restarts,
feeds the daily-budget chip) lives in the `llm_calls` table written by the
LLM router — see app/llm/usage_log.py. This tracker stays as the cheap
in-process view and as the hand-off point for per-call token counts
(`last_usage()`), which the router reads after each call.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import TypedDict

# Per-million-token prices in USD. Sourced from the provider's published
# pricing pages, recorded here as constants so the cost endpoint stays
# self-contained. Unknown models fall back to a free-tier zero.
PRICING_USD_PER_1M: dict[str, tuple[float, float]] = {
    # Gemini (Google AI Studio pricing, 2026; free tier zeros these in practice)
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.0-flash": (0.075, 0.30),
    "gemini-1.5-flash": (0.075, 0.30),
    # Groq (paid-tier list prices; the free tier zeroes these in practice)
    "openai/gpt-oss-20b": (0.075, 0.30),
    "openai/gpt-oss-120b": (0.15, 0.60),
    "llama-3.3-70b-versatile": (0.59, 0.79),  # retired upstream; kept for old rows
    # Defaults for unknown models — explicit zero, not a guess.
}


# Token counts of the most recent successful call in this asyncio context.
# Providers set it inside `record()`; the router (which awaits the provider
# in the same context) reads it back — the only way to get token counts out
# of `complete_structured`, whose return type is the caller's schema.
_last_usage: ContextVar[tuple[int, int] | None] = ContextVar("llm_last_usage", default=None)


def last_usage() -> tuple[int, int] | None:
    """(input_tokens, output_tokens) of the last recorded call, if any."""
    return _last_usage.get()


def reset_last_usage() -> None:
    _last_usage.set(None)


def _price_for(model: str) -> tuple[float, float]:
    return PRICING_USD_PER_1M.get(model, (0.0, 0.0))


def compute_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    in_per_m, out_per_m = _price_for(model)
    return (input_tokens / 1_000_000) * in_per_m + (output_tokens / 1_000_000) * out_per_m


@dataclass
class _ModelStat:
    provider: str
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class CostBreakdown(TypedDict):
    provider: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass
class CostSummary:
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    since: datetime
    by_model: list[CostBreakdown] = field(default_factory=list)


class CostTracker:
    """Process-global cost accounting. Thread-safe; rolls over per process
    restart. Providers call `record(...)` after every successful LLM call."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._since = datetime.now(UTC)
        self._stats: dict[tuple[str, str], _ModelStat] = {}

    def record(self, *, provider: str, model: str, input_tokens: int, output_tokens: int) -> None:
        _last_usage.set((input_tokens, output_tokens))
        if input_tokens <= 0 and output_tokens <= 0:
            return
        key = (provider, model)
        cost = compute_cost_usd(model, input_tokens, output_tokens)
        with self._lock:
            stat = self._stats.get(key)
            if stat is None:
                stat = _ModelStat(provider=provider, model=model)
                self._stats[key] = stat
            stat.calls += 1
            stat.input_tokens += input_tokens
            stat.output_tokens += output_tokens
            stat.cost_usd += cost

    def summary(self) -> CostSummary:
        with self._lock:
            stats = list(self._stats.values())
        return CostSummary(
            calls=sum(s.calls for s in stats),
            input_tokens=sum(s.input_tokens for s in stats),
            output_tokens=sum(s.output_tokens for s in stats),
            cost_usd=sum(s.cost_usd for s in stats),
            since=self._since,
            by_model=[
                CostBreakdown(
                    provider=s.provider,
                    model=s.model,
                    calls=s.calls,
                    input_tokens=s.input_tokens,
                    output_tokens=s.output_tokens,
                    cost_usd=s.cost_usd,
                )
                for s in sorted(stats, key=lambda x: x.cost_usd, reverse=True)
            ],
        )

    def reset(self) -> None:
        with self._lock:
            self._since = datetime.now(UTC)
            self._stats.clear()


_tracker = CostTracker()


def get_cost_tracker() -> CostTracker:
    return _tracker
