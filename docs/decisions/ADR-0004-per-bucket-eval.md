# ADR-0004: Score thesis quality per-bucket, not in aggregate

- **Date:** 2026-06-05
- **Status:** accepted
- **Phase:** 5 (eval harness)

## Context

The eval harness logs every thesis with `predicted_direction`, `confidence`,
`prediction_window_days`, and (eventually) `realized_direction` + `hit`.
The natural shape of "how good is the model" is a single Brier score
(or hit-rate, or calibration curve) over all theses.

But theses originate from three structurally different paths:

- **Manual copilot**: user supplies the idea; the model researches it.
  Selection bias: only ideas the user found worth typing.
- **Reactive autonomous**: news arrives; model triages then writes a
  thesis. Latency-disadvantaged by ~15 min vs paid feeds (free-tier
  Alpaca + EDGAR rules).
- **Catalyst-driven autonomous**: a known event (earnings, Fed, FDA)
  is on the calendar; model pre-positions a thesis days ahead. No
  latency disadvantage — we know the event is coming.

These distributions are not exchangeable. The catalyst path is the only
one with a plausible mechanism for measurable directional edge, because
it removes the latency handicap. The reactive path almost certainly
produces no edge (we are racing the market with delayed data). The
manual path is a different population entirely (whatever the user types).

Averaging the three together hides whether the model has edge in the
*one* path where it could.

## Decision

Every thesis records its `source_bucket` (`manual` | `reactive` |
`catalyst`). The eval API (`/eval/summary`, `/eval/calibration`) groups
metrics by bucket. The frontend Eval panel displays three independent
bucket cards + a per-bucket calibration plot. There is no single
aggregate Brier score in the public UI.

## Alternatives considered

- **Single aggregate score**. Simpler to compute and easier to summarise
  in a single number. Rejected because it obscures the only honest
  scientific claim the project can make (catalyst edge, if any) by
  diluting it with reactive noise.
- **Score everything separately by ticker / sector / direction**.
  Reasonable in the long run; deferred. The three-bucket split is the
  one boundary forced by structural asymmetries (latency, selection,
  timing). Sub-buckets within those are an analysis exercise, not a
  design boundary.
- **Score per-model rather than per-bucket**. Also useful (the manual
  copilot can run different models than the autonomous worker), but
  orthogonal — the per-model breakdown would be a column in addition
  to the per-bucket split, not instead of it.

## Consequences

- **Positive:**
  - The reactive bucket is allowed to score near 0.25 (coin-flip Brier)
    without that being read as "the system fails" — that's the
    expected outcome of a latency-disadvantaged signal, and labelling
    it as such is honest.
  - The catalyst bucket gets a fair shot at demonstrating edge if it
    exists, because it isn't averaged with structurally weaker signals.
  - Calibration plots per bucket are immediately interpretable: "in the
    catalyst bucket, when the model says 70%, it's right 65% of the
    time" is a single concrete sentence.
- **Negative:**
  - Three buckets means three sample-size problems. At any given moment
    one or more buckets has small N and noisy metrics. The UI mitigates
    this with explicit empty states ("no resolved outcomes in this
    bucket yet").
  - Adds one column to the persistence schema and one knob to every
    query that aggregates theses.
- **Reversibility:** Trivial. `source_bucket` is a column we already
  populate; collapsing the API + UI back to "everything" is a few-line
  change. Adding more buckets is also straightforward.

## References

- Related ADRs: [[ADR-0003-deterministic-risk]] (uses `confidence` for
  calibration, but not for execution).
- DESIGN.md §8 (per-bucket scoring requirement).
- README "What I learned" — Phase 5 (per-bucket scoring), Catalyst calendar.
- `app/eval/scoring.py` — Brier, hit-rate, calibration_buckets pure functions.
- `app/api/eval.py` — `/eval/summary` and `/eval/calibration` endpoints.
