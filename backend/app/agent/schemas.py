"""Thesis schemas (DESIGN.md §4 step 4 + §7 typed thesis object).

Two layers:

- `ThesisDraft` — what the LLM produces. Used as Gemini's `response_schema`
  in complete_structured(). Contains only the "creative" fields the model
  decides: direction, confidence, reasoning, suggested contract.

- `Thesis` — `ThesisDraft` plus server-side bookkeeping: correlation id,
  source bucket, generated_at, grounding result, LLM provider/model, funnel
  latency. This is what gets persisted to the eval DB.

The Pydantic `Thesis` maps 1-to-1 onto the SQLAlchemy `eval.models.Thesis`
ORM row via `to_orm_kwargs()`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ThesisDirection = Literal["long", "short"]
SourceBucket = Literal["manual", "reactive", "catalyst"]
OptionTypeLit = Literal["call", "put"]


class SuggestedContract(BaseModel):
    """The specific options trade the thesis recommends.

    Filled from real tool calls (e.g. `get_options_chain`). The orchestrator
    additionally verifies that this contract appears in a chain it actually
    fetched (so the model can't hallucinate a contract that doesn't exist).
    """

    underlying: str
    occ_symbol: str
    option_type: OptionTypeLit
    strike: Decimal
    expiration: date
    estimated_premium_per_contract: Decimal = Field(
        description="Price per contract in dollars (per-share, multiply by 100 for total)."
    )
    contracts: int = Field(ge=1, description="How many contracts to buy.")
    max_risk_usd: Decimal = Field(
        ge=0,
        description=(
            "Total dollar risk if every contract expires worthless. "
            "= estimated_premium_per_contract * contracts * 100."
        ),
    )

    # === Exit rules (the paper engine in Phase 7 honors these) ===
    exit_if_underlying_below: Decimal | None = Field(
        default=None,
        description=(
            "For long calls: stop-loss trigger if the UNDERLYING closes below this. "
            "Optional but recommended."
        ),
    )
    exit_if_underlying_above: Decimal | None = Field(
        default=None,
        description="For long puts: stop-loss trigger if the underlying closes above this.",
    )
    close_n_days_before_expiry: int | None = Field(
        default=None,
        ge=0,
        le=60,
        description=(
            "Time-based exit. Close the position this many days before expiry "
            "to avoid late-stage theta decay. Recommended: 5-7 for monthlies."
        ),
    )

    @field_validator("underlying", "occ_symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("expiration")
    @classmethod
    def _no_past_expiration(cls, v: date) -> date:
        today = datetime.now(UTC).date()
        if v < today:
            raise ValueError(
                f"expiration {v} is in the past (today is {today}). "
                f"Pick a contract from a fetched chain."
            )
        return v

    @model_validator(mode="after")
    def _check_max_risk_matches_premium(self) -> SuggestedContract:
        """max_risk_usd should equal premium x contracts x 100. Allow 1% tolerance."""
        expected = self.estimated_premium_per_contract * Decimal(self.contracts) * Decimal(100)
        if self.max_risk_usd <= 0:
            return self
        # Tolerance: 1% relative or $1 absolute (rounding slack)
        diff = abs(self.max_risk_usd - expected)
        if expected > 0 and diff > max(Decimal("1.0"), expected * Decimal("0.01")):
            raise ValueError(
                f"max_risk_usd ({self.max_risk_usd}) should equal premium x contracts x 100 "
                f"({self.estimated_premium_per_contract} x {self.contracts} x 100 = {expected})"
            )
        return self


class ThesisDraft(BaseModel):
    """The LLM-produced part of a thesis (Gemini's response_schema target)."""

    symbol: str
    direction: ThesisDirection
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Stated confidence in [0,1]. NOT used to gate execution (DESIGN.md §2.5).",
    )
    reasoning: str = Field(
        min_length=20,
        description=(
            "The case for this thesis, in plain English. Cite specific numbers "
            "from the tools you called. Every cited number is verified by the "
            "grounding check."
        ),
    )
    prediction_window_days: int = Field(
        ge=1,
        le=365,
        description="Number of days until the thesis should be evaluated.",
    )
    suggested_contract: SuggestedContract
    what_must_happen: str = Field(
        min_length=10,
        description=(
            "A specific, falsifiable statement of what makes this thesis right. "
            "Used later to objectively score the outcome."
        ),
    )

    @field_validator("symbol")
    @classmethod
    def _upper_symbol(cls, v: str) -> str:
        return v.upper()


class Thesis(ThesisDraft):
    """A full thesis with server-side bookkeeping. This is what gets persisted."""

    correlation_id: str
    source_bucket: SourceBucket
    generated_at: datetime
    grounding_check_passed: bool
    grounding_notes: str | None = Field(
        default=None,
        description="If grounding had issues, this records what was unverifiable.",
    )
    llm_provider: str
    llm_model: str
    funnel_latency_ms: int = Field(ge=0)

    def to_orm_kwargs(self) -> dict[str, Any]:
        """Map to keyword args for the SQLAlchemy `Thesis` ORM model."""
        return {
            "correlation_id": self.correlation_id,
            "source_bucket": self.source_bucket,
            "symbol": self.symbol,
            "generated_at": self.generated_at,
            "direction": self.direction,
            "confidence": self.confidence,
            "prediction_window_days": self.prediction_window_days,
            "reasoning": self.reasoning,
            "suggested_contract": self.suggested_contract.model_dump(mode="json"),
            "grounding_check_passed": self.grounding_check_passed,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "funnel_latency_ms": self.funnel_latency_ms,
        }
