"""Terminal as an MCP server — query the research terminal from an MCP client.

    TERMINAL_API_URL=http://localhost:8010 uv run python -m app.mcp_server

Read-only by construction: every tool is a GET against Terminal's own REST
API (the same endpoints the React frontend uses), and every tool is
annotated read-only / non-destructive. Nothing here can place a trade, start/stop the agent, or
spend LLM quota. It needs no database credentials — just the API URL.

Direction matters for the project's hard rule: an MCP client (e.g. Claude
Desktop) *reads from* Terminal. Terminal itself never calls an Anthropic
API and never consumes the owner's subscription.

Tools:
- terminal_status     — pipeline health: worker, ingestion, news WS, LLM routes
- list_theses         — recent theses, filterable by source bucket
- eval_scorecard      — per-bucket Brier/hit rate + the shadow->paper gate
- calibration         — stated confidence vs realized hit rate, per bucket
- portfolio           — both paper accounts + open shadow positions
- recent_news         — triaged news for a symbol (or the latest decisions)
- upcoming_catalysts  — the earnings calendar the agent pre-positions on
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
_BUCKETS = ("manual", "reactive", "catalyst")


def _api_base() -> str:
    return os.environ.get("TERMINAL_API_URL", "http://localhost:8000").rstrip("/")


class TerminalAPI:
    """Tiny async GET client over Terminal's REST API. Failures raise ToolError,
    which MCP passes to the client with its message (anything else would
    surface as an opaque "Error executing tool")."""

    def __init__(self, base_url: str | None = None, client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(base_url=base_url or _api_base(), timeout=15.0)

    async def get(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        try:
            resp = await self._client.get(path, params=clean)
        except httpx.HTTPError as e:
            raise ToolError(
                f"Terminal API unreachable at {self._client.base_url} ({type(e).__name__}). "
                "Is the stack running? (docker compose up -d)"
            ) from e
        if resp.status_code >= 400:
            raise ToolError(f"Terminal API {path} returned HTTP {resp.status_code}")
        return resp.json()


def build_server(api: TerminalAPI | None = None) -> MCPServer:
    client = api or TerminalAPI()
    server: MCPServer = MCPServer(
        name="terminal",
        title="Terminal — AI trading research (paper only)",
        instructions=(
            "Read-only view of a personal AI trading-research terminal. Paper trading "
            "only; data is free-tier and delayed. Theses are LLM-generated and scored "
            "forward; treat confidence as a claim to check against the eval scorecard, "
            "not as advice."
        ),
    )

    @server.tool(annotations=_READ_ONLY)
    async def terminal_status() -> dict[str, Any]:
        """Health of the always-on pipeline: agent worker state, ingestion
        service, news websocket, event counters, last error, and each LLM
        step's failover chain (which routes are parked on quota)."""
        status = await client.get("/autonomous/status")
        llm = await client.get("/llm/health")
        keep = (
            "state",
            "event_bus",
            "ingestion_mode",
            "ingestion_state",
            "news_stream_state",
            "events_consumed",
            "events_passed_triage",
            "events_stale",
            "events_skipped_quota",
            "events_skipped_thesis_cap",
            "theses_produced",
            "triage_failures",
            "thesis_failures",
            "queue_depth",
            "last_thesis_at",
            "last_error",
        )
        return {
            "worker": {k: status.get(k) for k in keep},
            "llm_steps": [
                {
                    "step": s["step"],
                    "status": s["status"],
                    "routes": [f"{r['provider']}:{r['model']} ({r['state']})" for r in s["routes"]],
                }
                for s in llm.get("steps", [])
            ],
        }

    @server.tool(annotations=_READ_ONLY)
    async def list_theses(bucket: str | None = None, limit: int = 15) -> list[dict[str, Any]]:
        """Most recent theses, newest first. `bucket` is one of manual
        (user-prompted), reactive (news-driven) or catalyst (scheduled
        earnings). Each has direction, confidence, the suggested option
        contract and the model that wrote it."""
        if bucket is not None and bucket not in _BUCKETS:
            raise ToolError(f"bucket must be one of {_BUCKETS}")
        rows = await client.get(
            "/autonomous/theses", limit=max(1, min(limit, 100)), source_bucket=bucket
        )
        return [
            {
                "id": t.get("id"),
                "symbol": t.get("symbol"),
                "bucket": t.get("source_bucket"),
                "direction": t.get("direction"),
                "confidence": t.get("confidence"),
                "window_days": t.get("prediction_window_days"),
                "generated_at": t.get("generated_at"),
                "contract": (t.get("suggested_contract") or {}).get("occ_symbol"),
                "max_risk_usd": (t.get("suggested_contract") or {}).get("max_risk_usd"),
                "model": f"{t.get('llm_provider')}:{t.get('llm_model')}",
                "reasoning": (t.get("reasoning") or "")[:600],
            }
            for t in rows
        ]

    @server.tool(annotations=_READ_ONLY)
    async def eval_scorecard() -> dict[str, Any]:
        """Is the AI any good? Per-bucket Brier score (0.25 = coin-flip
        baseline, lower is better) and hit rate on resolved theses, plus the
        shadow->paper graduation gate (50 theses, 95% grounding, Brier <= 0.25)."""
        summary = await client.get("/eval/summary")
        graduation = await client.get("/eval/graduation")
        return {"buckets": summary.get("buckets", []), "graduation": graduation}

    @server.tool(annotations=_READ_ONLY)
    async def calibration(bucket: str = "reactive") -> dict[str, Any]:
        """Calibration curve for one bucket: for each confidence band, how
        often theses in that band were actually right. A calibrated model's
        points sit on the diagonal."""
        if bucket not in _BUCKETS:
            raise ToolError(f"bucket must be one of {_BUCKETS}")
        data: dict[str, Any] = await client.get("/eval/calibration", bucket=bucket)
        return data

    @server.tool(annotations=_READ_ONLY)
    async def portfolio() -> dict[str, Any]:
        """Both paper accounts (conservative vs aggressive risk gates) and
        every open shadow position with its latest mark. Paper only."""
        accounts = await client.get("/portfolio/accounts")
        open_positions = await client.get("/portfolio/shadow-trades", status="open", limit=200)
        return {
            "accounts": [
                {
                    k: a.get(k)
                    for k in (
                        "kind",
                        "equity_usd",
                        "min_confidence",
                        "max_trade_cost_usd",
                        "open_shadow_positions",
                        "shadow_trades_total",
                    )
                }
                for a in accounts
            ],
            "open_positions": [
                {
                    k: p.get(k)
                    for k in (
                        "account_kind",
                        "occ_symbol",
                        "contracts",
                        "total_cost_usd",
                        "unrealized_pnl_usd",
                        "opened_at",
                        "expiration",
                    )
                }
                for p in open_positions
            ],
        }

    @server.tool(annotations=_READ_ONLY)
    async def recent_news(symbol: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """Triaged news. With a symbol: that ticker's recent stories and
        whether the triage gate passed them. Without: the latest decisions
        across the whole market."""
        if symbol:
            data = await client.get(f"/tickers/{symbol.upper()}/news", limit=max(1, min(limit, 50)))
            rows: list[dict[str, Any]] = data.get("rows", [])
            return rows
        status = await client.get("/autonomous/status")
        decisions: list[dict[str, Any]] = status.get("recent_triage_decisions", [])
        return decisions[-limit:][::-1]

    @server.tool(annotations=_READ_ONLY)
    async def upcoming_catalysts(within_days: int = 14) -> list[dict[str, Any]]:
        """Scheduled earnings the agent will pre-position on (thesis fires
        inside the lead window, during market hours)."""
        rows: list[dict[str, Any]] = await client.get(
            "/autonomous/catalysts", within_days=max(1, min(within_days, 60)), limit=50
        )
        return rows

    return server


def main() -> None:  # pragma: no cover — process entrypoint
    build_server().run("stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
