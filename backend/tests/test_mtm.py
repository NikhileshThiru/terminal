"""MtMService tests — synthetic option + underlying quotes, asserts persisted marks."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.data.types import OptionContract, ProviderUnavailable, ProviderUnavailableReason, Quote
from app.eval.models import Base
from app.eval.models import Thesis as ThesisRow
from app.portfolio.models import (
    AccountKind,
    PaperAccount,
    PositionMark,
    ShadowTrade,
    ShadowTradeStatus,
)
from app.portfolio.mtm import MtMService, _mid_price


class FakeOptions:
    name = "fake-opt"

    def __init__(self, quotes_by_occ: dict[str, OptionContract] | None = None) -> None:
        self.quotes = quotes_by_occ or {}
        self.fail_on: set[str] = set()

    async def get_expirations(self, symbol: str) -> list[date]:
        raise NotImplementedError

    async def get_chain(self, symbol: str, expiration: date) -> list[OptionContract]:
        raise NotImplementedError

    async def get_contract_quote(self, occ_symbol: str) -> OptionContract:
        if occ_symbol in self.fail_on:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.UPSTREAM_ERROR,
                message="boom",
                provider="fake-opt",
            )
        if occ_symbol not in self.quotes:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.DATA_MISSING,
                message=f"no quote for {occ_symbol}",
                provider="fake-opt",
            )
        return self.quotes[occ_symbol]


class FakePrices:
    name = "fake-px"

    def __init__(self, by_symbol: dict[str, Quote] | None = None) -> None:
        self.by_symbol = by_symbol or {}

    async def get_latest_quote(self, symbol: str) -> Quote:
        if symbol not in self.by_symbol:
            raise ProviderUnavailable(
                reason=ProviderUnavailableReason.DATA_MISSING,
                message=f"no quote for {symbol}",
                provider="fake-px",
            )
        return self.by_symbol[symbol]

    async def get_ohlc(self, *_: Any, **__: Any) -> list:
        return []


@pytest.fixture
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_trade(
    factory: async_sessionmaker, *, occ: str = "AAPL260821C00350000", premium: float = 3.88
) -> int:
    """Seed a paper account (singleton) + thesis + one open shadow trade. Returns the trade id."""
    async with factory() as session:
        # Get-or-create the aggressive account so multiple trades share it.
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
            generated_at=datetime.now(UTC),
            direction="long",
            confidence=0.7,
            prediction_window_days=14,
            reasoning="test",
            suggested_contract={"occ_symbol": occ},
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
            opened_at=datetime.now(UTC),
            underlying="AAPL",
            occ_symbol=occ,
            option_type="call",
            strike=Decimal("350"),
            expiration=date(2026, 8, 21),
            contracts=1,
            premium_per_contract_usd=Decimal(str(premium)),
            total_cost_usd=Decimal(str(premium * 100)),
            status=ShadowTradeStatus.SHADOW_OPEN.value,
            risk_reason="test",
        )
        session.add(trade)
        await session.commit()
        return int(trade.id)


def _contract(
    occ: str = "AAPL260821C00350000",
    *,
    bid: float | None = 5.0,
    ask: float | None = 5.2,
    last: float | None = None,
) -> OptionContract:
    return OptionContract(
        symbol="AAPL",
        occ_symbol=occ,
        expiration=date(2026, 8, 21),
        strike=Decimal("350"),
        option_type="call",
        bid=Decimal(str(bid)) if bid is not None else None,
        ask=Decimal(str(ask)) if ask is not None else None,
        last=Decimal(str(last)) if last is not None else None,
    )


def _quote(last: float = 320.0) -> Quote:
    return Quote(
        symbol="AAPL",
        bid=Decimal(str(last - 0.05)),
        ask=Decimal(str(last + 0.05)),
        last=Decimal(str(last)),
        timestamp=datetime.now(UTC),
    )


# === _mid_price helper ===


def test_mid_returns_bid_ask_mid_when_both_available() -> None:
    assert _mid_price(_contract(bid=4.0, ask=4.2)) == Decimal("4.1")


def test_mid_falls_back_to_last_when_no_bid_or_ask() -> None:
    assert _mid_price(_contract(bid=None, ask=None, last=4.5)) == Decimal("4.5")


def test_mid_returns_none_when_nothing_available() -> None:
    assert _mid_price(_contract(bid=None, ask=None, last=None)) is None


def test_mid_returns_none_when_bid_or_ask_is_zero() -> None:
    """Provider sometimes returns 0 for missing-side; treat that as no quote."""
    assert _mid_price(_contract(bid=0, ask=4.2, last=None)) is None


# === mark_all_open ===


@pytest.mark.asyncio
async def test_mark_writes_position_mark_for_open_trade(db_factory) -> None:
    trade_id = await _seed_trade(db_factory, premium=3.88)
    options = FakeOptions({"AAPL260821C00350000": _contract(bid=5.0, ask=5.2)})
    prices = FakePrices({"AAPL": _quote(last=320.0)})
    svc = MtMService(options_provider=options, price_provider=prices, session_factory=db_factory)

    result = await svc.mark_all_open()
    assert result.marks_written == 1
    assert result.skipped == 0
    assert result.errors == 0
    assert result.total_open_positions == 1

    async with db_factory() as session:
        marks = list((await session.execute(select(PositionMark))).scalars().all())
    assert len(marks) == 1
    m = marks[0]
    assert m.shadow_trade_id == trade_id
    assert m.option_mid_usd == Decimal("5.1")  # (5.0 + 5.2) / 2
    # Unrealized PnL: (5.1 - 3.88) * 100 * 1 = $122
    assert m.unrealized_pnl_usd == Decimal("122.00")
    assert m.underlying_price_usd == Decimal("320")


@pytest.mark.asyncio
async def test_mark_skips_when_option_provider_unavailable(db_factory) -> None:
    await _seed_trade(db_factory)
    options = FakeOptions()  # empty — every quote raises ProviderUnavailable
    prices = FakePrices({"AAPL": _quote()})
    svc = MtMService(options_provider=options, price_provider=prices, session_factory=db_factory)

    result = await svc.mark_all_open()
    assert result.marks_written == 0
    assert result.skipped == 1


@pytest.mark.asyncio
async def test_mark_skips_closed_trades(db_factory) -> None:
    """Closed trades shouldn't be re-marked — only shadow_open positions."""
    trade_id = await _seed_trade(db_factory)
    async with db_factory() as session:
        trade = await session.get(ShadowTrade, trade_id)
        assert trade is not None
        trade.status = ShadowTradeStatus.SHADOW_CLOSED.value
        await session.commit()

    options = FakeOptions({"AAPL260821C00350000": _contract()})
    prices = FakePrices({"AAPL": _quote()})
    svc = MtMService(options_provider=options, price_provider=prices, session_factory=db_factory)

    result = await svc.mark_all_open()
    assert result.total_open_positions == 0
    assert result.marks_written == 0


@pytest.mark.asyncio
async def test_partial_failure_is_isolated(db_factory) -> None:
    """One bad symbol must not stop marks for healthy positions."""
    await _seed_trade(db_factory, occ="AAPL260821C00350000")
    await _seed_trade(db_factory, occ="MSFT260821C00400000", premium=2.0)
    options = FakeOptions({"AAPL260821C00350000": _contract(bid=5.0, ask=5.2)})
    options.fail_on = {"MSFT260821C00400000"}
    prices = FakePrices({"AAPL": _quote(), "MSFT": _quote()})
    svc = MtMService(options_provider=options, price_provider=prices, session_factory=db_factory)

    result = await svc.mark_all_open()
    assert result.marks_written == 1
    assert result.skipped == 1
