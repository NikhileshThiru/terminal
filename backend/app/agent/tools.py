"""LLM-callable tools for the manual copilot (DESIGN.md §4 step 4).

Six tools, each delegating to a data provider, each returning a ToolResult
with grounding metadata so the Phase-4 grounding check can verify every
number the LLM cites.

Tools:
- get_quote          — latest price for a symbol
- get_ohlc           — daily OHLC bars over the last N days
- get_options_chain  — option chain for a symbol (nearest expiration by default)
- get_recent_filings — SEC 8-K/10-Q/10-K/Form-4 filings
- get_analyst_ratings — Finnhub consensus snapshots
- get_earnings_context — next earnings event + recent surprise history
"""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.data.finnhub import FinnhubProvider
from app.data.interfaces import FilingsProvider, OptionsProvider, PriceProvider
from app.data.types import (
    Filing,
    FilingType,
    ProviderUnavailable,
)
from app.llm.interface import AgentToolDef

_log = get_logger(__name__)


# === Tool input schemas (LLM sees these) ===


class GetQuoteInput(BaseModel):
    symbol: str = Field(description="Equity ticker, e.g. AAPL.")


class GetOhlcInput(BaseModel):
    symbol: str = Field(description="Equity ticker, e.g. AAPL.")
    days_back: int = Field(default=30, ge=1, le=365, description="How many trailing days of bars.")


class GetOptionsChainInput(BaseModel):
    symbol: str = Field(description="Equity ticker, e.g. AAPL.")
    expiration_date: str | None = Field(
        default=None,
        description=(
            "Optional expiration date in YYYY-MM-DD. If omitted, returns the "
            "nearest expiration after today."
        ),
    )


class GetRecentFilingsInput(BaseModel):
    symbol: str = Field(description="Equity ticker, e.g. AAPL.")
    filing_types: list[str] | None = Field(
        default=None,
        description="Optional list of SEC forms to filter to (e.g. ['8-K', '10-Q']).",
    )
    limit: int = Field(default=10, ge=1, le=50)


class GetAnalystRatingsInput(BaseModel):
    symbol: str = Field(description="Equity ticker, e.g. AAPL.")


class GetEarningsContextInput(BaseModel):
    symbol: str = Field(description="Equity ticker, e.g. AAPL.")


# === Result envelope ===


class ToolResult(BaseModel):
    """One tool's result. Carries grounding metadata so cited numbers are traceable."""

    tool_name: str
    arguments: dict[str, Any]
    success: bool
    data: Any | None = None
    error: str | None = None
    provider: str
    fetched_at: datetime


# === Tool descriptor ===


@dataclass
class Tool:
    name: str
    description: str
    input_schema: type[BaseModel]
    handler: Callable[[BaseModel], Awaitable[Any]]
    provider_name: str  # for ToolResult.provider

    def to_agent_def(self) -> AgentToolDef:
        """Render this tool as a provider-agnostic AgentToolDef (JSON schema)."""
        schema = self.input_schema.model_json_schema()
        cleaned = _strip_pydantic_schema_keys(schema)
        return AgentToolDef(
            name=self.name,
            description=self.description,
            parameters=cleaned,
        )


def _strip_pydantic_schema_keys(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Pydantic-generated JSON schema for picky LLM providers.

    - Drops pydantic-specific keys (`title`, `$defs`, `additionalProperties`).
    - Simplifies `anyOf` unions: Pydantic emits Decimal as
      `anyOf: [number, string-with-regex-pattern]`, and Optional[T] as
      `anyOf: [T, null]`. Groq's schema compiler rejects these. We collapse to
      the most useful single type.
    """
    cleaned = _simplify_anyof(schema)
    result: dict[str, Any] = _strip_keys(cleaned)
    return result


def _simplify_anyof(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return schema
    if "anyOf" in schema:
        variants = [v for v in schema["anyOf"] if isinstance(v, dict)]
        non_null = [v for v in variants if v.get("type") != "null"]
        # Prefer numeric variants over string-pattern variants (Decimal case).
        numeric = [v for v in non_null if v.get("type") in ("number", "integer")]
        chosen = numeric[0] if numeric else (non_null[0] if non_null else {})
        chosen = _simplify_anyof(chosen)
        # Preserve outer description / default if not already on chosen.
        for k in ("description", "default"):
            if k in schema and k not in chosen:
                chosen[k] = schema[k]
        return chosen
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if isinstance(v, dict):
            out[k] = _simplify_anyof(v)
        elif isinstance(v, list):
            out[k] = [_simplify_anyof(item) if isinstance(item, dict) else item for item in v]
        else:
            out[k] = v
    return out


def _strip_keys(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k in ("title", "$defs", "additionalProperties"):
            continue
        if isinstance(v, dict):
            out[k] = _strip_keys(v)
        elif isinstance(v, list):
            out[k] = [_strip_keys(item) if isinstance(item, dict) else item for item in v]
        else:
            out[k] = v
    return out


# === The registry ===


class ToolRegistry:
    """Holds tool definitions + dispatches LLM tool calls to handlers.

    Construct once per agent session with the relevant providers wired up.
    """

    def __init__(
        self,
        *,
        price_provider: PriceProvider,
        options_provider: OptionsProvider,
        filings_provider: FilingsProvider,
        finnhub: FinnhubProvider,
    ) -> None:
        self._price = price_provider
        self._options = options_provider
        self._filings = filings_provider
        self._finnhub = finnhub
        self._tools: dict[str, Tool] = {}
        self._register_builtins()

    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def agent_defs(self) -> list[AgentToolDef]:
        """All tools as provider-agnostic AgentToolDef objects."""
        return [t.to_agent_def() for t in self._tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        fetched_at = datetime.now(UTC)
        if tool is None:
            return ToolResult(
                tool_name=name,
                arguments=arguments,
                success=False,
                error=f"unknown tool: {name!r}",
                provider="registry",
                fetched_at=fetched_at,
            )

        try:
            parsed = tool.input_schema.model_validate(arguments)
        except Exception as e:
            return ToolResult(
                tool_name=name,
                arguments=arguments,
                success=False,
                error=f"invalid arguments: {e}",
                provider=tool.provider_name,
                fetched_at=fetched_at,
            )

        try:
            result = await tool.handler(parsed)
            return ToolResult(
                tool_name=name,
                arguments=arguments,
                success=True,
                data=result,
                provider=tool.provider_name,
                fetched_at=fetched_at,
            )
        except ProviderUnavailable as e:
            _log.warning(
                "tool_provider_unavailable",
                tool=name,
                reason=e.reason.value,
                provider=e.provider,
                message=e.message,
            )
            return ToolResult(
                tool_name=name,
                arguments=arguments,
                success=False,
                error=f"{e.reason.value}: {e.message}",
                provider=tool.provider_name,
                fetched_at=fetched_at,
            )
        except Exception as e:
            _log.exception("tool_unexpected_error", tool=name, error=str(e))
            return ToolResult(
                tool_name=name,
                arguments=arguments,
                success=False,
                error=f"{type(e).__name__}: {e}",
                provider=tool.provider_name,
                fetched_at=fetched_at,
            )

    def _register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def _register_builtins(self) -> None:
        self._register(
            Tool(
                name="get_quote",
                description=(
                    "Get the latest bid/ask/last price for an equity. Returns price in USD."
                ),
                input_schema=GetQuoteInput,
                handler=self._h_get_quote,
                provider_name=self._price.name,
            )
        )
        self._register(
            Tool(
                name="get_ohlc",
                description=(
                    "Get daily OHLC (open/high/low/close/volume) bars for the trailing "
                    "N days. Use this for recent price action context."
                ),
                input_schema=GetOhlcInput,
                handler=self._h_get_ohlc,
                provider_name=self._price.name,
            )
        )
        self._register(
            Tool(
                name="get_options_chain",
                description=(
                    "Get the option chain for an equity at a given expiration. If "
                    "expiration_date is omitted, returns the nearest expiration. The "
                    "response includes calls and puts with bid/ask. Use this to find "
                    "specific contracts to suggest in a thesis."
                ),
                input_schema=GetOptionsChainInput,
                handler=self._h_get_options_chain,
                provider_name=self._options.name,
            )
        )
        self._register(
            Tool(
                name="get_recent_filings",
                description=(
                    "Get recent SEC filings (8-K, 10-Q, 10-K, Form 4) for an equity. "
                    "Use this for material events and corporate actions."
                ),
                input_schema=GetRecentFilingsInput,
                handler=self._h_get_recent_filings,
                provider_name=self._filings.name,
            )
        )
        self._register(
            Tool(
                name="get_analyst_ratings",
                description=(
                    "Get the most recent analyst-rating consensus snapshots for an "
                    "equity (counts of strong-buy / buy / hold / sell / strong-sell)."
                ),
                input_schema=GetAnalystRatingsInput,
                handler=self._h_get_analyst_ratings,
                provider_name=self._finnhub.name,
            )
        )
        self._register(
            Tool(
                name="get_earnings_context",
                description=(
                    "Get the next scheduled earnings event plus the last few earnings "
                    "surprises for an equity. Use this when reasoning about earnings "
                    "catalysts."
                ),
                input_schema=GetEarningsContextInput,
                handler=self._h_get_earnings_context,
                provider_name=self._finnhub.name,
            )
        )

    # === Handlers ===

    async def _h_get_quote(self, inp: BaseModel) -> dict[str, Any]:
        assert isinstance(inp, GetQuoteInput)
        q = await self._price.get_latest_quote(inp.symbol)
        return q.model_dump(mode="json")

    async def _h_get_ohlc(self, inp: BaseModel) -> dict[str, Any]:
        assert isinstance(inp, GetOhlcInput)
        end = datetime.now(UTC) - timedelta(days=1)  # respect 15-min delay
        start = end - timedelta(days=inp.days_back)
        bars = await self._price.get_ohlc(inp.symbol, start=start, end=end)
        return {
            "symbol": inp.symbol.upper(),
            "bars": [b.model_dump(mode="json") for b in bars],
        }

    async def _h_get_options_chain(self, inp: BaseModel) -> dict[str, Any]:
        assert isinstance(inp, GetOptionsChainInput)
        if inp.expiration_date:
            expiration = date.fromisoformat(inp.expiration_date)
        else:
            expirations = await self._options.get_expirations(inp.symbol)
            today = date.today()
            future = [e for e in expirations if e >= today]
            if not future:
                return {"symbol": inp.symbol.upper(), "expiration": None, "contracts": []}
            expiration = future[0]
        chain = await self._options.get_chain(inp.symbol, expiration)
        return {
            "symbol": inp.symbol.upper(),
            "expiration": expiration.isoformat(),
            "contracts": [c.model_dump(mode="json") for c in chain],
        }

    async def _h_get_recent_filings(self, inp: BaseModel) -> dict[str, Any]:
        assert isinstance(inp, GetRecentFilingsInput)
        ft_filter: list[FilingType] | None = None
        if inp.filing_types:
            ft_filter = []
            for t in inp.filing_types:
                with contextlib.suppress(ValueError):
                    ft_filter.append(FilingType(t.upper()))
        filings = await self._filings.get_recent_filings(
            inp.symbol, filing_types=ft_filter, limit=inp.limit
        )
        return {
            "symbol": inp.symbol.upper(),
            "filings": [_filing_to_dict(f) for f in filings],
        }

    async def _h_get_analyst_ratings(self, inp: BaseModel) -> dict[str, Any]:
        assert isinstance(inp, GetAnalystRatingsInput)
        ratings = await self._finnhub.get_analyst_ratings(inp.symbol)
        return {
            "symbol": inp.symbol.upper(),
            "snapshots": [r.model_dump(mode="json") for r in ratings],
        }

    async def _h_get_earnings_context(self, inp: BaseModel) -> dict[str, Any]:
        assert isinstance(inp, GetEarningsContextInput)
        today = date.today()
        calendar = await self._finnhub.get_earnings_calendar(
            symbol=inp.symbol,
            from_date=today,
            to_date=today + timedelta(days=180),
        )
        surprises = await self._finnhub.get_earnings_surprises(inp.symbol, limit=6)
        return {
            "symbol": inp.symbol.upper(),
            "upcoming": [e.model_dump(mode="json") for e in calendar],
            "recent_surprises": [s.model_dump(mode="json") for s in surprises],
        }


def _filing_to_dict(f: Filing) -> dict[str, Any]:
    d = f.model_dump(mode="json")
    d["filing_type"] = f.filing_type.value
    return d


# Lightweight runtime check that all handlers are async (helps catch wiring mistakes)
def _assert_async_handlers(registry: ToolRegistry) -> None:
    for t in registry.tools():
        assert inspect.iscoroutinefunction(t.handler), f"{t.name} handler is not async"
