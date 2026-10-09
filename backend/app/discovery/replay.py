"""Replay mode — re-run triage over the stored event log, offline.

    python -m app.discovery.replay --limit 80 --balanced
    python -m app.discovery.replay --source redis --since 2026-10-01 --out replay.json
    python -m app.discovery.replay --route gemini:gemini-2.5-flash-lite --limit 40

Sources:
- `db` (default): the `triage_decisions` table — every event the worker ever
  triaged, with the decision it made at the time. The durable log.
- `redis`: the retained Redis stream (what the bus actually carried), joined
  to the DB for the original decision where one exists.

For each event it runs the *current* triage prompt + route (or `--route`)
and reports agreement with the original decision: overall, and the flips in
each direction. That answers the questions that come up in practice:
"did swapping the triage model change what passes?" (Groq retired
llama-3.3-70b in 2026; this is how the replacement was checked), and "did
my prompt edit make the gate stricter or looser?"

Deliberately triage-only and read-only:
- Never writes theses, triage decisions, or anything the eval reads. Theses
  on old events would be a backtest — contaminated, and the research tools
  would fetch *today's* prices anyway (CLAUDE.md §6).
- Never publishes to the live bus. Use POST /autonomous/inject to push a
  synthetic event through the full pipeline.
LLM calls ARE logged to `llm_calls` (step="replay"): they spend real quota.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.core.logging import configure_logging, get_logger
from app.discovery.models import TriageDecisionRow
from app.discovery.triage import triage
from app.discovery.types import DiscoveryEvent

_log = get_logger(__name__)

# Decisions that were never a model's call (gates that fire before triage).
_NON_MODEL_PREFIXES = ("off-session", "skipped:")


@dataclass
class ReplayRow:
    event_id: str
    symbol: str | None
    headline: str
    original_passed: bool | None
    original_confidence: float | None
    replay_passed: bool | None
    replay_confidence: float | None
    replay_reason: str | None
    latency_ms: int | None
    error: str | None = None

    @property
    def outcome(self) -> str:
        if self.error is not None or self.replay_passed is None:
            return "error"
        if self.original_passed is None:
            return "new"
        if self.original_passed == self.replay_passed:
            return "agree"
        return "pass->drop" if self.original_passed else "drop->pass"


def _row_to_event(r: TriageDecisionRow) -> DiscoveryEvent:
    return DiscoveryEvent(
        id=r.event_id,
        source=r.source,
        kind=r.kind,
        symbols=[r.symbol] if r.symbol else [],
        headline=r.headline,
        body=r.body_excerpt,
        url=r.url,
        published_at=r.published_at,
    )


def _is_model_decision(r: TriageDecisionRow) -> bool:
    return not r.reason.startswith(_NON_MODEL_PREFIXES)


async def load_from_db(
    *, since: datetime | None, limit: int, balanced: bool
) -> list[tuple[DiscoveryEvent, TriageDecisionRow | None]]:
    from app.eval.persistence import get_session_factory

    async with get_session_factory()() as session:
        stmt = select(TriageDecisionRow).order_by(TriageDecisionRow.decided_at.desc())
        if since is not None:
            stmt = stmt.where(TriageDecisionRow.decided_at >= since)
        rows = [r for r in (await session.execute(stmt)).scalars().all() if _is_model_decision(r)]
    if balanced:
        # Passes are rare (~15%); a plain "latest N" sample would barely test
        # the pass side of the gate. Take half of each, newest first.
        passed = [r for r in rows if r.passed][: limit // 2]
        dropped = [r for r in rows if not r.passed][: limit - len(passed)]
        rows = passed + dropped
    else:
        rows = rows[:limit]
    return [(_row_to_event(r), r) for r in rows]


async def load_from_redis(
    *, since: datetime | None, limit: int
) -> list[tuple[DiscoveryEvent, TriageDecisionRow | None]]:
    from redis.asyncio import Redis

    from app.core.config import get_settings
    from app.eval.persistence import get_session_factory

    s = get_settings()
    redis = Redis.from_url(s.redis_url, decode_responses=True)
    try:
        start = f"{int(since.timestamp() * 1000)}-0" if since else "-"
        entries = await redis.xrevrange(s.event_stream_key, max="+", min=start, count=limit)
    finally:
        await redis.aclose()
    events = []
    for _, fields in entries or []:
        try:
            events.append(DiscoveryEvent.model_validate_json(str((fields or {}).get("event", ""))))
        except Exception:
            continue
    async with get_session_factory()() as session:
        originals = {
            r.event_id: r
            for r in (
                await session.execute(
                    select(TriageDecisionRow).where(
                        TriageDecisionRow.event_id.in_([e.id for e in events])
                    )
                )
            ).scalars()
        }
    return [(e, originals.get(e.id)) for e in events]


def build_replay_router(route: str | None) -> tuple[Any, Any]:
    """(router, usage sink): the current triage chain (or a single `--route`),
    logging to llm_calls as step=replay."""
    from app.eval.persistence import get_session_factory
    from app.llm.factory import get_provider, get_router, parse_route_spec
    from app.llm.router import LLMRouter, Route
    from app.llm.usage_log import DbUsageSink

    if route:
        provider, model = parse_route_spec(route)
        routes = [Route(provider=get_provider(provider), model=model)]
    else:
        routes = list(get_router("triage").routes)
    sink = DbUsageSink(get_session_factory())
    return LLMRouter(step="replay", routes=routes, usage_sink=sink), sink


async def replay(
    pairs: list[tuple[DiscoveryEvent, TriageDecisionRow | None]], llm: Any
) -> list[ReplayRow]:
    out: list[ReplayRow] = []
    for event, original in pairs:
        row = ReplayRow(
            event_id=event.id,
            symbol=event.symbols[0] if event.symbols else None,
            headline=event.headline[:120],
            original_passed=original.passed if original else None,
            original_confidence=original.confidence if original else None,
            replay_passed=None,
            replay_confidence=None,
            replay_reason=None,
            latency_ms=None,
        )
        started = time.monotonic()
        try:
            decision = await triage(event, llm=llm, model="(router)")
        except Exception as e:
            row.error = f"{type(e).__name__}: {e}"[:200]
        else:
            row.replay_passed = decision.passed
            row.replay_confidence = decision.confidence
            row.replay_reason = decision.reason
            row.latency_ms = int((time.monotonic() - started) * 1000)
        out.append(row)
    return out


def summarize(rows: list[ReplayRow]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    compared = sum(counts.get(k, 0) for k in ("agree", "pass->drop", "drop->pass"))
    latencies = sorted(r.latency_ms for r in rows if r.latency_ms is not None)
    return {
        "events": len(rows),
        "compared": compared,
        "agreement": round(counts.get("agree", 0) / compared, 3) if compared else None,
        "outcomes": counts,
        "original_pass_rate": _rate([r.original_passed for r in rows]),
        "replay_pass_rate": _rate([r.replay_passed for r in rows]),
        "median_latency_ms": latencies[len(latencies) // 2] if latencies else None,
    }


def _rate(values: list[bool | None]) -> float | None:
    known = [v for v in values if v is not None]
    return round(sum(known) / len(known), 3) if known else None


def _print_report(rows: list[ReplayRow], summary: dict[str, Any], label: str) -> None:
    print(f"\nTriage replay — {label}")
    print("-" * 100)
    for r in rows:
        if r.outcome == "agree":
            continue
        print(f"{r.outcome:<11} {r.symbol or '—':<6} {r.headline[:70]}")
        if r.replay_reason:
            print(f"{'':<18}now: {r.replay_reason[:80]}")
        if r.error:
            print(f"{'':<18}error: {r.error[:80]}")
    print("-" * 100)
    for k, v in summary.items():
        print(f"{k:>20}: {v}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--source", choices=["db", "redis"], default="db")
    p.add_argument("--since", type=datetime.fromisoformat, default=None)
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--balanced", action="store_true", help="half passed, half dropped (db only)")
    p.add_argument("--route", default=None, help="provider:model to replay with (default: config)")
    p.add_argument("--out", default=None, help="write the full JSON report here")
    return p.parse_args(argv)


async def main(argv: list[str] | None = None) -> dict[str, Any]:
    configure_logging()
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    since = args.since.replace(tzinfo=UTC) if args.since and not args.since.tzinfo else args.since
    if args.source == "db":
        pairs = await load_from_db(since=since, limit=args.limit, balanced=args.balanced)
    else:
        pairs = await load_from_redis(since=since, limit=args.limit)
    llm, sink = build_replay_router(args.route)
    label = args.route or " -> ".join(r.label for r in llm.routes)
    rows = await replay(pairs, llm)
    summary = summarize(rows)
    _print_report(rows, summary, label)
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(
                {"route": label, "summary": summary, "rows": [asdict(r) for r in rows]},
                fh,
                indent=2,
                default=str,
            )
    await sink.drain()  # let the llm_calls writes land before the loop exits
    return summary


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
