"""MCP server: every tool is read-only, hits only GET endpoints, and shapes
the API's responses into compact answers."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp_server import TerminalAPI, build_server

BASE = "http://terminal.test"


def _server() -> Any:
    return build_server(TerminalAPI(base_url=BASE))


def _payload(result: Any) -> Any:
    """Tool results arrive as MCP text content holding JSON."""
    content = result.content if hasattr(result, "content") else result[0]
    texts = [c.text for c in content]
    return [json.loads(t) for t in texts] if len(texts) > 1 else json.loads(texts[0])


@pytest.mark.asyncio
async def test_every_tool_is_annotated_read_only() -> None:
    tools = await _server().list_tools()
    assert {t.name for t in tools} == {
        "terminal_status",
        "list_theses",
        "eval_scorecard",
        "calibration",
        "portfolio",
        "recent_news",
        "upcoming_catalysts",
    }
    for t in tools:
        assert t.annotations is not None
        assert t.annotations.read_only_hint is True
        assert t.annotations.destructive_hint is False


@pytest.mark.asyncio
@respx.mock(base_url=BASE, assert_all_called=False)
async def test_tools_only_issue_get_requests(respx_mock: respx.MockRouter) -> None:
    """Whatever a client asks, the server can't mutate Terminal: no POSTs."""
    respx_mock.route(method__in=["POST", "PUT", "PATCH", "DELETE"]).mock(
        side_effect=AssertionError("MCP server must never write")
    )
    respx_mock.get("/autonomous/status").mock(
        return_value=httpx.Response(200, json={"state": "running", "recent_triage_decisions": []})
    )
    respx_mock.get("/llm/health").mock(return_value=httpx.Response(200, json={"steps": []}))
    respx_mock.get("/eval/summary").mock(return_value=httpx.Response(200, json={"buckets": []}))
    respx_mock.get("/eval/graduation").mock(return_value=httpx.Response(200, json={"ready": False}))
    respx_mock.get("/portfolio/accounts").mock(return_value=httpx.Response(200, json=[]))
    respx_mock.get("/portfolio/shadow-trades").mock(return_value=httpx.Response(200, json=[]))
    s = _server()
    for name in ("terminal_status", "eval_scorecard", "portfolio"):
        await s.call_tool(name, {})


@pytest.mark.asyncio
@respx.mock(base_url=BASE)
async def test_list_theses_shapes_rows_and_rejects_unknown_bucket(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get("/autonomous/theses").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 7,
                    "symbol": "NVDA",
                    "source_bucket": "reactive",
                    "direction": "long",
                    "confidence": 0.62,
                    "generated_at": "2026-10-09T14:00:00Z",
                    "suggested_contract": {
                        "occ_symbol": "NVDA261017C00190000",
                        "max_risk_usd": "410",
                    },
                    "llm_provider": "gemini",
                    "llm_model": "gemini-2.5-flash",
                    "reasoning": "x" * 2000,
                }
            ],
        )
    )
    s = _server()
    out = _payload(await s.call_tool("list_theses", {"bucket": "reactive", "limit": 500}))
    row = out[0] if isinstance(out, list) else out
    assert row["contract"] == "NVDA261017C00190000"
    assert row["model"] == "gemini:gemini-2.5-flash"
    assert len(row["reasoning"]) == 600
    assert route.calls.last.request.url.params["limit"] == "100"  # clamped

    with pytest.raises(Exception, match="bucket"):
        await s.call_tool("list_theses", {"bucket": "crypto"})


@pytest.mark.asyncio
@respx.mock(base_url=BASE)
async def test_recent_news_unwraps_ticker_rows(respx_mock: respx.MockRouter) -> None:
    respx_mock.get("/tickers/AAPL/news").mock(
        return_value=httpx.Response(
            200, json={"symbol": "AAPL", "rows": [{"headline": "h", "passed": False}]}
        )
    )
    out = _payload(await _server().call_tool("recent_news", {"symbol": "aapl"}))
    assert out == {"headline": "h", "passed": False} or out == [{"headline": "h", "passed": False}]


@pytest.mark.asyncio
async def test_unreachable_api_gives_actionable_error() -> None:
    api = TerminalAPI(base_url="http://127.0.0.1:9")  # nothing listens here
    with pytest.raises(ToolError, match="docker compose up"):
        await api.get("/health")
