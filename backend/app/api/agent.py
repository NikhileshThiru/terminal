"""/agent — agent-activity feeds.

Currently just `/agent/events/stream`, the global SSE feed of every
CopilotEvent from every run (manual + reactive + catalyst). Used by the
Dashboard's Agent Reasoning pane so the same component shows whatever
agent is running, not just per-request manual copilot.
"""

from __future__ import annotations

import asyncio
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


@router.get("/events/stream")
async def stream_agent_events() -> StreamingResponse:
    """Global SSE stream of every agent event. Subscribe once on app boot;
    stays open for the life of the client. The server pushes a periodic
    heartbeat so reverse proxies don't time out the connection during
    quiet windows."""
    broadcaster = get_agent_broadcaster()

    async def gen() -> AsyncIterator[str]:
        # Hello so the frontend can render an empty "connected" state
        # immediately, before the first real event lands.
        yield f"event: hello\ndata: {json.dumps({})}\n\n"
        last_send = asyncio.get_event_loop().time()
        async for evt in broadcaster.stream():
            yield _sse(evt)
            last_send = asyncio.get_event_loop().time()
            # If we'd been idle for >25s with no events, emit a tick. In
            # practice the broadcaster will be active, but during quiet
            # market hours we still want the connection kept alive.
            now = asyncio.get_event_loop().time()
            if now - last_send > 25:
                yield f"event: tick\ndata: {json.dumps({})}\n\n"
                last_send = now

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)
