"""Portfolio ORM models (DESIGN.md §8).

Two paper accounts: conservative + aggressive. Their contrast IS the feature
— same theses, different risk gates, different outcomes — and the cleanest
test of whether LLM confidence translates to a sizing edge.

Phase 7 MVP only models `ShadowTrade` (the "what would have been traded"
record). Position tracking + mark-to-market + outcome resolution arrive in
the next session.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.eval.models import Base
from app.eval.models import Thesis as ThesisRow


class AccountKind(StrEnum):
    CONSERVATIVE = "conservative"
    AGGRESSIVE = "aggressive"


class ShadowTradeStatus(StrEnum):
    SHADOW_OPEN = "shadow_open"  # would be open if real
    SHADOW_CLOSED = "shadow_closed"  # would be closed (expired or sold)


class PaperAccount(Base):
    """One paper account. The kind uniquely identifies it (singleton-per-kind)."""

    __tablename__ = "paper_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))

    # Balances (Alpaca paper default = $100K)
    starting_balance_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    equity_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2))

    # Risk configuration (DESIGN.md §8, scoped to single-leg options)
    min_confidence: Mapped[float] = mapped_column(Float)
    max_trade_cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    max_trades_per_day: Mapped[int] = mapped_column(Integer)
    max_concurrent_positions: Mapped[int] = mapped_column(Integer)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    shadow_trades: Mapped[list[ShadowTrade]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class ShadowTrade(Base):
    """A trade that WOULD have been placed if the account were live.

    Records the full snapshot at decision time so we can score outcomes
    objectively later, even if the underlying contract data drifts.
    """

    __tablename__ = "shadow_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="CASCADE"), index=True
    )
    thesis_id: Mapped[int] = mapped_column(ForeignKey("theses.id"), index=True)

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    # Contract snapshot
    underlying: Mapped[str] = mapped_column(String(16), index=True)
    occ_symbol: Mapped[str] = mapped_column(String(32))
    option_type: Mapped[str] = mapped_column(String(8))  # call | put
    strike: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    expiration: Mapped[date] = mapped_column(Date, index=True)

    # Sizing
    contracts: Mapped[int] = mapped_column(Integer)
    premium_per_contract_usd: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    total_cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2))

    # Bookkeeping
    status: Mapped[str] = mapped_column(String(16), default=ShadowTradeStatus.SHADOW_OPEN)
    risk_reason: Mapped[str] = mapped_column(Text)

    # Close state — populated by OutcomeResolver when the position resolves.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    close_price_per_contract_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True
    )
    realized_pnl_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)

    account: Mapped[PaperAccount] = relationship(back_populates="shadow_trades")
    thesis: Mapped[ThesisRow] = relationship()
    marks: Mapped[list[PositionMark]] = relationship(
        back_populates="shadow_trade", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("idx_shadow_trades_account_opened", "account_id", "opened_at"),)


class CloseReason(StrEnum):
    """Why a shadow trade closed. Drives downstream P&L attribution + eval."""

    EXPIRED = "expired"  # contract reached expiration
    EXIT_UNDERLYING_BELOW = "exit_underlying_below"  # long-call stop-loss tripped
    EXIT_UNDERLYING_ABOVE = "exit_underlying_above"  # long-put stop-loss tripped
    THETA_EXIT = "theta_exit"  # close_n_days_before_expiry rule
    MANUAL = "manual"  # operator-closed (kill switch, etc.)


class PositionMark(Base):
    """One mark-to-market snapshot for a shadow trade.

    History-preserving (append-only): each tick writes a new row instead of
    overwriting. Lets us plot equity curve over time, and lets the resolver
    look back at the mark at any past moment.
    """

    __tablename__ = "position_marks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shadow_trade_id: Mapped[int] = mapped_column(
        ForeignKey("shadow_trades.id", ondelete="CASCADE"), index=True
    )
    marked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    underlying_price_usd: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    # Option mid (bid+ask)/2 from a real chain quote; null if the provider
    # didn't return a usable bid/ask and we fell back to a fair-value model.
    option_mid_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    # Per-contract dollar value used for this mark (mid if available, else
    # fair-value estimate). Multiplied by contracts * 100 for total P&L.
    mark_price_per_contract_usd: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    unrealized_pnl_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2))

    shadow_trade: Mapped[ShadowTrade] = relationship(back_populates="marks")

    __table_args__ = (Index("idx_position_marks_trade_marked", "shadow_trade_id", "marked_at"),)
