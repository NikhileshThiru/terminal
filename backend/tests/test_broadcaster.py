"""AgentEventBroadcaster — fan-out + the idle keepalive the SSE feed relies on."""

from __future__ import annotations

import asyncio

import pytest

from app.agent.broadcaster import AgentEventBroadcaster
from app.agent.copilot import CopilotEvent


@pytest.mark.asyncio
async def test_idle_stream_yields_keepalive_then_real_event() -> None:
    """Regression: the heartbeat used to live in the consumer loop *after*
    an event arrived, so a quiet stream never produced one."""
    b = AgentEventBroadcaster()
    stream = b.stream(idle_seconds=0.05)

    first = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
    assert first is None  # keepalive with zero events published

    await b.publish(CopilotEvent(kind="thinking", payload={"text": "hi"}))
    second = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
    assert second is not None and second.kind == "thinking"
    await stream.aclose()
    assert not b._subscribers  # unsubscribed on close


@pytest.mark.asyncio
async def test_stream_without_idle_never_yields_none() -> None:
    b = AgentEventBroadcaster()
    stream = b.stream()
    nxt = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0.05)
    assert not nxt.done()
    await b.publish(CopilotEvent(kind="started", payload={}))
    evt = await asyncio.wait_for(nxt, timeout=1.0)
    assert evt is not None and evt.kind == "started"
    await stream.aclose()
