"""Deterministic risk engine for the paper portfolio (DESIGN.md §2.5, §8).

NO LLM in this layer. Every decision is plain Python that an auditor can
read and replay. The LLM produces theses; the RiskEngine decides whether
they trade, and at what size.

Inputs:
- A Thesis (the LLM's output: direction, confidence, suggested_contract)
- A PaperAccount (which carries the risk config: min_confidence, caps, kill switch)
- Live state: trades placed today on this account, open-position count

Output:
- A RiskDecision: approved/rejected + sizing (possibly < the thesis suggested
  if we needed to cap to budget) + a human-readable reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.agent.schemas import Thesis
from app.portfolio.models import PaperAccount


@dataclass(frozen=True)
class RiskDecision:
    """The verdict for one (thesis, account) pair."""

    approved: bool
    contracts: int  # number of contracts to trade; 0 if rejected
    total_cost_usd: Decimal  # contracts x premium x 100; 0 if rejected
    reason: str  # always populated, for both approval and rejection


@dataclass(frozen=True)
class AccountState:
    """Live state the RiskEngine needs beyond the account config."""

    trades_today: int
    open_positions: int


_CONTRACT_MULTIPLIER = Decimal(100)


class RiskEngine:
    """Pure-function risk gate. Same input → same output, every time."""

    @staticmethod
    def evaluate(
        thesis: Thesis,
        account: PaperAccount,
        state: AccountState,
    ) -> RiskDecision:
        if account.kill_switch:
            return RiskDecision(
                approved=False,
                contracts=0,
                total_cost_usd=Decimal(0),
                reason=f"Account {account.kind!r} kill switch is on.",
            )

        if state.trades_today >= account.max_trades_per_day:
            return RiskDecision(
                approved=False,
                contracts=0,
                total_cost_usd=Decimal(0),
                reason=(
                    f"Daily trade cap reached: {state.trades_today}/{account.max_trades_per_day} "
                    f"on {account.kind!r}."
                ),
            )

        if state.open_positions >= account.max_concurrent_positions:
            return RiskDecision(
                approved=False,
                contracts=0,
                total_cost_usd=Decimal(0),
                reason=(
                    f"Concurrent-position cap reached: "
                    f"{state.open_positions}/{account.max_concurrent_positions}."
                ),
            )

        if thesis.confidence < account.min_confidence:
            return RiskDecision(
                approved=False,
                contracts=0,
                total_cost_usd=Decimal(0),
                reason=(
                    f"Confidence {thesis.confidence:.2f} below {account.kind!r} "
                    f"threshold {account.min_confidence:.2f}."
                ),
            )

        # Determine sizing. Start with the thesis's suggested contracts;
        # cap to budget if needed; reject if 1 contract alone exceeds budget.
        contract = thesis.suggested_contract
        premium = contract.estimated_premium_per_contract  # per-share dollars
        if premium <= 0:
            return RiskDecision(
                approved=False,
                contracts=0,
                total_cost_usd=Decimal(0),
                reason=f"Premium {premium} is non-positive — refusing to trade.",
            )

        cost_per_contract = premium * _CONTRACT_MULTIPLIER
        max_affordable = int(account.max_trade_cost_usd // cost_per_contract)
        if max_affordable < 1:
            return RiskDecision(
                approved=False,
                contracts=0,
                total_cost_usd=Decimal(0),
                reason=(
                    f"Even one contract costs ${cost_per_contract:.2f}, exceeding the "
                    f"{account.kind!r} per-trade cap of ${account.max_trade_cost_usd:.2f}."
                ),
            )

        suggested = max(1, contract.contracts)
        approved_contracts = min(suggested, max_affordable)
        total_cost = cost_per_contract * Decimal(approved_contracts)

        reason_parts = [
            f"Confidence {thesis.confidence:.2f} ≥ {account.kind!r} threshold "
            f"{account.min_confidence:.2f}.",
            f"Total cost ${total_cost:.2f} ≤ cap ${account.max_trade_cost_usd:.2f}.",
        ]
        if approved_contracts < suggested:
            reason_parts.append(
                f"Sized down from {suggested} to {approved_contracts} contracts to fit budget."
            )
        return RiskDecision(
            approved=True,
            contracts=approved_contracts,
            total_cost_usd=total_cost,
            reason=" ".join(reason_parts),
        )
