# ADR-0005: Paper trading only, forever — no real-money execution path

- **Date:** 2026-06-05
- **Status:** accepted
- **Phase:** all

## Context

The project simulates trading decisions and records what would have
happened — `ShadowTrade` rows, mark-to-market history, eval outcomes.
A natural follow-on instinct is to add a real-money execution path:
"the eval shows the catalyst bucket has edge; let's wire Alpaca's live
trading endpoint behind a confidence threshold."

The argument for the wiring is straightforward (the system already
produces the signal; placing the order is a small last step). The
argument against is the entire reason the project exists in its
current form.

## Decision

Real-money execution is permanently out of scope. The system simulates;
it does not place orders. No live brokerage endpoint is wired, no
order-routing code exists in the repository, and a human-in-the-loop
approval gate is *not* a sufficient safeguard to relax this — because
once the wiring exists, the gate becomes the only thing standing
between a bug and lost money.

This decision is not phased ("paper now, real later"). It is permanent.

## Alternatives considered

- **Real money with manual approval**. A "review-and-click-yes" gate
  in front of every order. Rejected: the gate is a thin layer of
  attention vs a hard architectural boundary. Attention fails. The
  boundary doesn't.
- **Real money with a small fixed budget**. "I'll only ever fund this
  with $200." Rejected: the engineering risks (credentials, key
  rotation, audit trail) are not proportional to the size of the
  funding. The interesting questions about the system don't get
  better answered with $200 of real money than with $100K of paper.
- **Paper trading API rather than self-simulated paper engine**. We
  already use Alpaca's paper account for paper-side workflows
  (credentials, account context). The self-simulated engine is what
  produces the audit trail and the eval-harness-friendly state. The
  external paper API would be an additional dependency for the same
  feature. Rejected on tech-debt grounds.

## Consequences

- **Positive:**
  - The disclaimer in the README is unambiguous and stays unambiguous.
    No "we plan to add live trading" footnote ever needs to be written.
  - Code review surface is much smaller — no key management, no order
    state machine, no idempotency-against-the-broker concerns, no
    PII/KYC implications.
  - The evaluation is the product. The thing being judged is the
    quality of the thesis pipeline, not its tradeable signal — and
    the eval harness measures exactly that (ADR-0004). Resume claim
    stays "I built and measured a research system," not "I built a
    trading bot."
  - The "what NOT to do" list in DESIGN.md §6 stays load-bearing.
- **Negative:**
  - The system will never know whether its measured catalyst-bucket
    edge survives real-market frictions (slippage, fill quality,
    execution latency). That's a real epistemic limit, accepted on
    purpose.
  - Excludes a flashier resume-day-1 demo ("watch it trade live") in
    favour of a less flashy one ("watch it reason live").
- **Reversibility:** Low, intentionally. Reversing would require new
  code, a new ADR superseding this one, and a new threat model.

## References

- Related ADRs: [[ADR-0001-architecture-overview]],
  [[ADR-0003-deterministic-risk]].
- DESIGN.md §1, §2.2, §6.
- README disclaimer.
