"""OutcomeResolver tests — synthetic time and prices.

Two independent jobs (see the resolver docstring):
- trade closing (expiry timing, settlement, exits, P&L), and
- thesis grading over the thesis's own prediction window — for every
  thesis, traded or not, never at the moment a trade's stop-loss fired.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

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
    WINDOW_METHOD,
    OutcomeResolver,
    _choose_close_reason,
    _classify_direction,
    expiry_cutoff,
)

ET = ZoneInfo("America/New_York")


def et(y: int, m: int, d: int, hh: int = 12, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET).astimezone(UTC)


class FakeOptions:
    name = "fake-opt"

    async def get_expirations(self, symbol: str) -> list[date]:
        raise NotImplementedError

    async def get_chain(self, symbol: str, expiration: date) -> list[OptionContract]:
        raise NotImplementedError

    async def get_contract_quote(self, occ_symbol: str) -> OptionContract:
        # Used by stop-loss / theta close paths (not by EXPIRED).
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
    """Daily bars: one per calendar day in the requested window (stamped at
    midnight ET, like Alpaca), close = `daily[day]` or `default_close`.
    5-minute bars: a single bar at the window start when `intraday` is set."""

    name = "fake-px"

    def __init__(
        self,
        *,
        last: float = 360.0,
        default_close: float | None = 310.0,
        daily: dict[date, float] | None = None,
        intraday: float | None = None,
    ) -> None:
        self.last = last
        self.default_close = default_close
        self.daily = daily or {}
        self.intraday = intraday

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
        if timeframe != "1Day":
            return [] if self.intraday is None else [_bar(symbol, start, self.intraday)]
        bars = []
        day = start.astimezone(ET).date()
        while datetime.combine(day, time.min, tzinfo=ET) < end:
            close = self.daily.get(day, self.default_close)
            if close is not None:
                ts = datetime.combine(day, time.min, tzinfo=ET).astimezone(UTC)
                bars.append(_bar(symbol, ts, close))
            day += timedelta(days=1)
        return bars


def _bar(symbol: str, ts: datetime, close: float) -> OHLCBar:
    px = Decimal(str(close))
    return OHLCBar(symbol=symbol, timestamp=ts, open=px, high=px, low=px, close=px, volume=1)


@pytest.fixture
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_thesis(
    factory: async_sessionmaker,
    *,
    generated_at: datetime,
    window_days: int = 7,
    direction: str = "long",
    confidence: float = 0.7,
    suggested_contract: dict[str, Any] | None = None,
) -> int:
    async with factory() as session:
        thesis = ThesisRow(
            correlation_id="x" * 16,
            source_bucket="reactive",
            symbol="AAPL",
            generated_at=generated_at,
            direction=direction,
            confidence=confidence,
            prediction_window_days=window_days,
            reasoning="test",
            suggested_contract=suggested_contract or {},
            grounding_check_passed=True,
            llm_provider="gemini",
            llm_model="gemini-2.5-flash",
            funnel_latency_ms=5000,
        )
        session.add(thesis)
        await session.commit()
        return int(thesis.id)


async def _seed_trade(
    factory: async_sessionmaker,
    *,
    thesis_id: int,
    expiration: date,
    opened_at: datetime,
    kind: str = AccountKind.AGGRESSIVE.value,
    strike: float = 350.0,
    premium: float = 3.88,
) -> int:
    async with factory() as session:
        account = (
            (await session.execute(select(PaperAccount).where(PaperAccount.kind == kind)))
            .scalars()
            .first()
        )
        if account is None:
            account = PaperAccount(
                kind=kind,
                name=kind.title(),
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
        trade = ShadowTrade(
            account_id=account.id,
            thesis_id=thesis_id,
            opened_at=opened_at,
            underlying="AAPL",
            occ_symbol="AAPL260821C00350000",
            option_type="call",
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


async def _outcomes(factory: async_sessionmaker) -> list[ThesisOutcome]:
    async with factory() as session:
        return list((await session.execute(select(ThesisOutcome))).scalars().all())


def _resolver(factory: async_sessionmaker, prices: FakePrices) -> OutcomeResolver:
    return OutcomeResolver(
        options_provider=FakeOptions(), price_provider=prices, session_factory=factory
    )


# === Pure functions ===


def test_classify_direction_bands() -> None:
    assert _classify_direction(1.0) == RealizedDirection.LONG.value
    assert _classify_direction(-1.0) == RealizedDirection.SHORT.value
    assert _classify_direction(0.3) == RealizedDirection.FLAT.value


def _trade_stub(expiration: date) -> ShadowTrade:
    return ShadowTrade(
        account_id=1,
        thesis_id=1,
        opened_at=datetime.now(UTC),
        underlying="AAPL",
        occ_symbol="AAPL260821C00350000",
        option_type="call",
        strike=Decimal("350"),
        expiration=expiration,
        contracts=1,
        premium_per_contract_usd=Decimal("3.88"),
        total_cost_usd=Decimal("388"),
        status=ShadowTradeStatus.SHADOW_OPEN.value,
        risk_reason="test",
    )


def _thesis_stub(contract: dict[str, Any]) -> ThesisRow:
    return ThesisRow(id=1, direction="long", suggested_contract=contract)


def test_expired_takes_precedence_over_other_reasons() -> None:
    reason = _choose_close_reason(
        _trade_stub(date(2020, 1, 1)), None, today=date(2026, 1, 2), underlying_now=Decimal(100)
    )
    assert reason == CloseReason.EXPIRED


def test_stop_loss_call_triggers_only_below_floor() -> None:
    t = _thesis_stub({"exit_if_underlying_below": 320})
    trade = _trade_stub(date(2030, 1, 1))
    below = _choose_close_reason(trade, t, today=date(2026, 1, 2), underlying_now=Decimal("310"))
    above = _choose_close_reason(trade, t, today=date(2026, 1, 2), underlying_now=Decimal("330"))
    assert below == CloseReason.EXIT_UNDERLYING_BELOW
    assert above is None


def test_theta_exit_triggers_within_window() -> None:
    reason = _choose_close_reason(
        _trade_stub(date(2026, 1, 10)),
        _thesis_stub({"close_n_days_before_expiry": 5}),
        today=date(2026, 1, 7),
        underlying_now=Decimal("350"),
    )
    assert reason == CloseReason.THETA_EXIT


def test_expiry_cutoff_is_4pm_eastern() -> None:
    # June: EDT (UTC-4) -> 20:00 UTC. December: EST (UTC-5) -> 21:00 UTC.
    assert expiry_cutoff(date(2026, 6, 19)) == datetime(2026, 6, 19, 20, 0, tzinfo=UTC)
    assert expiry_cutoff(date(2026, 12, 18)) == datetime(2026, 12, 18, 21, 0, tzinfo=UTC)


# === Job 1: trade closing ===


@pytest.mark.asyncio
async def test_expired_call_settles_on_expiration_close_not_live_quote(db_factory) -> None:
    tid = await _seed_thesis(db_factory, generated_at=et(2026, 5, 20))
    trade_id = await _seed_trade(
        db_factory, thesis_id=tid, expiration=date(2026, 6, 1), opened_at=et(2026, 5, 20)
    )
    # Live quote (999) deliberately differs from the expiration-day close (360).
    prices = FakePrices(last=999.0, daily={date(2026, 6, 1): 360.0})
    result = await _resolver(db_factory, prices).resolve_all(now=et(2026, 6, 2))
    assert result.closed == 1
    async with db_factory() as session:
        trade = await session.get(ShadowTrade, trade_id)
    assert trade is not None
    assert trade.close_reason == CloseReason.EXPIRED.value
    assert trade.close_price_per_contract_usd == Decimal("10")  # 360 - 350
    assert trade.realized_pnl_usd == Decimal("612.00")  # (10 - 3.88) * 100


@pytest.mark.asyncio
async def test_expired_otm_call_closes_at_zero(db_factory) -> None:
    tid = await _seed_thesis(db_factory, generated_at=et(2026, 5, 20))
    await _seed_trade(
        db_factory,
        thesis_id=tid,
        expiration=date(2026, 6, 1),
        opened_at=et(2026, 5, 20),
        strike=400.0,
    )
    prices = FakePrices(daily={date(2026, 6, 1): 340.0})
    await _resolver(db_factory, prices).resolve_all(now=et(2026, 6, 2))
    async with db_factory() as session:
        trade = (await session.execute(select(ShadowTrade))).scalar_one()
    assert trade.close_price_per_contract_usd == Decimal("0")
    assert trade.realized_pnl_usd == Decimal("-388.00")


@pytest.mark.asyncio
async def test_not_expired_until_4pm_et_on_expiration_day(db_factory) -> None:
    """Regression: 00:30 UTC on the expiration date is 8:30pm ET the evening
    BEFORE — the option still has a full trading day; it must stay open."""
    tid = await _seed_thesis(db_factory, generated_at=et(2026, 6, 1), window_days=60)
    await _seed_trade(
        db_factory, thesis_id=tid, expiration=date(2026, 6, 19), opened_at=et(2026, 6, 1)
    )
    r = _resolver(db_factory, FakePrices())
    assert (await r.resolve_all(now=datetime(2026, 6, 19, 0, 30, tzinfo=UTC))).closed == 0
    assert (await r.resolve_all(now=et(2026, 6, 19, 15, 59))).closed == 0
    assert (await r.resolve_all(now=et(2026, 6, 19, 16, 5))).closed == 1


@pytest.mark.asyncio
async def test_settlement_defers_until_expiry_bar_exists_then_falls_back(db_factory) -> None:
    tid = await _seed_thesis(db_factory, generated_at=et(2026, 5, 20), window_days=60)
    await _seed_trade(
        db_factory, thesis_id=tid, expiration=date(2026, 6, 1), opened_at=et(2026, 5, 20)
    )
    prices = FakePrices(last=360.0, default_close=None)  # no daily bars at all
    r = _resolver(db_factory, prices)
    assert (await r.resolve_all(now=et(2026, 6, 2))).closed == 0  # wait, don't guess
    assert (await r.resolve_all(now=et(2026, 6, 6))).closed == 1  # grace over: live quote
    async with db_factory() as session:
        trade = (await session.execute(select(ShadowTrade))).scalar_one()
    assert trade.close_price_per_contract_usd == Decimal("10")


@pytest.mark.asyncio
async def test_two_account_trades_both_close_without_crashing(db_factory) -> None:
    """Regression (July 2026): one thesis -> one trade per account. Both must
    close in one tick; the old one-outcome-per-trade write crashed the tick."""
    tid = await _seed_thesis(db_factory, generated_at=et(2026, 5, 20), window_days=60)
    for kind in (AccountKind.AGGRESSIVE.value, AccountKind.CONSERVATIVE.value):
        await _seed_trade(
            db_factory,
            thesis_id=tid,
            expiration=date(2026, 6, 1),
            opened_at=et(2026, 5, 20),
            kind=kind,
        )
    r = _resolver(db_factory, FakePrices(daily={date(2026, 6, 1): 360.0}))
    first = await r.resolve_all(now=et(2026, 6, 2))
    assert (first.closed, first.errors) == (2, 0)
    again = await r.resolve_all(now=et(2026, 6, 2))
    assert (again.closed, again.errors) == (0, 0)


# === Job 2: thesis grading over the prediction window ===


@pytest.mark.asyncio
async def test_thesis_graded_at_window_end_from_price_at_thesis_time(db_factory) -> None:
    await _seed_thesis(db_factory, generated_at=et(2026, 6, 1, 11, 0), window_days=7)
    prices = FakePrices(
        intraday=300.0,  # price when the thesis was written
        default_close=250.0,  # any other day's close (must NOT be used)
        daily={date(2026, 6, 8): 309.0},  # close on the window's last day
    )
    r = _resolver(db_factory, prices)

    before = await r.resolve_all(now=et(2026, 6, 8, 15, 0))  # last session still open
    assert before.outcomes_written == 0
    after = await r.resolve_all(now=et(2026, 6, 8, 16, 30))
    assert after.outcomes_written == 1

    (o,) = await _outcomes(db_factory)
    assert o.underlying_price_at_thesis == Decimal("300")
    assert o.underlying_price_at_eval == Decimal("309")
    assert o.pct_move == pytest.approx(3.0)
    assert (o.realized_direction, o.hit) == ("long", True)
    assert o.notes is not None and o.notes.startswith(WINDOW_METHOD)
    assert "eval_close=2026-06-08" in o.notes


@pytest.mark.asyncio
async def test_window_ending_on_weekend_uses_prior_trading_close(db_factory) -> None:
    # Fri 2026-06-05 + 1 day = Sat 6/6: no bar -> roll back to Friday's close.
    await _seed_thesis(db_factory, generated_at=et(2026, 6, 5, 10, 0), window_days=1)
    prices = FakePrices(intraday=100.0, default_close=None, daily={date(2026, 6, 5): 98.0})
    await _resolver(db_factory, prices).resolve_all(now=et(2026, 6, 8))
    (o,) = await _outcomes(db_factory)
    assert "eval_close=2026-06-05" in (o.notes or "")
    assert (o.realized_direction, o.hit) == ("short", False)


@pytest.mark.asyncio
async def test_stop_loss_exit_does_not_grade_the_thesis(db_factory) -> None:
    """Regression for the selection bias: a stop-loss fires *because* the
    price moved against the position. Grading the thesis at that moment (the
    old behaviour) made misses nearly certain. The trade closes; the thesis
    waits for its window — here the dip recovered and the call was right."""
    tid = await _seed_thesis(
        db_factory,
        generated_at=et(2026, 6, 1, 10, 0),
        window_days=5,
        suggested_contract={"exit_if_underlying_below": 295},
    )
    await _seed_trade(
        db_factory, thesis_id=tid, expiration=date(2026, 7, 17), opened_at=et(2026, 6, 1, 10, 0)
    )
    prices = FakePrices(last=290.0, intraday=300.0, daily={date(2026, 6, 6): 312.0})
    r = _resolver(db_factory, prices)

    dip = await r.resolve_all(now=et(2026, 6, 1, 14, 0))
    assert dip.closed == 1  # stop fired: trade closed
    assert dip.outcomes_written == 0  # ...but the thesis is NOT graded on the dip

    end = await r.resolve_all(now=et(2026, 6, 6, 17, 0))
    assert end.outcomes_written == 1
    (o,) = await _outcomes(db_factory)
    assert o.hit is True  # long, +4% over its window


@pytest.mark.asyncio
async def test_untraded_thesis_is_still_graded(db_factory) -> None:
    """Regression: outcomes used to be written only when a trade closed, so
    theses no account traded (below both confidence gates, over budget)
    silently vanished from the calibration plot."""
    await _seed_thesis(db_factory, generated_at=et(2026, 6, 1), window_days=3, confidence=0.4)
    prices = FakePrices(intraday=100.0, daily={date(2026, 6, 4): 97.0})
    result = await _resolver(db_factory, prices).resolve_all(now=et(2026, 6, 5))
    assert result.outcomes_written == 1


@pytest.mark.asyncio
async def test_two_trades_one_thesis_one_outcome(db_factory) -> None:
    tid = await _seed_thesis(db_factory, generated_at=et(2026, 5, 20), window_days=7)
    for kind in (AccountKind.AGGRESSIVE.value, AccountKind.CONSERVATIVE.value):
        await _seed_trade(
            db_factory,
            thesis_id=tid,
            expiration=date(2026, 6, 1),
            opened_at=et(2026, 5, 20),
            kind=kind,
        )
    prices = FakePrices(intraday=300.0, daily={date(2026, 5, 27): 310.0, date(2026, 6, 1): 360.0})
    r = _resolver(db_factory, prices)
    await r.resolve_all(now=et(2026, 6, 2))
    await r.resolve_all(now=et(2026, 6, 3))
    assert len(await _outcomes(db_factory)) == 1


@pytest.mark.asyncio
async def test_missing_baseline_skips_instead_of_guessing(db_factory) -> None:
    await _seed_thesis(db_factory, generated_at=et(2026, 6, 1), window_days=1)
    result = await _resolver(db_factory, FakePrices(default_close=None)).resolve_all(
        now=et(2026, 6, 5)
    )
    assert result.outcomes_written == 0
    assert result.skipped == 1


@pytest.mark.asyncio
async def test_regrade_legacy_outcomes_in_place_keeping_old_values(db_factory) -> None:
    tid = await _seed_thesis(db_factory, generated_at=et(2026, 6, 1, 10, 0), window_days=5)
    async with db_factory() as session:
        session.add(
            ThesisOutcome(
                thesis_id=tid,
                evaluated_at=et(2026, 6, 1, 14, 0),
                realized_direction="short",
                hit=False,
                underlying_price_at_thesis=Decimal("300"),
                underlying_price_at_eval=Decimal("290"),
                pct_move=-3.33,
                notes="shadow_trade_id=1, close_reason=exit_underlying_below",
            )
        )
        await session.commit()
    r = _resolver(db_factory, FakePrices(intraday=300.0, daily={date(2026, 6, 6): 312.0}))

    counts = await r.regrade_legacy_outcomes(now=et(2026, 6, 10))
    assert counts == {"regraded": 1, "flipped": 1, "skipped": 0}
    (o,) = await _outcomes(db_factory)
    assert o.hit is True
    assert o.notes is not None and o.notes.startswith(WINDOW_METHOD)
    assert "was: hit=False realized=short" in o.notes
    assert "close_reason=exit_underlying_below" in o.notes

    # Idempotent: a second run leaves window-graded rows alone.
    assert await r.regrade_legacy_outcomes(now=et(2026, 6, 10)) == {
        "regraded": 0,
        "flipped": 0,
        "skipped": 0,
    }
