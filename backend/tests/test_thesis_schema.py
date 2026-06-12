"""Thesis schema validation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.agent.schemas import SuggestedContract, Thesis, ThesisDraft


def _good_contract() -> SuggestedContract:
    # Far-future expiration so the past-date validator never trips on this fixture.
    return SuggestedContract(
        underlying="aapl",  # lowercase to test the upper validator
        occ_symbol="aapl301220c00150000",
        option_type="call",
        strike=Decimal("150"),
        expiration=date(2030, 12, 20),
        estimated_premium_per_contract=Decimal("5.10"),
        contracts=1,
        max_risk_usd=Decimal("510"),
    )


def _good_draft() -> ThesisDraft:
    return ThesisDraft(
        symbol="aapl",
        direction="long",
        confidence=0.65,
        reasoning="AAPL beat earnings by 12% with raised guidance, this is bullish.",
        prediction_window_days=30,
        suggested_contract=_good_contract(),
        what_must_happen="AAPL must close above $160 within 30 days.",
    )


def test_contract_uppercases_symbols() -> None:
    c = _good_contract()
    assert c.underlying == "AAPL"
    assert c.occ_symbol == "AAPL301220C00150000"


def test_draft_uppercases_symbol() -> None:
    d = _good_draft()
    assert d.symbol == "AAPL"


def test_draft_rejects_out_of_range_confidence() -> None:
    with pytest.raises(ValueError):
        ThesisDraft(
            symbol="AAPL",
            direction="long",
            confidence=1.5,
            reasoning="X" * 30,
            prediction_window_days=30,
            suggested_contract=_good_contract(),
            what_must_happen="X" * 20,
        )


def test_draft_rejects_too_short_reasoning() -> None:
    with pytest.raises(ValueError):
        ThesisDraft(
            symbol="AAPL",
            direction="long",
            confidence=0.5,
            reasoning="short",
            prediction_window_days=30,
            suggested_contract=_good_contract(),
            what_must_happen="X" * 20,
        )


def test_thesis_to_orm_kwargs_has_expected_fields() -> None:
    t = Thesis(
        **_good_draft().model_dump(),
        correlation_id="abc123",
        source_bucket="manual",
        generated_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        grounding_check_passed=True,
        llm_provider="gemini",
        llm_model="gemini-2.5-flash",
        funnel_latency_ms=4500,
    )
    kwargs = t.to_orm_kwargs()
    assert kwargs["correlation_id"] == "abc123"
    assert kwargs["source_bucket"] == "manual"
    assert kwargs["symbol"] == "AAPL"
    assert kwargs["direction"] == "long"
    assert kwargs["confidence"] == 0.65
    assert kwargs["grounding_check_passed"] is True
    assert kwargs["llm_provider"] == "gemini"
    assert kwargs["funnel_latency_ms"] == 4500
    sc = kwargs["suggested_contract"]
    assert sc["occ_symbol"] == "AAPL301220C00150000"
    assert isinstance(sc["strike"], (str, float, int))


def test_contract_validates_nonneg_max_risk() -> None:
    with pytest.raises(ValueError):
        SuggestedContract(
            underlying="AAPL",
            occ_symbol="AAPL301220C00150000",
            option_type="call",
            strike=Decimal("150"),
            expiration=date(2030, 12, 20),
            estimated_premium_per_contract=Decimal("5"),
            contracts=1,
            max_risk_usd=Decimal("-1"),
        )


def test_contract_validates_min_contracts() -> None:
    with pytest.raises(ValueError):
        SuggestedContract(
            underlying="AAPL",
            occ_symbol="AAPL301220C00150000",
            option_type="call",
            strike=Decimal("150"),
            expiration=date(2030, 12, 20),
            estimated_premium_per_contract=Decimal("5"),
            contracts=0,
            max_risk_usd=Decimal("0"),
        )


def test_contract_rejects_past_expiration() -> None:
    """The GOOG hallucination picked a 2024 expiration. Now blocked."""
    with pytest.raises(ValueError, match="past"):
        SuggestedContract(
            underlying="AAPL",
            occ_symbol="AAPL240920C00150000",
            option_type="call",
            strike=Decimal("150"),
            expiration=date(2024, 9, 20),
            estimated_premium_per_contract=Decimal("5"),
            contracts=1,
            max_risk_usd=Decimal("500"),
        )


def test_contract_rejects_max_risk_inconsistent_with_premium() -> None:
    """The GOOG hallucination claimed max_risk=$5500 but premium math also worked,
    so this specific check is about catching cases where the model bungled the math."""
    with pytest.raises(ValueError, match="premium"):
        SuggestedContract(
            underlying="AAPL",
            occ_symbol="AAPL301220C00150000",
            option_type="call",
            strike=Decimal("150"),
            expiration=date(2030, 12, 20),
            estimated_premium_per_contract=Decimal("5"),  # x 1 x 100 = $500
            contracts=1,
            max_risk_usd=Decimal("9999"),  # WRONG
        )


def test_contract_accepts_exit_if_fields() -> None:
    c = SuggestedContract(
        underlying="AAPL",
        occ_symbol="AAPL301220C00150000",
        option_type="call",
        strike=Decimal("150"),
        expiration=date(2030, 12, 20),
        estimated_premium_per_contract=Decimal("5"),
        contracts=1,
        max_risk_usd=Decimal("500"),
        exit_if_underlying_below=Decimal("145"),
        close_n_days_before_expiry=7,
    )
    assert c.exit_if_underlying_below == Decimal("145")
    assert c.close_n_days_before_expiry == 7
