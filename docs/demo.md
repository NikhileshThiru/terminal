# Demo — a screenshot tour

Real data from the running system (captured 2026-10-09, during market hours).
Nothing here is seeded or mocked: every thesis, trade and outcome on these
screens came out of the live pipeline. Paper trading only.

---

## 1. The dashboard — the market, all day

![Dashboard](screenshots/final-2026-10/dashboard.png)

- **Header:** market session and NYSE clock, the autonomous worker's state, the
  thesis model, LLM calls today against the free-tier budget (the dot is route
  health — see §6), backend health.
- **Status strip:** the funnel this session (events → triaged → theses), the
  eval to date, both paper accounts, and when the last thesis/news landed.
- **Watchlist** with sparklines and live marks (UI prioritization only — the
  agent watches the whole market), a **chart** with timeframe selector, **ticker
  info** with the options-chain deep dive, the **live theses** feed, and the
  **news/triage** feed showing every event the gate passed or dropped, and why.

## 2. Autonomous — the always-on half

![Autonomous](screenshots/final-2026-10/autonomous.png)

The worker's health at a glance: ingestion service (`up`), Alpaca news
websocket (`connected`), events consumed, triage passes, theses, *visible*
skips (stale backlog / quota exhausted / daily thesis cap), failures, and the
models in use. Below: every autonomous thesis with its grounding badge, and the
earnings calendar the catalyst scheduler pre-positions on.

## 3. Ask the Agent — the same engine, pointed at your idea

Type a hunch in plain English. The agent's tool-calling loop streams live over
SSE into the Agent Reasoning pane — quotes, options chain, analyst ratings,
earnings history, then the grounding check on every number it cited (§6 shows
the stream, on the run that hit the quota wall).

![The thesis it produced](screenshots/final-2026-10/copilot.png)

The result: direction, stated confidence, a specific contract from a chain it
actually fetched (strike and premium verified against it), max risk inside the
budget, the payoff at expiration, and a falsifiable "what must happen" with its
evaluation window. This thesis is now in the `manual` eval bucket, and the
aggressive paper account opened a shadow trade on it (the conservative account
declined: over its $300 per-trade cap).

## 4. Portfolio — two accounts, one experiment

![Portfolio](screenshots/final-2026-10/portfolio.png)

The same theses run through two deterministic risk gates — conservative
(confidence ≥ 0.70, smaller) and aggressive (≥ 0.50, bigger). Open positions
are marked to market every 5 minutes; closed trades show why they closed
(stop, theta exit, expiry settled at the expiration-day close) and the realized
P&L.

## 5. Evaluation — is the AI any good?

![Evaluation](screenshots/final-2026-10/eval.png)

- **Per-bucket scorecards:** manual / reactive / catalyst are different games
  (ADR-0004), so they're never averaged. Brier 0.25 is the coin-flip baseline.
- **Shadow → paper go/no-go:** the autonomous agent stays shadow-only until it
  clears 50 theses, ≥95% grounding (measured over attempts, including
  rejections) and Brier ≤ 0.25. Today it clears one (grounding: 100%, 9 of
  9 judged attempts) and misses two (31 of 50 theses; Brier 0.289) — and says so.
- **Calibration plot:** stated confidence vs realized hit rate; perfect
  calibration is the diagonal.
- **Every graded prediction:** each thesis is graded over its own prediction
  window — from the price when it was written to the close at the window's end
  — traded or not (ADR-0008). Re-graded outcomes keep their old values in the
  notes, so the correction is auditable.

## 6. LLM routes — running on free tiers, visibly

![LLM routes](screenshots/final-2026-10/llm-routes.png)

Each step's failover chain (triage on Groq, theses across seven Gemini models —
free quotas are per model), which routes are parked on a quota and until when,
and today's per-model usage from the durable `llm_calls` log (ADR-0006).

![Quota exhausted](screenshots/final-2026-10/quota-exhausted.png)

What running out looks like: 10:17 ET on the first live morning, after 10
theses, every free thesis model was parked on its daily cap. A manual run
streams its first tool calls, then fails fast with the reason — no hang, no
guess — and the routes recover at midnight Pacific.

---

**Also available:** a read-only MCP server (`python -m app.mcp_server`) so an
MCP client can ask "how is the agent doing?" in plain language, and
`python -m app.discovery.replay` to re-run triage over the stored event log.
See the [runbook](runbook.md).
