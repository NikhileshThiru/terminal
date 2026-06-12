"""POST /copilot/thesis — the manual copilot entry point (DESIGN.md §4).

Two flavors:
- `/copilot/thesis` — blocking. Returns the final Thesis JSON.
- `/copilot/thesis/stream` — SSE. Streams CopilotEvent records as the
  agent loop runs (thinking, tool_call, tool_result, grounding, done).
  Used by the AgentReasoningPane so the demo shows the model *thinking*
  instead of a 20-second blank wait.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.build import build_copilot
from app.agent.copilot import Copilot, CopilotError, CopilotEvent
from app.agent.schemas import Thesis
from app.core.logging import get_logger
from app.data.types import ProviderUnavailable
from app.eval.persistence import write_thesis

_log = get_logger(__name__)

router = APIRouter(prefix="/copilot", tags=["copilot"])


class ThesisRequest(BaseModel):
    user_thesis: str = Field(min_length=5, max_length=2000)
    risk_budget_usd: float | None = Field(default=None, gt=0)


def get_copilot() -> Copilot:
    """FastAPI dependency. Tests can override this via app.dependency_overrides."""
    return build_copilot()


async def _persist_and_consider(thesis: Thesis) -> int | None:
    thesis_id: int | None = None
    try:
        thesis_id = await write_thesis(thesis)
        _log.info("thesis_id_for_run", thesis_id=thesis_id)
    except Exception:
        _log.exception("thesis_persist_failed")
    if thesis_id is not None:
        try:
            from app.portfolio.engine import get_paper_engine

            await get_paper_engine().consider_thesis(thesis_id, thesis)
        except Exception:
            _log.exception("paper_engine_consider_failed", thesis_id=thesis_id)
    return thesis_id


@router.post("/thesis", response_model=Thesis)
async def generate_thesis(
    req: ThesisRequest,
    copilot: Copilot = Depends(get_copilot),
) -> Thesis:
    try:
        run = await copilot.generate(
            req.user_thesis,
            risk_budget_usd=req.risk_budget_usd,
        )
    except ProviderUnavailable as e:
        raise HTTPException(
            status_code=503,
            detail={"reason": e.reason.value, "provider": e.provider, "message": str(e)},
        ) from e
    except CopilotError as e:
        raise HTTPException(status_code=502, detail=f"copilot: {e}") from e

    await _persist_and_consider(run.thesis)
    return run.thesis


def _sse_format(evt: CopilotEvent) -> str:
    """Encode one SSE record. Each kind is a separate `event:` line, with
    the JSON-encoded payload on `data:`. Trailing blank line is required
    by the SSE spec."""
    return f"event: {evt.kind}\ndata: {json.dumps(evt.payload, default=str)}\n\n"


@router.post("/thesis/stream")
async def generate_thesis_stream(
    req: ThesisRequest,
    copilot: Copilot = Depends(get_copilot),
) -> StreamingResponse:
    """SSE stream of the copilot run. Frontend uses fetch+ReadableStream
    (EventSource doesn't support POST). Closes after the final `done` or
    `error` event."""
    queue: asyncio.Queue[CopilotEvent | None] = asyncio.Queue()

    async def sink(evt: CopilotEvent) -> None:
        await queue.put(evt)

    async def runner() -> None:
        try:
            run = await copilot.generate(
                req.user_thesis,
                risk_budget_usd=req.risk_budget_usd,
                event_sink=sink,
            )
            await _persist_and_consider(run.thesis)
            await queue.put(
                CopilotEvent(
                    kind="done",
                    payload={"thesis": run.thesis.model_dump(mode="json")},
                )
            )
        except ProviderUnavailable as e:
            await queue.put(
                CopilotEvent(
                    kind="error",
                    payload={
                        "kind": "provider_unavailable",
                        "reason": e.reason.value,
                        "provider": e.provider,
                        "message": str(e),
                    },
                )
            )
        except CopilotError as e:
            await queue.put(CopilotEvent(kind="error", payload={"message": str(e)}))
        except Exception as e:  # pragma: no cover — guard against truly unknown errors
            _log.exception("copilot_stream_unexpected_error")
            await queue.put(CopilotEvent(kind="error", payload={"message": f"unexpected: {e}"}))
        finally:
            await queue.put(None)  # sentinel

    runner_task = asyncio.create_task(runner())

    async def event_gen() -> AsyncIterator[str]:
        try:
            while True:
                evt = await queue.get()
                if evt is None:
                    break
                yield _sse_format(evt)
        finally:
            if not runner_task.done():
                runner_task.cancel()

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # so reverse proxies don't buffer the stream
    }
    return StreamingResponse(event_gen(), media_type="text/event-stream", headers=headers)
