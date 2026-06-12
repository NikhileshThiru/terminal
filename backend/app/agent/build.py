"""Wire providers + tool registry + LLM into a fully-formed Copilot.

The thesis-step LLM is selected by `LLM_THESIS_PROVIDER` in config — Gemini
(default; 1M TPM is roomy enough for our multi-tool conversations) or Groq
(wired and tested but TPM-tight for thesis, fine as a fallback). Both speak
the LLMProvider Protocol. Providers may raise `ProviderUnavailable(AUTH_MISSING)`
if a key isn't set in the environment; the endpoint catches that.
"""

from __future__ import annotations

from functools import lru_cache

from app.agent.copilot import Copilot
from app.agent.tools import ToolRegistry
from app.core.config import get_settings
from app.core.logging import get_logger
from app.data.alpaca import AlpacaProvider
from app.data.edgar import EdgarFilingsProvider
from app.data.finnhub import FinnhubProvider
from app.data.types import ProviderUnavailable, ProviderUnavailableReason
from app.llm.gemini import GeminiProvider
from app.llm.groq import GroqProvider
from app.llm.interface import LLMProvider

_log = get_logger(__name__)


def _build_llm(provider_name: str) -> LLMProvider:
    if provider_name == "groq":
        return GroqProvider()
    if provider_name == "gemini":
        return GeminiProvider()
    raise ProviderUnavailable(
        reason=ProviderUnavailableReason.NOT_IMPLEMENTED,
        message=(
            f"LLM provider {provider_name!r} is configured but not wired. "
            "Supported: 'gemini', 'groq'."
        ),
        provider=provider_name,
        retryable=False,
    )


@lru_cache(maxsize=1)
def build_copilot() -> Copilot:
    """Construct the full agent stack. Cached after first call.

    Providers (DESIGN.md §5):
    - prices + options : Alpaca (primary)
    - filings          : SEC EDGAR
    - analyst/earnings : Finnhub

    LLM for the thesis step is config-driven (LLM_THESIS_PROVIDER + LLM_THESIS_MODEL).
    """
    settings = get_settings()
    alpaca = AlpacaProvider()
    edgar = EdgarFilingsProvider()
    finnhub = FinnhubProvider()

    llm = _build_llm(settings.llm_thesis_provider)
    _log.info(
        "copilot_built",
        thesis_provider=settings.llm_thesis_provider,
        thesis_model=settings.llm_thesis_model,
    )

    registry = ToolRegistry(
        price_provider=alpaca,
        options_provider=alpaca,
        filings_provider=edgar,
        finnhub=finnhub,
    )
    return Copilot(llm=llm, registry=registry, model=settings.llm_thesis_model)
