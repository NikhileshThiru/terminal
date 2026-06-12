# ADR-0001: Three-process architecture meeting at PostgreSQL

- **Date:** 2026-06-01
- **Status:** accepted
- **Phase:** 1 (Foundation)

## Context

Terminal is not a single web app. It has three distinct workloads (DESIGN.md §4):

1. **Always-on data ingestion** — long-lived WebSocket connections to Alpaca's
   Benzinga news feed, polling of SEC EDGAR for new 8-Ks, the flag scanner.
   Must run independently of any browser; reconnection and dedup are owned here.
2. **Always-on agent worker** — consumes the event stream, runs the LLM decision
   funnel (triage → thesis → grounding → risk → paper engine), persists to
   Postgres. Also runs scheduled jobs (catalyst calendar, mark-to-market).
3. **Request-driven HTTP/SSE/WebSocket server** — serves the React frontend,
   streams agent reasoning over SSE, streams live price ticks over WebSocket.

These workloads have different lifecycle, scaling, and failure characteristics.
Bundling them into one process couples concerns and makes failure isolation
much harder (one crashing ingestion handler should not take the dashboard
offline).

We also need to be able to develop on a Mac, run 24/7 on a spare laptop, and
optionally fall back to a small VPS — all from the same artifact (DESIGN.md
§11). A request-driven serverless target (Vercel, Netlify) cannot host the
always-on producers.

## Decision

Three logical processes that **never call each other directly**. They meet only
at the database (PostgreSQL) and the in-memory event bus (Redis Streams).

```
Producers (always-on)              Consumer (request-driven)
─────────────────────              ─────────────────────────
Ingestion ──events──> Redis Streams
                          │
                          ▼
                      Agent worker ───results──> PostgreSQL ◄─reads── FastAPI ─> React
```

Each process is its own Docker container in `docker-compose.yml`. The compose
file is the deployment artifact; the same file runs on Mac, laptop, or VPS.

## Alternatives considered

- **Monolithic FastAPI app with background tasks.** Simpler bootstrap, but
  conflates lifetimes. A FastAPI worker restart kills in-flight ingestion;
  ingestion failures degrade HTTP latency. Rejected: failure isolation matters
  more than the few hours of saved scaffolding.
- **Direct calls between processes (gRPC/HTTP).** Producers calling consumers
  creates back-pressure issues and requires producers to know about consumer
  topology. Rejected: Postgres + Redis Streams give us durability and
  decoupling for free.
- **Kafka / Redpanda for the event bus.** Real Kafka semantics matter at high
  event volume. We expect ~130 news events/day plus EDGAR — comfortably under
  the threshold where Redis Streams is insufficient (DESIGN.md §3 upgrade
  trigger). Rejected: would burn complexity budget on nothing.
- **Temporal for orchestration.** Durable workflows are appealing, but
  idempotent workers with state in Postgres survive restarts well enough for
  this project (DESIGN.md §3). Rejected for the same reason as Kafka.
- **Serverless (Vercel + Vercel Cron + cloud Postgres).** Cannot host
  always-on WebSocket consumers. Even the request-driven part fights against
  the SSE/WebSocket usage pattern. Rejected: fundamentally wrong runtime model.

## Consequences

**Positive:**
- Each process can crash independently; the others keep working.
- The agent worker can be rewritten in any language later (we won't, but the
  option exists because the coupling is data-only).
- Adding a fourth process (e.g., Discord bot) is trivial — it reads Postgres,
  optionally subscribes to Redis Streams. No existing code changes.
- Same docker-compose runs everywhere (Mac dev, laptop 24/7, VPS fallback).
- Replay and synthetic-event injection are easy because the bus is the
  contract: produce events into Redis Streams, consumers don't care where
  they came from.

**Negative:**
- Three processes means three Dockerfiles, three sets of logs, three startup
  orderings to think about (compose handles the orderings via `depends_on`
  with `condition: service_healthy`).
- Local development requires running multiple processes (or `docker compose
  up`) rather than a single `uvicorn` command.
- A schema change to Postgres is a coordination point across all three
  processes. Alembic migrations are mandatory from day 1, not optional.
- Event-bus failure isolation is on us: if Redis Streams goes down, the agent
  worker stops processing new events. Postgres remains the system of record;
  the producer continues to write raw events to Postgres so nothing is lost.

**Reversibility:** Moderate. Merging two processes into one is straightforward
if it ever makes sense (they share a language and codebase). Splitting them
further is harder. We expect the current factoring to remain correct through
the project's lifetime.

## References

- DESIGN.md §4 (Architecture)
- DESIGN.md §3 (Tech stack — explicit upgrade triggers for the deferred
  alternatives)
- DESIGN.md §11 (Hosting — explains why serverless is unsuitable)
- Related ADRs: [[ADR-0002-data-source-resilience]] (Phase 2)
