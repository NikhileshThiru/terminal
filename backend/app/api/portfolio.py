"""Portfolio API — accounts + shadow trades + equity curve (DESIGN.md §8)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from app.eval.persistence import get_session_factory
from app.portfolio.models import PaperAccount, PositionMark, ShadowTrade

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


class AccountSummary(BaseModel):
    id: int
    kind: str
    name: str
    starting_balance_usd: Decimal
    equity_usd: Decimal
    min_confidence: float
    max_trade_cost_usd: Decimal
    max_trades_per_day: int
    max_concurrent_positions: int
    kill_switch: bool
    # Live stats
    open_shadow_positions: int
    shadow_trades_today: int
    shadow_trades_total: int
    total_cost_open_usd: Decimal


class ShadowTradeOut(BaseModel):
    id: int
    account_id: int
    account_kind: str
    thesis_id: int
    opened_at: datetime
    underlying: str
    occ_symbol: str
    option_type: str
    strike: Decimal
    expiration: date
    contracts: int
    premium_per_contract_usd: Decimal
    total_cost_usd: Decimal
    status: str
    risk_reason: str
    # Lifecycle — what the position is doing NOW (open) or did (closed).
    closed_at: datetime | None = None
    close_reason: str | None = None
    realized_pnl_usd: Decimal | None = None
    # Latest mark for open positions (None until the first MTM tick lands).
    unrealized_pnl_usd: Decimal | None = None
    marked_at: datetime | None = None


@router.get("/accounts", response_model=list[AccountSummary])
async def list_accounts() -> list[AccountSummary]:
    factory = get_session_factory()
    async with factory() as session:
        accounts = (
            (await session.execute(select(PaperAccount).order_by(PaperAccount.kind)))
            .scalars()
            .all()
        )
        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        out: list[AccountSummary] = []
        for a in accounts:
            open_count = (
                await session.scalar(
                    select(func.count(ShadowTrade.id))
                    .where(ShadowTrade.account_id == a.id)
                    .where(ShadowTrade.status == "shadow_open")
                )
                or 0
            )
            today_count = (
                await session.scalar(
                    select(func.count(ShadowTrade.id))
                    .where(ShadowTrade.account_id == a.id)
                    .where(ShadowTrade.opened_at >= today_start)
                )
                or 0
            )
            total_count = (
                await session.scalar(
                    select(func.count(ShadowTrade.id)).where(ShadowTrade.account_id == a.id)
                )
                or 0
            )
            total_open_cost = (
                await session.scalar(
                    select(func.coalesce(func.sum(ShadowTrade.total_cost_usd), 0))
                    .where(ShadowTrade.account_id == a.id)
                    .where(ShadowTrade.status == "shadow_open")
                )
                or 0
            )
            # Live equity, derived the same way the equity curve derives it:
            # starting balance + realized P&L on closed trades + the latest
            # mark per still-open trade. The stored a.equity_usd is only the
            # seed value and would read $100,000 forever.
            realized_total = (
                await session.scalar(
                    select(func.coalesce(func.sum(ShadowTrade.realized_pnl_usd), 0))
                    .where(ShadowTrade.account_id == a.id)
                    .where(ShadowTrade.status == "shadow_closed")
                )
                or 0
            )
            latest_mark = (
                select(
                    PositionMark.shadow_trade_id,
                    func.max(PositionMark.marked_at).label("max_at"),
                )
                .join(ShadowTrade, PositionMark.shadow_trade_id == ShadowTrade.id)
                .where(ShadowTrade.account_id == a.id)
                .where(ShadowTrade.status == "shadow_open")
                .group_by(PositionMark.shadow_trade_id)
                .subquery()
            )
            unrealized_total = (
                await session.scalar(
                    select(func.coalesce(func.sum(PositionMark.unrealized_pnl_usd), 0)).join(
                        latest_mark,
                        (PositionMark.shadow_trade_id == latest_mark.c.shadow_trade_id)
                        & (PositionMark.marked_at == latest_mark.c.max_at),
                    )
                )
                or 0
            )
            equity_live = (
                a.starting_balance_usd
                + Decimal(str(realized_total))
                + Decimal(str(unrealized_total))
            )
            out.append(
                AccountSummary(
                    id=a.id,
                    kind=a.kind,
                    name=a.name,
                    starting_balance_usd=a.starting_balance_usd,
                    equity_usd=equity_live,
                    min_confidence=a.min_confidence,
                    max_trade_cost_usd=a.max_trade_cost_usd,
                    max_trades_per_day=a.max_trades_per_day,
                    max_concurrent_positions=a.max_concurrent_positions,
                    kill_switch=a.kill_switch,
                    open_shadow_positions=int(open_count),
                    shadow_trades_today=int(today_count),
                    shadow_trades_total=int(total_count),
                    total_cost_open_usd=Decimal(str(total_open_cost)),
                )
            )
        return out


@router.get("/shadow-trades", response_model=list[ShadowTradeOut])
async def list_shadow_trades(
    account_kind: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
) -> list[ShadowTradeOut]:
    factory = get_session_factory()
    async with factory() as session:
        stmt = (
            select(ShadowTrade, PaperAccount.kind)
            .join(PaperAccount, PaperAccount.id == ShadowTrade.account_id)
            .order_by(desc(ShadowTrade.opened_at))
            .limit(limit)
        )
        if account_kind:
            stmt = stmt.where(PaperAccount.kind == account_kind)
        rows = (await session.execute(stmt)).all()

        # Latest mark per listed trade, in one query.
        trade_ids = [t.id for (t, _) in rows]
        latest_marks: dict[int, tuple[Decimal, datetime]] = {}
        if trade_ids:
            latest = (
                select(
                    PositionMark.shadow_trade_id,
                    func.max(PositionMark.marked_at).label("max_at"),
                )
                .where(PositionMark.shadow_trade_id.in_(trade_ids))
                .group_by(PositionMark.shadow_trade_id)
                .subquery()
            )
            mark_rows = (
                await session.execute(
                    select(
                        PositionMark.shadow_trade_id,
                        PositionMark.unrealized_pnl_usd,
                        PositionMark.marked_at,
                    ).join(
                        latest,
                        (PositionMark.shadow_trade_id == latest.c.shadow_trade_id)
                        & (PositionMark.marked_at == latest.c.max_at),
                    )
                )
            ).all()
            latest_marks = {tid: (pnl, at) for (tid, pnl, at) in mark_rows}

    return [
        ShadowTradeOut(
            id=t.id,
            account_id=t.account_id,
            account_kind=kind,
            thesis_id=t.thesis_id,
            opened_at=t.opened_at,
            underlying=t.underlying,
            occ_symbol=t.occ_symbol,
            option_type=t.option_type,
            strike=t.strike,
            expiration=t.expiration,
            contracts=t.contracts,
            premium_per_contract_usd=t.premium_per_contract_usd,
            total_cost_usd=t.total_cost_usd,
            status=t.status,
            risk_reason=t.risk_reason,
            closed_at=t.closed_at,
            close_reason=t.close_reason,
            realized_pnl_usd=t.realized_pnl_usd,
            unrealized_pnl_usd=(latest_marks.get(t.id) or (None, None))[0],
            marked_at=(latest_marks.get(t.id) or (None, None))[1],
        )
        for (t, kind) in rows
    ]


# === Equity curve ===
#
# Equity at time t = starting_balance + closed_realized_total(t) + open_unrealized_total(t)
# where the open total uses the latest mark per still-open trade as of t.
# History-preserving: drives the conservative-vs-aggressive A/B chart that
# IS the experimental story of the project.


class EquityPoint(BaseModel):
    t: datetime
    equity: Decimal
    open_unrealized: Decimal
    closed_realized: Decimal


class EquityCurveResponse(BaseModel):
    account_kind: str
    starting_balance_usd: Decimal
    points: list[EquityPoint]


@router.get("/accounts/{account_kind}/equity-curve", response_model=EquityCurveResponse)
async def equity_curve(
    account_kind: str,
    days: int = Query(default=30, ge=1, le=365),
) -> EquityCurveResponse:
    """Time-series of equity for one account. Each PositionMark and each
    close-event becomes one point; we re-derive the running totals so the
    series is self-consistent even when marks land out of order."""
    factory = get_session_factory()
    since = datetime.now(UTC) - timedelta(days=days)
    async with factory() as session:
        account = (
            await session.execute(select(PaperAccount).where(PaperAccount.kind == account_kind))
        ).scalar_one_or_none()
        if account is None:
            raise HTTPException(status_code=404, detail=f"unknown account_kind {account_kind!r}")

        marks_rows = (
            await session.execute(
                select(
                    PositionMark.marked_at,
                    PositionMark.unrealized_pnl_usd,
                    PositionMark.shadow_trade_id,
                )
                .join(ShadowTrade, PositionMark.shadow_trade_id == ShadowTrade.id)
                .where(
                    ShadowTrade.account_id == account.id,
                    PositionMark.marked_at >= since,
                )
                .order_by(PositionMark.marked_at)
            )
        ).all()

        closed_rows = (
            await session.execute(
                select(
                    ShadowTrade.closed_at,
                    ShadowTrade.realized_pnl_usd,
                    ShadowTrade.id,
                )
                .where(
                    ShadowTrade.account_id == account.id,
                    ShadowTrade.status == "shadow_closed",
                    ShadowTrade.closed_at.is_not(None),
                    ShadowTrade.closed_at >= since,
                )
                .order_by(ShadowTrade.closed_at)
            )
        ).all()

    # Merge marks + closes into one chronological event stream.
    events: list[tuple[datetime, str, int, Decimal]] = []
    for marked_at, unreal, trade_id in marks_rows:
        if marked_at is None:
            continue
        events.append((marked_at, "mark", trade_id, unreal or Decimal(0)))
    for closed_at, realized, trade_id in closed_rows:
        if closed_at is None:
            continue
        events.append((closed_at, "close", trade_id, realized or Decimal(0)))
    events.sort(key=lambda e: e[0])

    open_pnl_per_trade: dict[int, Decimal] = {}
    realized_total = Decimal(0)
    points: list[EquityPoint] = [
        EquityPoint(
            t=since,
            equity=account.starting_balance_usd,
            open_unrealized=Decimal(0),
            closed_realized=Decimal(0),
        )
    ]
    for ts, kind, trade_id, value in events:
        if kind == "mark":
            open_pnl_per_trade[trade_id] = value
        else:  # close
            open_pnl_per_trade.pop(trade_id, None)
            realized_total += value
        open_total = sum(open_pnl_per_trade.values(), start=Decimal(0))
        equity = account.starting_balance_usd + realized_total + open_total
        points.append(
            EquityPoint(
                t=ts,
                equity=equity,
                open_unrealized=open_total,
                closed_realized=realized_total,
            )
        )

    return EquityCurveResponse(
        account_kind=account_kind,
        starting_balance_usd=account.starting_balance_usd,
        points=points,
    )
