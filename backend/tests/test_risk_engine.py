"""RiskEngine: tests for every rejection path + happy path."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.agent.schemas import SuggestedContract, Thesis
from app.portfolio.models import AccountKind, PaperAccount
from app.portfolio.risk import AccountState, RiskEngine


def _account(
    *,
    kind: AccountKind = AccountKind.CONSERVATIVE,
    min_confidence: float = 0.7,
    max_trade_cost_usd: Decimal = Decimal("300.00"),
    max_trades_per_day: int = 3,
    max_concurrent_positions: int = 5,
    kill_switch: bool = False,
) -> PaperAccount:
    return PaperAccount(
        id=1,
        kind=kind.value,
        name=f"{kind.value}",
        starting_balance_usd=Decimal("100000"),
        equity_usd=Decimal("100000"),
        min_confidence=min_confidence,
        max_trade_cost_usd=max_trade_cost_usd,
        max_trades_per_day=max_trades_per_day,
        max_concurrent_positions=max_concurrent_positions,
        kill_switch=kill_switch,
        created_at=datetime.now(UTC),
    )


def _thesis(
    *,
    confidence: float = 0.75,
    premium: Decimal = Decimal("2.00"),
    contracts: int = 1,
) -> Thesis:
    exp = date.today() + timedelta(days=30)
    return Thesis(
        symbol="AAPL",
        direction="long",
        confidence=confidence,
        reasoning="x" * 30,
        prediction_window_days=14,
        suggested_contract=SuggestedContract(
            underlying="AAPL",
            occ_symbol="AAPL301220C00150000",
            option_type="call",
            strike=Decimal("150"),
            expiration=exp,
            estimated_premium_per_contract=premium,
            contracts=contracts,
            max_risk_usd=premium * contracts * Decimal(100),
        ),
        what_must_happen="closes above strike",
        correlation_id="x" * 16,
        source_bucket="manual",
        generated_at=datetime.now(UTC),
        grounding_check_passed=True,
        llm_provider="gemini",
        llm_model="gemini-2.5-flash",
        funnel_latency_ms=3000,
    )


def _state(trades_today: int = 0, open_positions: int = 0) -> AccountState:
    return AccountState(trades_today=trades_today, open_positions=open_positions)


def test_happy_path_approves_full_contracts() -> None:
    d = RiskEngine.evaluate(
        thesis=_thesis(confidence=0.8, premium=Decimal("2.00"), contracts=1),
        account=_account(),
        state=_state(),
    )
    assert d.approved is True
    assert d.contracts == 1
    assert d.total_cost_usd == Decimal("200.00")
    assert "threshold" in d.reason


def test_kill_switch_rejects() -> None:
    d = RiskEngine.evaluate(thesis=_thesis(), account=_account(kill_switch=True), state=_state())
    assert d.approved is False
    assert "kill switch" in d.reason.lower()


def test_daily_cap_reached_rejects() -> None:
    d = RiskEngine.evaluate(
        thesis=_thesis(),
        account=_account(max_trades_per_day=3),
        state=_state(trades_today=3),
    )
    assert d.approved is False
    assert "daily trade cap" in d.reason.lower()


def test_concurrent_positions_cap_rejects() -> None:
    d = RiskEngine.evaluate(
        thesis=_thesis(),
        account=_account(max_concurrent_positions=5),
        state=_state(open_positions=5),
    )
    assert d.approved is False
    assert "concurrent-position" in d.reason.lower()


def test_low_confidence_rejects() -> None:
    d = RiskEngine.evaluate(
        thesis=_thesis(confidence=0.55),
        account=_account(min_confidence=0.7),
        state=_state(),
    )
    assert d.approved is False
    assert "confidence" in d.reason.lower()
    assert "0.55" in d.reason
    assert "0.70" in d.reason


def test_aggressive_account_accepts_lower_confidence() -> None:
    d = RiskEngine.evaluate(
        thesis=_thesis(confidence=0.55),
        account=_account(kind=AccountKind.AGGRESSIVE, min_confidence=0.5),
        state=_state(),
    )
    assert d.approved is True


def test_one_contract_too_expensive_rejects() -> None:
    # Premium $20 x 100 = $2,000 per contract; cap is $300.
    d = RiskEngine.evaluate(
        thesis=_thesis(premium=Decimal("20.00"), contracts=1),
        account=_account(max_trade_cost_usd=Decimal("300")),
        state=_state(),
    )
    assert d.approved is False
    assert "exceeding" in d.reason.lower() or "cap" in d.reason.lower()


def test_sizes_down_when_thesis_oversizes() -> None:
    # Premium $1 x 100 = $100/contract; thesis wants 5 contracts ($500); cap is $300.
    d = RiskEngine.evaluate(
        thesis=_thesis(premium=Decimal("1.00"), contracts=5),
        account=_account(max_trade_cost_usd=Decimal("300")),
        state=_state(),
    )
    assert d.approved is True
    assert d.contracts == 3
    assert d.total_cost_usd == Decimal("300.00")
    assert "sized down" in d.reason.lower()


def test_nonpositive_premium_rejects() -> None:
    d = RiskEngine.evaluate(
        thesis=_thesis(premium=Decimal("0.00")),
        account=_account(),
        state=_state(),
    )
    assert d.approved is False
    assert "non-positive" in d.reason.lower()


def test_decision_is_deterministic() -> None:
    """Same inputs → same output. (Smoke test of pure-function discipline.)"""
    args = (_thesis(), _account(), _state())
    a = RiskEngine.evaluate(*args)
    b = RiskEngine.evaluate(*args)
    assert a == b
