# ADR-0002: Data source resilience patterns

- **Date:** 2026-06-01
- **Status:** accepted
- **Phase:** 2 (Data layer + resilience)

## Context

Every data source for Terminal is a free tier with no SLA (DESIGN.md §2.4):
Alpaca (IEX delayed 15 min, 200 req/min), EDGAR (10 req/s, 429 if exceeded),
Finnhub (60 req/min, advanced endpoints premium-only), yfinance (no SLA,
unofficial, breaks periodically), FRED (free, simple registration). The
agent funnel runs unattended and must not crash because one source is down
(DESIGN.md §7: graceful degradation).

Three failure modes recur:

1. **Quota exhaustion.** We exceed the free-tier rate and start getting 429s
   for the rest of the window.
2. **Upstream outage.** A source returns 5xx or times out. Retrying immediately
   makes it worse.
3. **Silent breakage.** A source returns 200 but with malformed or empty data
   (especially yfinance after Yahoo HTML changes).

We also re-query the same ticker's filings/fundamentals repeatedly within a
research session, which wastes quota.

## Decision

Every data provider composes the same three patterns via a `BaseProvider`
base class. Composition order: **cache → circuit breaker → rate limiter →
fetch**.

```
┌─────────┐
│ Caller  │  await provider.get_xxx(...)
└────┬────┘
     │
     ▼
┌──────────────┐  hit ─────────┐
│  TTL cache   │               │
└──────┬───────┘               │
       │ miss                  │
       ▼                       │
┌──────────────────┐  open ──> ProviderUnavailable(CIRCUIT_OPEN)
│ Circuit breaker  │
└──────┬───────────┘
       │ closed/half-open
       ▼
┌──────────────────┐  block until token available
│  Rate limiter    │
└──────┬───────────┘
       │
       ▼
┌──────────────────┐  network call to upstream
│  fetch(fn)       │ ── exception ──> retry w/ backoff (max N), then
└──────┬───────────┘                  ProviderUnavailable(UPSTREAM_ERROR)
       │ success
       ▼
   cache + return
```

**TTL cache** (`app/data/cache.py`): in-memory dict, async-safe, per-key
expiry. Interface allows swapping to Redis (Phase 6+) without touching
providers.

**Token-bucket rate limiter** (`app/data/rate_limit.py`): one bucket per
provider, sized to the source's published limit. `acquire()` blocks; no
quota-exhaustion 429s in normal operation.

**Three-state circuit breaker** (`app/data/circuit_breaker.py`): closed →
opens after N consecutive failures → half-open after recovery timeout → one
trial call decides whether to close or reopen. While open, calls raise
immediately, protecting the upstream and the agent funnel's latency.

**`BaseProvider`** (`app/data/base.py`): composes all three plus
retry-with-exponential-backoff for transient errors. Subclasses call
`self._fetch_cached(key, ttl, fetch_fn)` from their public methods. Any raw
exception from `fetch_fn` is translated to `ProviderUnavailable` — the agent
funnel sees only the typed failure path.

## Alternatives considered

- **No cache, retry-only.** Quota exhaustion would be guaranteed under
  realistic call patterns. Rejected.
- **`tenacity` for retry, `pyrate-limiter` for rate, `pybreaker` for breaker.**
  Three libraries, three configuration surfaces, dependency-version risk.
  Hand-rolled implementations are ~30 lines each and let the BaseProvider
  compose them cleanly. Rejected: we'd save maybe 100 lines of code at the
  cost of three external surfaces. Not worth it.
- **Per-method decorators** (`@cached`, `@rate_limited`, `@circuit_broken`).
  Decorator stacking is fragile and hides the composition order. Rejected:
  explicit `_fetch_cached(key, ttl, fn)` is easier to read and test.
- **Provider-level fallback** ("if Alpaca prices fail, fall back to yfinance
  in code"). Couples providers to each other and hides the failure from
  config visibility. Rejected: fallbacks are config-driven at the registry
  level instead (DESIGN.md §5 Fallbacks table).

## Consequences

**Positive:**
- Every provider has identical resilience semantics. Reviewing a new provider
  is mostly checking it calls `_fetch_cached`.
- The agent funnel handles one failure type: `ProviderUnavailable` with a
  typed reason. It never sees raw `httpx.HTTPError`, `TimeoutError`, etc.
- The breaker prevents thundering-herd retries against a down source.
- Tests for the resilience layer are pure — no network — so they run in
  milliseconds.

**Negative:**
- A misconfigured cache TTL can mask staleness. Mitigation: TTLs are in
  config, not code, and per-call-type (`cache_ttl_quotes=5s` vs
  `cache_ttl_filings_list=600s`).
- Retry plus circuit breaker creates a subtle interaction: each retry
  counts as a separate failure to the breaker. Mitigation: `max_retries`
  defaults to 2; `failure_threshold` defaults to 5 — a single fully-failing
  call uses 3 of the budget.
- In-memory cache is per-process. Producer, agent worker, and FastAPI each
  hold their own. This is fine until thesis-memory retrieval (Phase 6+),
  when we'll move to Redis as already planned in DESIGN.md §3.

**Reversibility:** High. Swapping the cache backend is a one-class change.
Removing the circuit breaker would be a config flag (`breaker_enabled=false`)
if we ever wanted to bypass it for debugging.

## References

- DESIGN.md §5 (Data sources, including the Fallbacks table)
- DESIGN.md §7 (Coding conventions: cache, rate-limit, circuit-breaker, graceful degradation)
- Code: `backend/app/data/{cache,rate_limit,circuit_breaker,base}.py`
- Tests: `backend/tests/test_{cache,rate_limit,circuit_breaker,base_provider}.py`
- Related ADRs: [[ADR-0001-architecture-overview]]
