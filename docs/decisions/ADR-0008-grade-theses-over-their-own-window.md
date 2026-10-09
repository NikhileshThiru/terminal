# ADR-0008: Grade every thesis over its own prediction window, independent of trades

- **Date:** 2026-10-09
- **Status:** accepted (supersedes the grading part of the Phase 7 resolver)
- **Phase:** 5 / 7 (eval harness, paper engine)

## Context

The outcome resolver graded a thesis at the moment its shadow trade closed,
from that day's daily close to the price at close time. Reviewing the 24
graded outcomes showed two selection biases:

- **Grading at the stop-loss.** Most trades closed on the thesis's own
  `exit_if_underlying_below/above` stop — usually the same afternoon. A
  stop fires *because* the price moved against the position, so the grade
  was sampled at the worst moment, never at the horizon the thesis claimed
  (`prediction_window_days`). The autonomous bucket read an 18% hit rate.
- **Untraded theses were never graded.** Outcomes were written only on trade
  close, so a thesis that neither account traded (below both confidence
  gates, over budget) vanished from the calibration plot.

## Decision

Thesis grading and trade closing are separate jobs in `OutcomeResolver`.
Trades still close on expiry (4pm ET, settled at the expiration-day close),
stop, or theta exit — that's the P&L story. Every thesis, traded or not, is
graded once its window's last session has closed: realized move = close on
the window's last trading day (weekends roll back) vs. the price when the
thesis was written (last 5-minute bar at or before `generated_at`; never a
close from after it). ±0.5% is "flat" and counts as a miss. Existing
outcomes were re-graded in place (`python -m app.portfolio.regrade`), with
their previous values preserved in `notes` and a pre-regrade `pg_dump` kept.

## Alternatives considered

- **Keep grading at trade close.** It measures the stop-loss rule, not the
  prediction — the bias this ADR exists to remove.
- **Grade on option P&L.** A long call can lose while the stock goes up
  (theta, IV crush); that scores the trade structure, not the prediction.
- **Leave old outcomes, grade only new theses.** Leaves a biased sample in
  the headline numbers. The predictions themselves were stored before any
  outcome existed, so re-measuring them with later market data is still a
  forward test — no hindsight enters the prediction.

## Consequences

- **Positive:** the calibration plot measures what each thesis actually
  claimed; untraded theses count; trade exits can be tuned without
  touching the eval.
- **Negative:** grades arrive only after the window ends (a 55-day thesis
  takes 55 days). The re-grade flipped 10 of 24 outcomes (7 miss→hit, 3
  hit→miss) — the honest numbers are still worse than a coin flip
  (autonomous hit rate 32%, Brier 0.289), and are reported as such.
- **Reversibility:** Medium: the old values are in `notes` and the backup.

## References

- Related: [[ADR-0004]] (per-bucket scoring), [[ADR-0003]] (deterministic risk).
- `app/portfolio/resolver.py`, `app/portfolio/regrade.py`.
