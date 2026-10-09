"""/agent — agent-activity feeds.

Currently just `/agent/events/stream`, the global SSE feed of every
CopilotEvent from every run (manual + reactive + catalyst). Used by the
Dashboard's Agent Reasoning pane so the same component shows whatever
agent is running, not just per-request manual copilot.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.agent.broadcaster import get_agent_broadcaster
from app.agent.copilot import CopilotEvent
from app.core.logging import get_logger

_log = get_logger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])


def _sse(evt: CopilotEvent) -> str:
    return f"event: {evt.kind}\ndata: {json.dumps(evt.payload, default=str)}\n\n"


KEEPALIVE_SECONDS = 25.0


@router.get("/events/stream")
async def stream_agent_events() -> StreamingResponse:
    """Global SSE stream of every agent event. Subscribe once on app boot;
    stays open for the life of the client. After KEEPALIVE_SECONDS with no
    events the server sends a `tick` so reverse proxies (Cloudflare Tunnel
    idles out at ~100s) don't drop the connection during quiet windows."""
    broadcaster = get_agent_broadcaster()

    async def gen() -> AsyncIterator[str]:
        # Hello so the frontend can render an empty "connected" state
        # immediately, before the first real event lands.
        yield f"event: hello\ndata: {json.dumps({})}\n\n"
        async for evt in broadcaster.stream(idle_seconds=KEEPALIVE_SECONDS):
            if evt is None:
                yield f"event: tick\ndata: {json.dumps({})}\n\n"
            else:
                yield _sse(evt)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)
