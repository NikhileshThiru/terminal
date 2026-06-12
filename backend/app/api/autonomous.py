"""POST /autonomous/{start,stop} + GET /autonomous/{status,theses,catalysts}.

Toggles + observability for the always-on discovery pipeline.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from app.core.logging import get_logger
from app.data.types import ProviderUnavailable
from app.discovery.models import CatalystEvent
from app.discovery.worker import WorkerState, WorkerStatus, get_worker
from app.eval.models import Thesis as ThesisRow
from app.eval.persistence import get_session_factory

_log = get_logger(__name__)

router = APIRouter(prefix="/autonomous", tags=["autonomous"])


class WorkerStatusResponse(BaseModel):
    state: str
    started_at: datetime | None
    stopped_at: datetime | None
    watchlist: list[str]
    poll_interval_seconds: float
    triage_provider: str
    triage_model: str
    thesis_provider: str
    thesis_model: str
    polls_completed: int
    events_published: int
    events_consumed: int
    events_passed_triage: int
    theses_produced: int
    triage_failures: int
    thesis_failures: int
    persistence_failures: int
    queue_depth: int
    last_event_at: datetime | None
    last_thesis_at: datetime | None
    last_poll_at: datetime | None
    last_error: str | None
    recent_triage_decisions: list[dict[str, Any]] = Field(default_factory=list)
    # Reconciliation jobs (Phase 7+).
    mtm_ticks_completed: int = 0
    mtm_marks_written: int = 0
    resolver_ticks_completed: int = 0
    resolver_outcomes_written: int = 0
    last_mtm_at: datetime | None = None
    last_resolver_at: datetime | None = None
    # Alpaca news WS (Step 4).
    news_stream_connected: bool = False
    news_stream_connects: int = 0
    news_stream_messages: int = 0
    news_stream_events_published: int = 0
    news_stream_duplicates_dropped: int = 0
    last_news_message_at: datetime | None = None
    # Catalyst calendar (Step 5).
    catalyst_fetcher_runs: int = 0
    catalyst_events_upserted: int = 0
    catalyst_scheduler_runs: int = 0
    catalyst_theses_triggered: int = 0
    last_catalyst_fetch_at: datetime | None = None
    last_catalyst_trigger_at: datetime | None = None


def _to_response(s: WorkerStatus) -> WorkerStatusResponse:
    return WorkerStatusResponse(
        state=s.state.value if isinstance(s.state, WorkerState) else str(s.state),
        started_at=s.started_at,
        stopped_at=s.stopped_at,
        watchlist=s.watchlist,
        poll_interval_seconds=s.poll_interval_seconds,
        triage_provider=s.triage_provider,
        triage_model=s.triage_model,
        thesis_provider=s.thesis_provider,
        thesis_model=s.thesis_model,
        polls_completed=s.polls_completed,
        events_published=s.events_published,
        events_consumed=s.events_consumed,
        events_passed_triage=s.events_passed_triage,
        theses_produced=s.theses_produced,
        triage_failures=s.triage_failures,
        thesis_failures=s.thesis_failures,
        persistence_failures=s.persistence_failures,
        queue_depth=s.queue_depth,
        last_event_at=s.last_event_at,
        last_thesis_at=s.last_thesis_at,
        last_poll_at=s.last_poll_at,
        last_error=s.last_error,
        recent_triage_decisions=s.recent_triage_decisions,
        mtm_ticks_completed=s.mtm_ticks_completed,
        mtm_marks_written=s.mtm_marks_written,
        resolver_ticks_completed=s.resolver_ticks_completed,
        resolver_outcomes_written=s.resolver_outcomes_written,
        last_mtm_at=s.last_mtm_at,
        last_resolver_at=s.last_resolver_at,
        news_stream_connected=s.news_stream_connected,
        news_stream_connects=s.news_stream_connects,
        news_stream_messages=s.news_stream_messages,
        news_stream_events_published=s.news_stream_events_published,
        news_stream_duplicates_dropped=s.news_stream_duplicates_dropped,
        last_news_message_at=s.last_news_message_at,
        catalyst_fetcher_runs=s.catalyst_fetcher_runs,
        catalyst_events_upserted=s.catalyst_events_upserted,
        catalyst_scheduler_runs=s.catalyst_scheduler_runs,
        catalyst_theses_triggered=s.catalyst_theses_triggered,
        last_catalyst_fetch_at=s.last_catalyst_fetch_at,
        last_catalyst_trigger_at=s.last_catalyst_trigger_at,
    )


@router.post("/start", response_model=WorkerStatusResponse)
async def start() -> WorkerStatusResponse:
    worker = get_worker()
    try:
        await worker.start()
    except ProviderUnavailable as e:
        raise HTTPException(
            status_code=503,
            detail={"reason": e.reason.value, "provider": e.provider, "message": str(e)},
        ) from e
    return _to_response(worker.status())


@router.post("/stop", response_model=WorkerStatusResponse)
async def stop() -> WorkerStatusResponse:
    worker = get_worker()
    await worker.stop()
    return _to_response(worker.status())


async def _hydrate_recent_decisions_from_db(
    response: WorkerStatusResponse, limit: int = 20
) -> WorkerStatusResponse:
    """If the in-memory triage buffer is small (post-restart), top it up
    from the persistent triage_decisions table so the News pane always
    shows a real feed even immediately after the worker restarts."""
    if len(response.recent_triage_decisions) >= limit:
        return response
    try:
        from app.discovery.models import TriageDecisionRow

        factory = get_session_factory()
        async with factory() as session:
            stmt = (
                select(TriageDecisionRow).order_by(desc(TriageDecisionRow.decided_at)).limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()
        seen_ids = {d.get("event_id") for d in response.recent_triage_decisions}
        merged = list(response.recent_triage_decisions)
        # Append DB rows that aren't already in the in-memory buffer.
        for r in rows:
            if r.event_id in seen_ids:
                continue
            merged.append(
                {
                    "event_id": r.event_id,
                    "symbol": r.symbol,
                    "headline": r.headline,
                    "body_excerpt": r.body_excerpt,
                    "url": r.url,
                    "passed": r.passed,
                    "reason": r.reason,
                    "confidence": r.confidence,
                    "at": r.decided_at.isoformat(),
                    "source": r.source,
                    "kind": r.kind,
                }
            )
        # Newest-first by decided_at — front end can reverse if it likes.
        merged.sort(key=lambda d: str(d.get("at") or ""), reverse=True)
        response.recent_triage_decisions = merged[:limit]
    except Exception:
        _log.exception("triage_history_hydrate_failed")
    return response


@router.get("/status", response_model=WorkerStatusResponse)
async def status() -> WorkerStatusResponse:
    resp = _to_response(get_worker().status())
    return await _hydrate_recent_decisions_from_db(resp)


class InjectEventRequest(BaseModel):
    """Synthetic discovery-event payload. Used by the demo + tests to fire
    a fake earnings beat / 8-K without waiting for a real one to arrive."""

    symbol: str = Field(min_length=1, max_length=10)
    headline: str = Field(min_length=5, max_length=300)
    kind: str = Field(default="news", pattern="^(filing|news|scan)$")
    body: str | None = Field(default=None, max_length=2000)


class InjectEventResponse(BaseModel):
    event_id: str
    accepted: bool


@router.post("/inject", response_model=InjectEventResponse)
async def inject_event(req: InjectEventRequest) -> InjectEventResponse:
    """Push a synthetic event onto the discovery bus. Worker must be
    running. Useful for the demo (fire an earnings beat → watch reactive
    thesis flow through the funnel in real time) and for seeding the eval
    harness during development."""
    from uuid import uuid4

    from app.discovery.types import DiscoveryEvent

    worker = get_worker()
    if not worker.is_running:
        raise HTTPException(
            status_code=409,
            detail="autonomous worker is not running; POST /autonomous/start first",
        )
    event = DiscoveryEvent(
        id=f"synthetic-{uuid4().hex[:12]}",
        source="rss",  # closest to "synthetic" — keeps the existing source enum honest
        kind=req.kind,
        symbols=[req.symbol.upper()],
        headline=req.headline,
        body=req.body,
        url=None,
        published_at=datetime.now(UTC),
        payload={"synthetic": True},
    )
    try:
        await worker.inject(event)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return InjectEventResponse(event_id=event.id, accepted=True)


class RecentThesis(BaseModel):
    id: int
    symbol: str
    direction: str
    confidence: float
    source_bucket: str
    generated_at: datetime
    grounding_check_passed: bool
    reasoning: str
    suggested_contract: dict[str, Any] | None
    llm_provider: str
    llm_model: str
    funnel_latency_ms: int | None
    correlation_id: str


class UpcomingCatalyst(BaseModel):
    """One row of /autonomous/catalysts."""

    id: int
    symbol: str
    event_type: str
    event_date: date
    event_hour: str | None
    estimated_eps: Decimal | None
    state: str
    thesis_id: int | None
    days_until: int = Field(description="Days from today to event_date. Can be negative if past.")


@router.get("/catalysts", response_model=list[UpcomingCatalyst])
async def upcoming_catalysts(
    within_days: int = Query(default=14, ge=1, le=180),
    limit: int = Query(default=50, ge=1, le=200),
    state: str | None = Query(default=None, description="Filter: scheduled | triggered | expired"),
) -> list[UpcomingCatalyst]:
    """Catalyst calendar — events within the window, ordered by date."""
    from datetime import UTC as _UTC
    from datetime import timedelta as _td

    today = datetime.now(_UTC).date()
    end = today + _td(days=within_days)
    factory = get_session_factory()
    async with factory() as session:
        stmt = (
            select(CatalystEvent)
            .where(CatalystEvent.event_date >= today)
            .where(CatalystEvent.event_date <= end)
            .order_by(CatalystEvent.event_date)
            .limit(limit)
        )
        if state:
            stmt = stmt.where(CatalystEvent.state == state)
        rows = (await session.execute(stmt)).scalars().all()
    return [
        UpcomingCatalyst(
            id=r.id,
            symbol=r.symbol,
            event_type=r.event_type,
            event_date=r.event_date,
            event_hour=r.event_hour,
            estimated_eps=r.estimated_eps,
            state=r.state,
            thesis_id=r.thesis_id,
            days_until=(r.event_date - today).days,
        )
        for r in rows
    ]


@router.get("/theses", response_model=list[RecentThesis])
async def recent_theses(
    limit: int = Query(default=10, ge=1, le=100),
    source_bucket: str | None = Query(
        default=None,
        description="Filter to one bucket (manual | reactive | catalyst). Default: all.",
    ),
    include_pre_strictness: bool = Query(
        default=False,
        description=(
            "Include theses produced before the Phase 4.5 strictness fixes. "
            "Excluded by default so dashboards reflect current orchestrator behavior."
        ),
    ),
) -> list[RecentThesis]:
    """Recent theses across all buckets (the Live Theses feed). Rows carry
    their source_bucket so the UI can badge them; callers that want a single
    bucket pass ?source_bucket=."""
    factory = get_session_factory()
    async with factory() as session:
        stmt = select(ThesisRow).order_by(desc(ThesisRow.generated_at)).limit(limit)
        if source_bucket:
            stmt = stmt.where(ThesisRow.source_bucket == source_bucket)
        if not include_pre_strictness:
            stmt = stmt.where(ThesisRow.pre_strictness.is_(False))
        rows = (await session.execute(stmt)).scalars().all()
    return [
        RecentThesis(
            id=r.id,
            symbol=r.symbol,
            direction=r.direction,
            confidence=r.confidence,
            source_bucket=r.source_bucket,
            generated_at=r.generated_at,
            grounding_check_passed=r.grounding_check_passed,
            reasoning=r.reasoning,
            suggested_contract=r.suggested_contract,
            llm_provider=r.llm_provider,
            llm_model=r.llm_model,
            funnel_latency_ms=r.funnel_latency_ms,
            correlation_id=r.correlation_id,
        )
        for r in rows
    ]
