# ADR-0003: Deterministic risk + sizing, never gated on LLM confidence

- **Date:** 2026-06-05
- **Status:** accepted
- **Phase:** 7 (autonomous shadow mode)

## Context

Every thesis the system produces carries a `confidence` field — the LLM's
self-reported probability that its directional call is correct. The
question: should this number be allowed to drive consequential decisions
(does the trade place, how many contracts, into which account)?

Two opposing forces:

- LLM confidence is the easiest possible signal to use. It's a single
  scalar; you can write `if confidence >= 0.7: place_trade(size=...)` in
  one line.
- LLM confidence is also uncalibrated. Models tend to overclaim certainty,
  especially when reasoning over numbers (this project's domain). Without
  forward-tested data on the specific model + prompt combo, "0.85" means
  whatever the model wants it to mean — and shifts as the model is
  upgraded, the prompt is tuned, or the data pipeline changes.

The post-GOOG-hallucination incident (Phase 4.5) reinforced this: the
model returned schema-valid, confidently-stated output with fabricated
revenue numbers and an over-budget contract. Trusting the model's own
"I'm confident" claim is the failure mode the project is most exposed to.

## Decision

LLM confidence is informational only. Trade execution, sizing, and
account routing are decided by deterministic, auditable code in
`app/portfolio/risk.py` (the `RiskEngine`), with inputs that are either
config-driven constants or live portfolio state — never the LLM's own
self-assessment as the gate.

The LLM produces theses. Code decides whether they trade.

Concretely:

- **Account gates** are pure config:
  `min_confidence`, `max_trade_cost_usd`, `max_trades_per_day`,
  `max_concurrent_positions`, `kill_switch`. Same thesis hits both the
  conservative and aggressive gates independently; either, neither, or
  both can approve.
- **Sizing** is `min(max_trade_cost_usd, max_risk_usd_from_thesis)`.
  The LLM proposes a contract count; code clamps it.
- **Outcome** of every approval/rejection is written inline on the
  `ShadowTrade` row as `risk_reason`, so an audit of any trade traces
  back to the exact rule that fired.
- **Confidence is logged** alongside every thesis and used in the eval
  harness for calibration scoring (ADR-0004). It is *measured*, not
  *trusted*.

## Alternatives considered

- **Gate on confidence** (e.g. `place if confidence >= 0.7`). Rejected:
  amplifies the uncalibrated-scalar failure mode. Means the model can
  hallucinate itself into a trade by being suitably confident in its
  hallucination.
- **Use a calibration multiplier** (scale confidence by historical hit
  rate per bucket). Tempting, but requires the calibration data to
  exist before any trade can be placed — chicken/egg. Worse, it bakes
  the LLM's self-report into the loop as a load-bearing signal.
- **Let the LLM decide sizing** (have the model output a contract count
  and trust it). Rejected: code already has to enforce the budget cap
  to avoid the GOOG-style "11× over budget" failure mode, so the LLM's
  count is already a suggestion, not a directive — formalising that.
- **Don't do shadow mode; only manual copilot**. Cuts the autonomous
  half of the project (DESIGN.md §1). Rejected as too narrow a scope.

## Consequences

- **Positive:**
  - The system has one clearly LLM-trusting boundary (the thesis
    content + grounding check) and one clearly code-trusting boundary
    (everything after). The split is auditable, testable, and
    explainable in a 30-second interview answer.
  - The conservative/aggressive A/B test (DESIGN.md §8) becomes a clean
    experiment on whether stated confidence translates to a sizing
    edge — exactly what the eval harness scores.
  - Adding new risk rules is a code change in one file, not a prompt
    change in many.
- **Negative:**
  - The system can ignore a high-confidence call if it doesn't fit the
    account gates. That's the point, but it means some "good" theses
    will be no-traded — which is fine because the eval harness still
    scores the *thesis*, separate from whether it shadow-traded.
  - Risk rules drift from the design doc over time. Mitigated by
    persisting `risk_reason` per trade so any audit shows exactly what
    rule fired.
- **Reversibility:** High. Allowing LLM confidence to gate execution
  would be a one-function change. The current design is the cautious
  default, not a one-way door.

## References

- Related ADRs: [[ADR-0004-per-bucket-eval]], [[ADR-0005-paper-only-forever]]
- DESIGN.md §2 ("Evaluate, don't trust"), §8 (deterministic risk).
- README "What I learned" — Phase 4.5 (GOOG hallucination), Phase 7 (shadow trades, deterministic risk).
- `app/portfolio/risk.py` — the engine itself.
