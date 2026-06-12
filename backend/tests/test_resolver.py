"""OutcomeResolver tests — synthetic time/prices, asserts thesis-outcome rows."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.data.types import OHLCBar, OptionContract, Quote
from app.eval.models import Base, RealizedDirection, ThesisOutcome
from app.eval.models import Thesis as ThesisRow
from app.portfolio.models import (
    AccountKind,
    CloseReason,
    PaperAccount,
    ShadowTrade,
    ShadowTradeStatus,
)
from app.portfolio.resolver import (
    OutcomeResolver,
    _choose_close_reason,
    _classify_direction,
)


class FakeOptions:
    name = "fake-opt"

    async def get_expirations(self, symbol: str) -> list[date]:
        raise NotImplementedError

    async def get_chain(self, symbol: str, expiration: date) -> list[OptionContract]:
        raise NotImplementedError

    async def get_contract_quote(self, occ_symbol: str) -> OptionContract:
        # Default — used by stop-loss / theta close paths (not by EXPIRED).
        return OptionContract(
            symbol="AAPL",
            occ_symbol=occ_symbol,
            expiration=date(2026, 8, 21),
            strike=Decimal("350"),
            option_type="call",
            bid=Decimal("2.0"),
            ask=Decimal("2.2"),
            last=None,
        )


class FakePrices:
    name = "fake-px"

    def __init__(self, last: float = 360.0, ohlc_close: float | None = 310.0) -> None:
        self.last = last
        self.ohlc_close = ohlc_close

    async def get_latest_quote(self, symbol: str) -> Quote:
        return Quote(
            symbol=symbol,
            bid=Decimal(str(self.last - 0.05)),
            ask=Decimal(str(self.last + 0.05)),
            last=Decimal(str(self.last)),
            timestamp=datetime.now(UTC),
        )

    async def get_ohlc(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "1Day"
    ) -> list[OHLCBar]:
        if self.ohlc_close is None:
            return []
        # Return one daily bar at the start of the window.
        return [
            OHLCBar(
                symbol=symbol,
                timestamp=start,
                open=Decimal(str(self.ohlc_close)),
                high=Decimal(str(self.ohlc_close)),
                low=Decimal(str(self.ohlc_close)),
                close=Decimal(str(self.ohlc_close)),
                volume=1_000_000,
            )
        ]


@pytest.fixture
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_open_trade(
    factory: async_sessionmaker,
    *,
    expiration: date,
    opened_at: datetime | None = None,
    option_type: str = "call",
    strike: float = 350.0,
    premium: float = 3.88,
    direction: str = "long",
    suggested_contract: dict[str, Any] | None = None,
    confidence: float = 0.7,
) -> int:
    opened_at = opened_at or datetime.now(UTC) - timedelta(days=30)
    async with factory() as session:
        account = (
            (
                await session.execute(
                    select(PaperAccount).where(PaperAccount.kind == AccountKind.AGGRESSIVE.value)
                )
            )
            .scalars()
            .first()
        )
        if account is None:
            account = PaperAccount(
                kind=AccountKind.AGGRESSIVE.value,
                name="Aggressive",
                starting_balance_usd=Decimal("100000"),
                equity_usd=Decimal("100000"),
                min_confidence=0.5,
                max_trade_cost_usd=Decimal("500"),
                max_trades_per_day=8,
                max_concurrent_positions=10,
                kill_switch=False,
                created_at=datetime.now(UTC),
            )
            session.add(account)
            await session.flush()
        thesis = ThesisRow(
            correlation_id="x" * 16,
            source_bucket="manual",
            symbol="AAPL",
            generated_at=opened_at,
            direction=direction,
            confidence=confidence,
            prediction_window_days=30,
            reasoning="test",
            suggested_contract=suggested_contract or {},
            grounding_check_passed=True,
            llm_provider="gemini",
            llm_model="gemini-2.5-flash",
            funnel_latency_ms=5000,
        )
        session.add(thesis)
        await session.flush()
        trade = ShadowTrade(
            account_id=account.id,
            thesis_id=thesis.id,
            opened_at=opened_at,
            underlying="AAPL",
            occ_symbol="AAPL260821C00350000",
            option_type=option_type,
            strike=Decimal(str(strike)),
            expiration=expiration,
            contracts=1,
            premium_per_contract_usd=Decimal(str(premium)),
            total_cost_usd=Decimal(str(premium * 100)),
            status=ShadowTradeStatus.SHADOW_OPEN.value,
            risk_reason="test",
        )
        session.add(trade)
        await session.commit()
        return int(trade.id)


# === _classify_direction ===


def test_classify_long_when_pct_above_flat_band() -> None:
    assert _classify_direction(2.0) == RealizedDirection.LONG.value


def test_classify_short_when_pct_below_flat_band() -> None:
    assert _classify_direction(-2.0) == RealizedDirection.SHORT.value


def test_classify_flat_inside_band() -> None:
    assert _classify_direction(0.3) == RealizedDirection.FLAT.value
    assert _classify_direction(-0.3) == RealizedDirection.FLAT.value


# === _choose_close_reason ===


def test_expired_takes_precedence_over_other_reasons() -> None:
    trade = _make_trade_stub(expiration=date(2020, 1, 1), option_type="call")
    reason = _choose_close_reason(trade, None, today=date(2026, 1, 2), underlying_now=Decimal(100))
    assert reason == CloseReason.EXPIRED


def test_stop_loss_call_triggers_when_underlying_drops_below_floor() -> None:
    trade = _make_trade_stub(expiration=date(2030, 1, 1), option_type="call")
    thesis = _make_thesis_stub(suggested_contract={"exit_if_underlying_below": 320})
    reason = _choose_close_reason(
        trade, thesis, today=date(2026, 1, 2), underlying_now=Decimal("310")
    )
    assert reason == CloseReason.EXIT_UNDERLYING_BELOW


def test_stop_loss_call_no_trigger_when_above_floor() -> None:
    trade = _make_trade_stub(expiration=date(2030, 1, 1), option_type="call")
    thesis = _make_thesis_stub(suggested_contract={"exit_if_underlying_below": 320})
    reason = _choose_close_reason(
        trade, thesis, today=date(2026, 1, 2), underlying_now=Decimal("330")
    )
    assert reason is None


def test_theta_exit_triggers_within_window() -> None:
    expiry = date(2026, 1, 10)
    trade = _make_trade_stub(expiration=expiry, option_type="call")
    thesis = _make_thesis_stub(suggested_contract={"close_n_days_before_expiry": 5})
    reason = _choose_close_reason(
        trade, thesis, today=date(2026, 1, 7), underlying_now=Decimal("350")
    )
    assert reason == CloseReason.THETA_EXIT


# === resolve_all ===


@pytest.mark.asyncio
async def test_expired_call_in_the_money_closes_with_intrinsic_value(db_factory) -> None:
    trade_id = await _seed_open_trade(
        db_factory, expiration=date(2026, 6, 1)
    )  # already past at "now"
    resolver = OutcomeResolver(
        options_provider=FakeOptions(),
        price_provider=FakePrices(last=360.0, ohlc_close=310.0),
        session_factory=db_factory,
    )
    result = await resolver.resolve_all(now=datetime(2026, 6, 2, tzinfo=UTC))
    assert result.closed == 1
    assert result.outcomes_written == 1

    async with db_factory() as session:
        trade = await session.get(ShadowTrade, trade_id)
        outcome = (
            await session.execute(select(ThesisOutcome).where(ThesisOutcome.thesis_id == 1))
        ).scalar_one()

    assert trade is not None
    assert trade.status == ShadowTradeStatus.SHADOW_CLOSED.value
    assert trade.close_reason == CloseReason.EXPIRED.value
    # Strike 350, underlying 360 → intrinsic $10.
    assert trade.close_price_per_contract_usd == Decimal("10")
    # Realized P&L: (10 - 3.88) * 100 * 1 = $612
    assert trade.realized_pnl_usd == Decimal("612.00")
    # Underlying went 310 → 360 = +16.13% → LONG
    assert outcome.realized_direction == RealizedDirection.LONG.value
    assert outcome.hit is True  # thesis was long, underlying went long
    assert outcome.pct_move > 15


@pytest.mark.asyncio
async def test_expired_call_out_of_the_money_closes_at_zero(db_factory) -> None:
    await _seed_open_trade(db_factory, expiration=date(2026, 6, 1), strike=400.0)
    resolver = OutcomeResolver(
        options_provider=FakeOptions(),
        price_provider=FakePrices(last=340.0, ohlc_close=310.0),
        session_factory=db_factory,
    )
    result = await resolver.resolve_all(now=datetime(2026, 6, 2, tzinfo=UTC))
    assert result.closed == 1

    async with db_factory() as session:
        trade = (await session.execute(select(ShadowTrade))).scalar_one()
        outcome = (await session.execute(select(ThesisOutcome))).scalar_one()
    # Strike 400, underlying 340 → intrinsic 0.
    assert trade.close_price_per_contract_usd == Decimal("0")
    # Lost the entire premium.
    assert trade.realized_pnl_usd == Decimal("-388.00")
    # Underlying still went up though.
    assert outcome.realized_direction == RealizedDirection.LONG.value
    assert outcome.hit is True  # thesis (long) matches underlying direction


@pytest.mark.asyncio
async def test_unresolved_trade_skipped(db_factory) -> None:
    """A trade nowhere near expiration with no exit triggers stays open."""
    await _seed_open_trade(db_factory, expiration=date(2030, 1, 1), suggested_contract={})
    resolver = OutcomeResolver(
        options_provider=FakeOptions(),
        price_provider=FakePrices(last=320.0),
        session_factory=db_factory,
    )
    result = await resolver.resolve_all(now=datetime(2026, 6, 2, tzinfo=UTC))
    assert result.closed == 0
    assert result.outcomes_written == 0


@pytest.mark.asyncio
async def test_resolver_skips_when_open_price_unavailable(db_factory) -> None:
    """If we can't get the underlying-at-open price, refuse to write a bogus outcome."""
    await _seed_open_trade(db_factory, expiration=date(2026, 6, 1))
    resolver = OutcomeResolver(
        options_provider=FakeOptions(),
        price_provider=FakePrices(last=360.0, ohlc_close=None),  # OHLC returns []
        session_factory=db_factory,
    )
    result = await resolver.resolve_all(now=datetime(2026, 6, 2, tzinfo=UTC))
    # No outcomes written despite the trade being technically resolvable.
    assert result.outcomes_written == 0


# === Helpers for the pure-function tests ===


def _make_trade_stub(
    expiration: date, option_type: str = "call", strike: float = 350.0
) -> ShadowTrade:
    return ShadowTrade(
        account_id=1,
        thesis_id=1,
        opened_at=datetime.now(UTC) - timedelta(days=30),
        underlying="AAPL",
        occ_symbol="AAPL260821C00350000",
        option_type=option_type,
        strike=Decimal(str(strike)),
        expiration=expiration,
        contracts=1,
        premium_per_contract_usd=Decimal("3.88"),
        total_cost_usd=Decimal("388"),
        status=ShadowTradeStatus.SHADOW_OPEN.value,
        risk_reason="test",
    )


def _make_thesis_stub(suggested_contract: dict[str, Any]) -> ThesisRow:
    return ThesisRow(
        id=1,
        correlation_id="x" * 16,
        source_bucket="manual",
        symbol="AAPL",
        generated_at=datetime.now(UTC),
        direction="long",
        confidence=0.7,
        prediction_window_days=30,
        reasoning="test",
        suggested_contract=suggested_contract,
        grounding_check_passed=True,
        llm_provider="gemini",
        llm_model="gemini-2.5-flash",
        funnel_latency_ms=5000,
    )
