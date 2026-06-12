"""Anthropic (Claude) provider — Phase 1 stub.

Wired alongside Gemini from day 1 to prove the LLMProvider interface is
genuinely model-agnostic (DESIGN.md §3). Real impl lands in Phase 4.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from app.data.types import ProviderUnavailable, ProviderUnavailableReason
from app.llm.interface import (
    AgentMessage,
    AgentToolDef,
    AgentTurn,
    LLMMessage,
    LLMResponse,
    LLMUsage,
)

T = TypeVar("T", bound=BaseModel)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    async def complete(
        self,
        messages: list[LLMMessage],
        model: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> LLMResponse:
        raise ProviderUnavailable(
            reason=ProviderUnavailableReason.NOT_IMPLEMENTED,
            message="AnthropicProvider.complete is a Phase 1 stub; real impl in Phase 4",
            provider=self.name,
            retryable=False,
        )

    async def complete_structured(
        self,
        messages: list[LLMMessage],
        model: str,
        schema: type[T],
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> T:
        raise ProviderUnavailable(
            reason=ProviderUnavailableReason.NOT_IMPLEMENTED,
            message="AnthropicProvider.complete_structured is a Phase 1 stub",
            provider=self.name,
            retryable=False,
        )

    async def step_agent(
        self,
        conversation: list[AgentMessage],
        tools: list[AgentToolDef],
        model: str,
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> AgentTurn:
        raise ProviderUnavailable(
            reason=ProviderUnavailableReason.NOT_IMPLEMENTED,
            message=(
                "AnthropicProvider.step_agent is a stub. "
                "DESIGN.md §2.1 (free forever) precludes wiring this provider in production."
            ),
            provider=self.name,
            retryable=False,
        )

    @staticmethod
    def _unused_helper(_: LLMUsage) -> None:  # pragma: no cover
        """Suppress unused-import warning for LLMUsage (kept for symmetry with other providers)."""
        return None
