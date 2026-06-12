# Step 9 plan — deferred earned features

Written 2026-06-12, after the fresh-eyes redesign pass. These are the four
remaining "earned" features from the rolling plan. None block the demo gate;
all are independent of each other. Recommended order below is by
value-per-hour given what's already wired.

---

## 1. Discord alerts (smallest, highest demo value)

**What:** POST to a Discord webhook when (a) any thesis is persisted and
(b) a shadow trade is placed. Formatted embed: ticker, direction, confidence,
source bucket, suggested contract, grounding status, link to the dashboard.

**Design:**
- Config: `DISCORD_WEBHOOK_URL` in `.env` (absent → feature off; no code path
  change). Add `discord_webhook_url: str | None = None` to `Settings`.
- New module `app/notify/discord.py` with `async def send_thesis_alert(thesis)`
  and `async def send_trade_alert(trade)`. Plain `httpx` POST, 5s timeout,
  fire-and-forget (`asyncio.create_task`) — a Discord outage must never block
  the funnel (DESIGN.md §2.4 graceful degradation).
- Call sites: `eval/persistence.write_thesis` (after commit) and
  `portfolio/engine` (after a shadow trade commits). Both behind
  `if settings.discord_webhook_url`.
- Rate safety: Discord webhooks allow ~30 req/min; we produce a few events/day.
  No limiter needed; log-and-drop on 429.

**Tests:** unit-test the embed builder (pure function); integration test with
`respx`-mocked webhook asserting fire-and-forget never raises.

**Effort:** ~1 session.

---

## 2. Persistent LLM usage log (observability, part 1)

**What:** replace the process-scoped `CostTracker` singleton with a durable
`llm_usage_log` table so cost survives restarts. The header chip then shows
all-time + session numbers.

**Design:**
- Alembic migration: `llm_usage_log(id, at, provider, model, call_site,
  input_tokens, output_tokens, cost_usd, correlation_id)`.
- `CostTracker.record()` keeps its in-memory aggregate (cheap, sync) and ALSO
  enqueues a row; a tiny async writer flushes to Postgres (or write directly —
  call volume is tiny, ~100/day).
- `/llm/cost-summary` gains `?window=session|all|7d`.
- Keep pricing constants in `app/llm/cost.py` as the single source.

**Effort:** ~1 session. Do this BEFORE Langfuse so the data layer exists even
if Langfuse is dropped later.

## 3. Langfuse traces (observability, part 2)

**What:** ship every LLM call (triage + thesis + grounding retries) as a
Langfuse trace, keyed by correlation ID, so a thesis's whole funnel is
inspectable as one trace tree.

**Design:**
- Free tier: Langfuse Cloud (50k observations/mo) or self-host the Docker
  container alongside Postgres/Redis later. Config: `LANGFUSE_PUBLIC_KEY`,
  `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` (absent → disabled).
- The plumbing already exists: `LLMProvider` implementations return `LLMUsage`,
  and `Copilot.generate` threads an `event_sink`. Add a second sink that maps
  `CopilotEvent`s to Langfuse spans (`started`→trace, `tool_call`/`tool_result`
  →spans, `grounding_check`→event, `done`→trace close).
- Never block the funnel: wrap the client in try/except + `log.warning`.

**Verify first (DESIGN.md §10):** Langfuse free-tier limits as of mid-2026.

**Effort:** ~1 session once the usage log exists.

---

## 4. Replay mode (development quality-of-life)

**What:** re-consume stored discovery events through the funnel for offline
debugging (DESIGN.md §9 Phase 6 calls this out). Today the injector covers
single synthetic events; replay covers "what would yesterday have looked like."

**Design:**
- The raw material already lands in `triage_decisions` (headline, symbol,
  source, kind, decided_at) and `seen_discovery_events`. Add
  `POST /autonomous/replay?since=...&limit=...` that re-publishes stored
  events onto the in-memory bus with `replayed=true` in the envelope.
- Guardrails: replayed events that produce theses MUST be tagged (new
  `Thesis.replayed` bool or reuse `pre_strictness`-style flag) and excluded
  from `/eval/summary` by default — replay output is for debugging, not the
  forward-tested record (ADR-0005 spirit: never contaminate the eval).
- Optional: `--speed` multiplier to compress a day into minutes.

**Effort:** 1–2 sessions (the eval-exclusion flag is the important part).

---

## 5. MCP server (the "flex", build last)

**What:** expose the system as MCP tools so Claude Desktop (or any MCP client)
can query it: `get_positions`, `get_thesis_history(symbol)`, `get_eval_summary`,
`get_upcoming_catalysts`, `run_copilot_thesis(idea, budget)`.

**Design:**
- Python MCP SDK (`mcp` package), stdio transport, new entrypoint
  `backend/app/mcp/server.py` reading the same Postgres + calling the same
  service functions as the API routers (share code via small service-layer
  functions, not by importing FastAPI handlers).
- Read-only tools first; `run_copilot_thesis` is the only write and it's
  paper-only by construction.
- Document in README with a Claude Desktop config snippet — this is a
  portfolio piece, the README story matters as much as the code.

**Effort:** 1–2 sessions.

---

## Sequencing

1. Discord alerts (instant gratification, tiny)
2. Persistent usage log
3. Langfuse
4. Replay mode
5. MCP server

Nothing here requires Redis Streams, Temporal, or any §6 "don't" — all five
fit the existing v1 stack.
