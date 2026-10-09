# Runbook — running Terminal continuously

Operational guide for running the full stack under `docker-compose`. The same
`docker-compose.yml` runs on the dev Mac (now — **Phase A**), the spare laptop
(24/7 target — **Phase B**), or a VPS (fallback). See `CLAUDE.md` §11 for the
deployment story; this doc is the day-to-day command reference.

> **This is Phase A: continuous running on the dev Mac.** Everything below is
> for the Mac. Phase B (home server + Tailscale + nightly `pg_dump`) reuses the
> exact same commands pointed at a different machine.

---

## This machine (dev Mac): isolated ports

This Mac already runs a **shared native PostgreSQL 14** (brew `postgresql@14`,
databases `papersearch` / `scribur_db` / `terminal`) on 5432, a native Redis on
6379, and other projects' dev servers (octave on 5173, another backend on 8000).
Terminal's Docker stack therefore runs on **remapped host ports** via a local,
gitignored `docker-compose.override.yml` so it collides with none of that. The
base `docker-compose.yml` keeps default ports for the home server / VPS, where
there is no conflict and no override is needed.

| Service | Default port (base / home server) | **This Mac (override)** |
|---|---|---|
| Frontend (UI) | 5173 | **5174** → <http://localhost:5174> |
| Backend (API) | 8000 | **8010** → <http://localhost:8010> |
| Postgres | 5432 | **5433** |
| Redis | 6379 | **6380** |

> **When following the commands below on this Mac, substitute `8010` for `8000`
> and `5174` for `5173`** in any `curl`/browser URL. The `docker compose …`
> commands themselves are unchanged — Compose auto-merges the override file.
> Docker-internal comms are unaffected (containers still talk to `postgres:5432`
> / `redis:6379` on the compose network).

**Data origin (one-time migration, 2026-06-25).** Terminal's eval data was
migrated out of the shared native Postgres into Terminal's own Docker volume
(`terminal_postgres_data`) with `pg_dump` → restore. The native copy was left
fully intact on disk as a rollback. The backup lives at
`backend/.local/terminal-native-backup-2026-06-25.sql` (gitignored). Terminal no
longer depends on the native Postgres/Redis at all; it is self-contained in
Docker. Rollback, if ever needed: stop the Docker stack and restart the native
services + native backend.

---

## The stack at a glance

Five services, defined in `docker-compose.yml`. Three of them are the
application's logical processes (CLAUDE.md §4), which meet only at Postgres
and Redis:

| Service | Image / build | Port | Durable volume | Role |
|---|---|---|---|---|
| `ingestion` | `./backend`, `python -m app.discovery.ingest` | — | — | Always-on producer: Alpaca news websocket + EDGAR poller → Redis Streams |
| `backend` | `./backend` (FastAPI + agent worker) | 8000 | — (state in Postgres) | Consumes the stream (triage → thesis → risk → paper engine), reconciliation jobs, catalyst scheduler, API + SSE |
| `frontend` | `./frontend` (static build served by nginx) | 5173 | — | The terminal UI |
| `postgres` | `postgres:16-alpine` | 5432 (loopback only) | `postgres_data` | Theses, outcomes, trades, triage log, `llm_calls`, `thesis_attempts` |
| `redis` | `redis:7-alpine` | 6379 (loopback only) | `redis_data` | `terminal:events` stream (the bus + replay log) and the ingestion heartbeat |

All five use `restart: unless-stopped`. The agent worker's jobs (reactive
runner, reconciliation, catalyst scheduler) are in-process `asyncio` tasks
inside `backend` and auto-resume on every boot (`autonomous_autostart = True`).
The `ingestion` container has no HTTP port; its healthcheck passes while its
heartbeat key (`terminal:ingest:status`, 45 s TTL) is fresh in Redis.

---

## Prerequisites

1. **Docker Desktop must be running.** If `docker compose ps` prints
   `Cannot connect to the Docker daemon`, start Docker Desktop first (open the
   app, or `open -a Docker`, then wait ~30s for the whale icon to settle).
2. **`.env` must exist** in the repo root (gitignored). The backend reads it via
   `env_file`. `ENVIRONMENT=development` **must** stay set — in `production` the
   backend serves an empty CORS allow-list and the dockerized frontend can no
   longer call it. Copy from `.env.example` if starting fresh.
3. Run all commands from the repo root (`/Users/nikhilesh/Work/Projects/Terminal`).

> **Heads-up: port conflicts.** If you also run the backend locally
> (`uv run uvicorn ...`) or a local Postgres, ports 8000 / 5432 will already be
> taken and `docker compose up` will fail to bind. Stop the local processes
> first, or run only the containers you need. Pick one source of truth per port.

---

## TL;DR — the commands you actually use

```bash
docker compose up -d --build      # start everything (rebuild images if code changed)
docker compose ps                 # what's running + health
docker compose logs -f backend    # follow backend logs (Ctrl-C to detach; containers keep running)
docker compose restart backend    # restart just the backend (worker auto-resumes)
docker compose restart            # restart the whole stack
docker compose stop               # stop everything, keep containers + data
docker compose down               # remove containers + network, KEEP volumes (data safe)
```

> ⚠️ **NEVER run `docker compose down -v` unless you intend to delete the
> database.** The `-v` flag removes the named volumes (`postgres_data`,
> `redis_data`), which **erases all eval data, theses, and paper trades** — the
> irreplaceable, accumulating value of this project. Plain `down` (no `-v`) is
> always safe.

---

## First-time setup (fresh machine or fresh volume)

```bash
cp .env.example .env          # then fill in the free API keys
docker compose up -d --build  # that's it
```

The backend runs `alembic upgrade head` before starting uvicorn (idempotent),
so a brand-new `postgres_data` volume gets its schema automatically, and the
`ingestion` service waits for the backend to be healthy (i.e. migrated)
before it starts writing dedup rows. Then open <http://localhost:5173>.

> The frontend is a static bundle with the API address baked in at build time
> (`VITE_API_URL`, a build arg in `docker-compose.yml`). If you change the
> published API port, rebuild the frontend: `docker compose build frontend`.

---

## Daily operations

### Start / stop

```bash
docker compose up -d                 # start (detached). add --build after code changes
docker compose stop                  # graceful stop, containers + volumes preserved
docker compose start                 # start previously-stopped containers
```

### Restart

```bash
docker compose restart               # whole stack
docker compose restart backend       # one service — worker auto-resumes on boot
```

A `restart` preserves all data (it does not touch volumes). Events live in the
Redis stream, and the worker ACKs each one only after processing it, so an
event the backend was mid-way through when it restarted is re-delivered on
boot. Restarting `backend` doesn't touch the news websocket (it lives in
`ingestion`). Events that sat on the bus longer than
`reactive_max_event_age_seconds` (15 min) while the worker was down are skipped
as *stale* with a visible triage decision, not triaged — see "What survives a
restart" below.

### Logs

```bash
docker compose logs -f               # follow all services
docker compose logs -f backend       # one service
docker compose logs --tail=200 backend   # last 200 lines, no follow
```

Logs are structured JSON with correlation IDs. Detaching from `-f` (Ctrl-C)
does **not** stop the containers.

### Status / health

```bash
docker compose ps                                  # container state + health column
curl -s localhost:8000/health | jq                 # backend liveness
curl -s localhost:8000/autonomous/status | jq '{state, ingestion_state, news_stream_state, events_consumed, theses_produced, last_error}'
curl -s localhost:8000/llm/health | jq '.steps[] | {step, status, routes: [.routes[] | "\(.provider):\(.model) \(.state)"]}'
```

After any restart, confirm `/autonomous/status` shows `"state": "running"`,
`"ingestion_state": "up"` and `"news_stream_state": "connected"`.

### Updating after a `git pull`

```bash
git pull
docker compose up -d --build    # rebuild changed images; migrations apply on backend start
```

---

## Continuous running on the Mac (the Phase A goal)

`restart: unless-stopped` means: if a container crashes, or the Docker daemon
restarts (e.g. after a Mac reboot), Docker brings the container back
**automatically** — *unless* you explicitly `docker compose stop`/`down`'d it
(that state is remembered across daemon restarts). So:

- **To survive a Mac reboot**, Docker Desktop must come back on login. Enable
  **Docker Desktop → Settings → General → "Start Docker Desktop when you sign
  in."** Without it, nothing restarts until you manually open Docker.
- Once that's set and the stack is `up`, the Mac can reboot and the whole stack
  (including the agent worker) comes back on its own.
- The tradeoff: the Mac must stay powered and awake. Check **System Settings →
  Battery/Lock Screen** and prevent sleep on power if you want true 24/7 (or
  accept that the agent pauses while the Mac sleeps). This limitation is exactly
  why Phase B moves to an always-on home server.

---

## What survives a restart vs. what's ephemeral

| State | Where it lives | Survives `restart` / `down`? | Survives `down -v`? |
|---|---|---|---|
| Eval outcomes, theses, attempts, paper trades, accounts, triage decisions, catalysts, `llm_calls` | Postgres (`postgres_data` volume) | ✅ yes | ❌ **deleted** |
| Event bus + replay log (`terminal:events`, ~2 months retained) | Redis (`redis_data` volume) | ✅ yes; un-ACKed events re-delivered | ❌ deleted (events lost; eval data unaffected) |
| LLM route cooldowns, funnel counters in the status strip | process memory | ❌ reset (cooldowns re-learn on the next 429) | ❌ |

**Backups (Phase B).** The eval data is the one thing that can't be regenerated.
On the home server, set up a nightly `pg_dump` to cloud storage (Backblaze B2
free 10 GB) per `CLAUDE.md` §11. A manual dump any time:

```bash
docker compose exec postgres pg_dump -U terminal terminal > backup-$(date +%F).sql
```

---

## Restart-policy audit + live verification (2026-06-25)

Done as part of the Phase A "continuous running" task.

**Static audit:**

- ✅ All four services declare `restart: unless-stopped`.
- ✅ No service spawns an OS-level child process or detached thread that escapes
  its container. The backend's always-on work is 13 in-process `asyncio` tasks;
  the only `run_in_executor` is a bounded `ThreadPoolExecutor(max_workers=4)`
  for blocking yfinance calls — all bounded to the uvicorn process and reaped
  with the container.
- ✅ Worker auto-resumes after restart (`autonomous_autostart = True`, and it is
  skipped only when `environment == "test"`).
- ✅ Durable state is entirely in Postgres on a named volume; a `restart`/`down`
  preserves it. Only `down -v` destroys it (warned above).
- ✅ ~~Migrations are manual~~ — fixed 2026-10: the backend command runs
  `alembic upgrade head` before uvicorn, so upgrades are self-healing.

**Live verification (ran on the dockerized stack with the real eval data):**

- ✅ `docker compose restart` (whole stack): row counts identical before/after
  (theses ids `1,2,3,4`; 3 shadow trades; 3 outcomes), worker auto-resumed to
  `state: running`, backend healthy in ~8s.
- ✅ `docker compose down` + `up -d` (containers fully removed and recreated —
  the reboot scenario): both named volumes persisted, data identical, worker
  `running`, frontend HTTP 200. This is the proof that survives a Mac reboot.
- ✅ Worker came up connected: Alpaca news WS `connected`, triage=groq,
  thesis=gemini.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Cannot connect to the Docker daemon` | Docker Desktop not running | Start Docker Desktop, wait ~30s |
| `bind: address already in use` on 8000/5432 | local backend/Postgres running | Stop the local process, or don't run both on the same port |
| Frontend loads but API calls are CORS-blocked | `ENVIRONMENT` ≠ `development` | Set `ENVIRONMENT=development` in `.env`, `docker compose up -d backend` |
| Backend crashes on boot with "relation does not exist" | fresh volume, no schema | `docker compose run --rm backend alembic upgrade head` |
| `/autonomous/status` shows `stopped` after restart | autostart disabled or startup error | check `docker compose logs backend` for `autonomous_worker_autostart_failed` |
| `uv run` fails locally with "No such file or directory" | stale venv shebangs after a repo move | `rm -rf backend/.venv && cd backend && uv sync` (this is a local-dev issue, not a container one) |
| `ingestion_state: "down"` | ingestion container stopped/crashing | `docker compose logs --tail=100 ingestion`; `docker compose up -d ingestion` |
| `news_stream_state: "auth_failed"` | Alpaca key/secret wrong or revoked | fix `.env`, `docker compose up -d ingestion` (the stream retries every max-backoff interval meanwhile) |
| News stream logs `406 connection limit exceeded` | old session still holds Alpaca's single free slot (right after a restart) | nothing — it backs off and reconnects on its own |
| Triage feed shows `skipped: all triage LLM routes rate-limited` | every free quota in the chain is parked | expected under heavy news; `/llm/health` shows each route's `cooldown_until`. Daily Gemini quotas reset at midnight Pacific |
| Triage feed shows `skipped: stale` | the worker came back to a backlog, or one slow thesis (minutes of failover across overloaded models) held the single consumer while news queued | expected — reactive news older than 15 min isn't worth a thesis. Known limit: the worker processes events one at a time, so thesis latency directly delays triage of the next events |
| UI calls the wrong API port | `VITE_API_URL` is baked at build time | change the build arg, `docker compose build frontend && docker compose up -d frontend` |

---

## Operator tools

```bash
# Replay: re-run triage over the stored event log; report agreement with the
# original decisions (read-only; LLM calls are logged as step=replay).
docker compose exec backend python -m app.discovery.replay --limit 80 --balanced
docker compose exec backend python -m app.discovery.replay --source redis --since 2026-10-09

# One-time re-grade of outcomes written by the pre-2026-10 trade-close method
# (idempotent; previous values kept in thesis_outcomes.notes). Back up first.
docker compose exec postgres pg_dump -U terminal terminal > backup-$(date +%F).sql
docker compose exec backend python -m app.portfolio.regrade

# Push a synthetic event through the full funnel (a dry run: never persisted,
# traded or scored)
curl -s -X POST localhost:8000/autonomous/inject -H 'content-type: application/json' \
  -d '{"symbol":"NVDA","headline":"NVIDIA beats Q3 estimates, raises guidance"}'
```

### MCP server (query Terminal from an MCP client)

Read-only: seven tools over the REST API, no database credentials needed.
For Claude Desktop, add to `claude_desktop_config.json`
(`~/Library/Application Support/Claude/` on macOS), adjusting the path and port:

```json
{
  "mcpServers": {
    "terminal": {
      "command": "uv",
      "args": ["--directory", "/path/to/Terminal/backend", "run", "python", "-m", "app.mcp_server"],
      "env": { "TERMINAL_API_URL": "http://localhost:8000" }
    }
  }
}
```

Tools: `terminal_status`, `list_theses`, `eval_scorecard`, `calibration`,
`portfolio`, `recent_news`, `upcoming_catalysts`. The client reads *from*
Terminal; Terminal itself never calls an Anthropic API.
