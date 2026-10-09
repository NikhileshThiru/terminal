"""Wire providers + tool registry + LLM into a fully-formed Copilot.

The thesis-step LLM is the config-driven thesis router (app/llm/factory.py):
Gemini 2.5 Flash first, then other Gemini models (each a separate free daily
quota) when one is rate-limited or out of quota. Routes may be skipped if a
key isn't set; with no usable route the router build raises
`ProviderUnavailable(AUTH_MISSING)`, which the endpoint catches.
"""

from __future__ import annotations

from functools import lru_cache

from app.agent.copilot import Copilot
from app.agent.tools import ToolRegistry
from app.core.config import get_settings
from app.core.logging import get_logger
from app.data.registry import get_alpaca, get_edgar, get_finnhub
from app.llm.factory import get_router

_log = get_logger(__name__)


@lru_cache(maxsize=1)
def build_copilot() -> Copilot:
    """Construct the full agent stack. Cached after first call.

    Providers (DESIGN.md §5):
    - prices + options : Alpaca (primary)
    - filings          : SEC EDGAR
    - analyst/earnings : Finnhub

    LLM for the thesis step is the thesis router (LLM_THESIS_PROVIDER/MODEL
    + LLM_THESIS_FALLBACKS).
    """
    settings = get_settings()
    alpaca = get_alpaca()
    edgar = get_edgar()
    finnhub = get_finnhub()

    llm = get_router("thesis")
    _log.info(
        "copilot_built",
        thesis_routes=[r.label for r in llm.routes],
    )

    registry = ToolRegistry(
        price_provider=alpaca,
        options_provider=alpaca,
        filings_provider=edgar,
        finnhub=finnhub,
    )
    from app.eval.persistence import record_attempt

    return Copilot(
        llm=llm,
        registry=registry,
        model=settings.llm_thesis_model,
        attempt_sink=record_attempt,
    )
