"""Outcome resolver — closes shadow trades, and grades theses.

Two jobs, deliberately independent:

1. **Close trades** (the P&L story). Walk open shadow trades; close on
   expiration (4pm ET, settled at the expiration-day close), the thesis's
   stop-loss, or its theta exit. Fills `closed_at / close_reason /
   realized_pnl`.

2. **Grade theses** (the eval — what the calibration plot is made of). Every
   thesis — traded or not — is graded once its own prediction window has
   ended: the underlying's move from its price *when the thesis was written*
   to the close on the window's last day, against the stated direction.

Why they must be separate (found 2026-10, see README "What I learned"): the
original resolver graded a thesis at the moment its trade closed. Most trades
closed on a stop-loss — and a stop-loss fires *because* the price moved
against the position — so the grade was sampled at the worst moment, usually
the same afternoon, and never at the horizon the thesis actually claimed.
Theses that no account traded were never graded at all. Both are selection
bias; the old outcomes are re-graded by `regrade_legacy_outcomes()`
(`python -m app.portfolio.regrade`), keeping their old values in `notes`.

Scoring the underlying rather than the option's P&L is intentional: a long
call can lose money while the stock goes up (theta, IV crush), and that's
the trade structure's fault, not the prediction's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from functools import lru_cache
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.core.logging import get_logger
from app.data.interfaces import OptionsProvider, PriceProvider
from app.data.types import OHLCBar, ProviderUnavailable
from app.eval.models import RealizedDirection, ThesisOutcome
from app.eval.models import Thesis as ThesisRow
from app.portfolio.models import (
    CloseReason,
    PaperAccount,
    ShadowTrade,
    ShadowTradeStatus,
)
from app.portfolio.mtm import _mid_price

_log = get_logger(__name__)

_CONTRACT_MULTIPLIER = Decimal(100)
# A move smaller than this (in % terms) is "flat" — neither up nor down.
# Stops random walk noise from being scored as a directional hit/miss.
_FLAT_BAND_PCT = 0.5  # ±0.5% counts as flat

_EASTERN = ZoneInfo("America/New_York")
# US equity options stop trading at 4:00pm ET on expiration day.
_EXPIRY_CUTOFF = time(16, 0)
# If the expiration-day daily bar still isn't available this long after
# expiry (provider outage), settle on the live quote rather than never.
_SETTLEMENT_GRACE = timedelta(days=3)
_SESSION_CLOSE = time(16, 0)
# Outcome rows graded by the window method start their notes with this.
WINDOW_METHOD = "method=window"


def expiry_cutoff(expiration: date) -> datetime:
    """The moment an option expiring on `expiration` stops trading (UTC)."""
    return datetime.combine(expiration, _EXPIRY_CUTOFF, tzinfo=_EASTERN).astimezone(UTC)


def _aware(ts: datetime) -> datetime:
    """SQLite drops tzinfo on read; every timestamp in this codebase is UTC."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _eastern_date(ts: datetime) -> date:
    return _aware(ts).astimezone(_EASTERN).date()


@dataclass(frozen=True)
class ResolveResult:
    """Counts summary for one tick of `OutcomeResolver.resolve_all()`."""

    closed: int
    outcomes_written: int
    skipped: int
    errors: int


@dataclass(frozen=True)
class Grade:
    """A thesis's realized outcome under the window method."""

    realized_direction: str
    hit: bool
    pct_move: float
    price_at_thesis: Decimal
    price_at_eval: Decimal
    eval_day: date
    baseline_source: str  # "5min" | "daily"

    def notes(self, window_days: int) -> str:
        return (
            f"{WINDOW_METHOD}; window={window_days}d; baseline={self.baseline_source}; "
            f"eval_close={self.eval_day.isoformat()}"
        )


def window_end_day(thesis: ThesisRow) -> date:
    """Last calendar day (ET) of the thesis's prediction window."""
    return _eastern_date(thesis.generated_at) + timedelta(
        days=max(1, int(thesis.prediction_window_days or 1))
    )


def window_closed(thesis: ThesisRow, now: datetime) -> bool:
    """True once the window's last session has closed (4pm ET on the end day)."""
    end = datetime.combine(window_end_day(thesis), _SESSION_CLOSE, tzinfo=_EASTERN)
    return now >= end.astimezone(UTC)


def _classify_direction(pct_move: float) -> str:
    """Map a percent move to long/short/flat (DESIGN.md §8: realized_direction)."""
    if pct_move > _FLAT_BAND_PCT:
        return RealizedDirection.LONG.value
    if pct_move < -_FLAT_BAND_PCT:
        return RealizedDirection.SHORT.value
    return RealizedDirection.FLAT.value


def _choose_close_reason(
    trade: ShadowTrade,
    thesis: ThesisRow | None,
    today: date,
    underlying_now: Decimal,
    now: datetime | None = None,
) -> CloseReason | None:
    """Decide if/why a trade should close. Returns None if it should stay open.

    `today` is the US/Eastern calendar date. Expiration fires at 4:00pm ET on
    the expiration date — not at UTC midnight, which is ~8pm ET the evening
    *before*, when the option still has a full trading day left.

    Order of precedence: expiration first (it's binding), then stop-loss
    (capital preservation), then theta exit (avoid late decay). The thesis's
    exit fields live on the JSON suggested_contract blob; this reads them
    defensively in case they're missing.
    """
    expired = (
        now >= expiry_cutoff(trade.expiration) if now is not None else today > trade.expiration
    )
    if expired:
        return CloseReason.EXPIRED

    contract_payload = (thesis.suggested_contract if thesis is not None else None) or {}

    # Stop-loss: long calls bail if underlying drops below the threshold;
    # long puts bail if it rises above.
    if trade.option_type == "call":
        floor = contract_payload.get("exit_if_underlying_below")
        if floor is not None and underlying_now < Decimal(str(floor)):
            return CloseReason.EXIT_UNDERLYING_BELOW
    else:  # put
        ceiling = contract_payload.get("exit_if_underlying_above")
        if ceiling is not None and underlying_now > Decimal(str(ceiling)):
            return CloseReason.EXIT_UNDERLYING_ABOVE

    # Theta exit: close N days before expiration to avoid late-stage decay.
    theta_days = contract_payload.get("close_n_days_before_expiry")
    if (
        theta_days is not None
        and theta_days > 0
        and today >= trade.expiration - timedelta(days=int(theta_days))
    ):
        return CloseReason.THETA_EXIT

    return None


class OutcomeResolver:
    """Closes shadow trades and grades theses (see module docstring)."""

    def __init__(
        self,
        *,
        options_provider: OptionsProvider,
        price_provider: PriceProvider,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._options = options_provider
        self._prices = price_provider
        self._session_factory = session_factory

    async def resolve_all(self, *, now: datetime | None = None) -> ResolveResult:
        now = now or datetime.now(UTC)
        closed, closed_skipped, closed_errors = await self._close_trades(now)
        written, graded_skipped, graded_errors = await self._grade_theses(now)
        return ResolveResult(
            closed=closed,
            outcomes_written=written,
            skipped=closed_skipped + graded_skipped,
            errors=closed_errors + graded_errors,
        )

    # === Job 1: close trades ===

    async def _close_trades(self, now: datetime) -> tuple[int, int, int]:
        today = _eastern_date(now)
        async with self._session_factory() as session:
            open_trades = list(
                (
                    await session.execute(
                        select(ShadowTrade)
                        .where(ShadowTrade.status == ShadowTradeStatus.SHADOW_OPEN.value)
                        .order_by(ShadowTrade.expiration)
                    )
                )
                .scalars()
                .all()
            )

            # account_id → kind map for alert text (one query per tick, not per trade).
            account_kinds: dict[int, str] = {}
            if open_trades:
                rows = (
                    await session.execute(
                        select(PaperAccount.id, PaperAccount.kind).where(
                            PaperAccount.id.in_({t.account_id for t in open_trades})
                        )
                    )
                ).all()
                account_kinds = {int(aid): kind for aid, kind in rows}

            closed_trades: list[ShadowTrade] = []
            skipped = errors = 0
            for trade in open_trades:
                try:
                    if await self._maybe_close_trade(session, trade, now, today):
                        closed_trades.append(trade)
                except ProviderUnavailable as e:
                    skipped += 1
                    _log.warning("resolver_skipped", trade_id=trade.id, reason=e.reason.value)
                except Exception:
                    errors += 1
                    _log.exception("resolver_unexpected_error", trade_id=trade.id)

            if closed_trades:
                await session.commit()
                _log.info(
                    "resolver_trades_closed",
                    closed=len(closed_trades),
                    skipped=skipped,
                    errors=errors,
                )
                for trade in closed_trades:
                    _alert_trade_close(account_kinds.get(int(trade.account_id), "unknown"), trade)
            return len(closed_trades), skipped, errors

    async def _maybe_close_trade(
        self,
        session: AsyncSession,
        trade: ShadowTrade,
        now: datetime,
        today: date,
    ) -> bool:
        """Close `trade` if an exit rule fires. Returns True if it closed."""
        # Zero-guarded (Quote.safe_price): a one-sided after-hours quote must
        # NOT trip an exit rule — it once read as a phantom -50% move.
        underlying_now = (await self._prices.get_latest_quote(trade.underlying)).safe_price()
        if underlying_now is None:
            return False

        # Close-reason logic reads the thesis's exit_if_underlying_* and
        # close_n_days_before_expiry fields.
        thesis = await session.get(ThesisRow, trade.thesis_id)
        if thesis is None:
            _log.warning("resolver_thesis_missing", thesis_id=trade.thesis_id)
            return False

        reason = _choose_close_reason(trade, thesis, today, underlying_now, now)
        if reason is None:
            return False

        # EXPIRED settles on the expiration day's official close, not whenever
        # this tick happens to run. No settlement bar yet -> next tick.
        underlying_eval = underlying_now
        if reason == CloseReason.EXPIRED:
            settlement = await self._settlement_price(trade, now, underlying_now)
            if settlement is None:
                return False
            underlying_eval = settlement

        close_price = await self._close_price(trade, reason, underlying_eval)
        trade.status = ShadowTradeStatus.SHADOW_CLOSED.value
        trade.closed_at = now
        trade.close_reason = reason.value
        trade.close_price_per_contract_usd = close_price
        trade.realized_pnl_usd = (
            (close_price - trade.premium_per_contract_usd)
            * _CONTRACT_MULTIPLIER
            * Decimal(trade.contracts)
        )
        _log.info(
            "resolver_closed_trade",
            trade_id=trade.id,
            thesis_id=trade.thesis_id,
            reason=reason.value,
            realized_pnl=str(trade.realized_pnl_usd),
        )
        return True

    # === Job 2: grade theses (the eval) ===

    async def _grade_theses(self, now: datetime) -> tuple[int, int, int]:
        """Write an outcome for every ungraded thesis whose window has closed."""
        async with self._session_factory() as session:
            candidates = [
                t
                for t in (
                    await session.execute(
                        select(ThesisRow)
                        .options(selectinload(ThesisRow.outcome))
                        .where(ThesisRow.pre_strictness.is_(False))
                        .order_by(ThesisRow.generated_at)
                    )
                )
                .scalars()
                .all()
                if t.outcome is None and window_closed(t, now)
            ]
            written = skipped = errors = 0
            for thesis in candidates:
                try:
                    grade = await self.grade_thesis(thesis)
                except Exception:
                    errors += 1
                    _log.exception("resolver_grade_failed", thesis_id=thesis.id)
                    continue
                if grade is None:
                    skipped += 1
                    continue
                session.add(_outcome_row(thesis, grade, now))
                written += 1
                _log.info(
                    "resolver_thesis_graded",
                    thesis_id=thesis.id,
                    symbol=thesis.symbol,
                    direction=thesis.direction,
                    realized=grade.realized_direction,
                    hit=grade.hit,
                    pct_move=round(grade.pct_move, 2),
                )
            if written:
                await session.commit()
            return written, skipped, errors

    async def grade_thesis(self, thesis: ThesisRow) -> Grade | None:
        """Grade one thesis under the window method; None if prices are missing."""
        baseline = await self._price_at(thesis.symbol, thesis.generated_at)
        if baseline is None:
            _log.warning("resolver_missing_baseline", thesis_id=thesis.id, symbol=thesis.symbol)
            return None
        price_at_thesis, source = baseline
        end_day = window_end_day(thesis)
        closing = await self._close_on_or_before(thesis.symbol, end_day)
        if closing is None or closing[1] < _eastern_date(thesis.generated_at):
            _log.warning("resolver_missing_eval_close", thesis_id=thesis.id, end_day=str(end_day))
            return None
        price_at_eval, eval_day = closing
        pct_move = float((price_at_eval - price_at_thesis) / price_at_thesis * Decimal(100))
        realized = _classify_direction(pct_move)
        return Grade(
            realized_direction=realized,
            hit=thesis.direction == realized and realized != RealizedDirection.FLAT.value,
            pct_move=pct_move,
            price_at_thesis=price_at_thesis,
            price_at_eval=price_at_eval,
            eval_day=eval_day,
            baseline_source=source,
        )

    async def regrade_legacy_outcomes(self, *, now: datetime | None = None) -> dict[str, int]:
        """One-time correction: re-grade outcomes written by the old
        trade-close method under the window method, in place. The previous
        values are preserved in `notes`. Idempotent (skips window rows)."""
        now = now or datetime.now(UTC)
        counts = {"regraded": 0, "flipped": 0, "skipped": 0}
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(ThesisOutcome, ThesisRow).join(
                        ThesisRow, ThesisRow.id == ThesisOutcome.thesis_id
                    )
                )
            ).all()
            for outcome, thesis in rows:
                if (outcome.notes or "").startswith(WINDOW_METHOD) or not window_closed(
                    thesis, now
                ):
                    continue
                grade = await self.grade_thesis(thesis)
                if grade is None:
                    counts["skipped"] += 1
                    continue
                was = (
                    f"was: hit={outcome.hit} realized={outcome.realized_direction} "
                    f"pct={outcome.pct_move:.2f} graded={outcome.evaluated_at:%Y-%m-%d} "
                    f"({outcome.notes or ''})"
                )
                counts["flipped"] += int(outcome.hit != grade.hit)
                outcome.realized_direction = grade.realized_direction
                outcome.hit = grade.hit
                outcome.pct_move = grade.pct_move
                outcome.underlying_price_at_thesis = grade.price_at_thesis
                outcome.underlying_price_at_eval = grade.price_at_eval
                outcome.evaluated_at = now
                outcome.notes = f"{grade.notes(thesis.prediction_window_days)}; regraded; {was}"
                counts["regraded"] += 1
            await session.commit()
        _log.info("resolver_legacy_regrade", **counts)
        return counts

    # === Price helpers ===

    async def _price_at(self, symbol: str, ts: datetime) -> tuple[Decimal, str] | None:
        """The underlying's price at `ts`: the last 5-minute bar at or before it
        (an off-hours thesis gets the last traded price), else the last daily
        close whose session ended before `ts` — never a close from *after*
        the thesis was written."""
        ts = _aware(ts)
        try:
            intraday = await self._prices.get_ohlc(
                symbol,
                start=ts - timedelta(days=4),
                end=ts + timedelta(minutes=5),
                timeframe="5Min",
            )
        except ProviderUnavailable:
            intraday = []
        at_or_before = [b for b in intraday if b.timestamp <= ts]
        if at_or_before:
            return max(at_or_before, key=lambda b: b.timestamp).close, "5min"
        try:
            daily = await self._prices.get_ohlc(
                symbol, start=ts - timedelta(days=7), end=ts + timedelta(days=1)
            )
        except ProviderUnavailable:
            return None
        settled = [
            b
            for b in daily
            if datetime.combine(_eastern_date(b.timestamp), _SESSION_CLOSE, tzinfo=_EASTERN) <= ts
        ]
        if settled:
            return max(settled, key=lambda b: b.timestamp).close, "daily"
        return None

    async def _close_on_or_before(self, symbol: str, day: date) -> tuple[Decimal, date] | None:
        """Close of the last trading day on or before `day` (weekends/holidays
        roll back to the prior session)."""
        start = datetime.combine(day - timedelta(days=7), time.min, tzinfo=_EASTERN)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=_EASTERN)
        try:
            bars = await self._prices.get_ohlc(
                symbol, start=start.astimezone(UTC), end=end.astimezone(UTC)
            )
        except ProviderUnavailable:
            return None
        eligible = [b for b in bars if _eastern_date(b.timestamp) <= day]
        if not eligible:
            return None
        last = max(eligible, key=lambda b: b.timestamp)
        return last.close, _eastern_date(last.timestamp)

    async def _close_price(
        self,
        trade: ShadowTrade,
        reason: CloseReason,
        underlying_now: Decimal,
    ) -> Decimal:
        """Per-contract close price for the option."""
        if reason == CloseReason.EXPIRED:
            # Intrinsic value at expiration. Long call: max(0, S - K). Long put: max(0, K - S).
            if trade.option_type == "call":
                return max(Decimal(0), underlying_now - trade.strike)
            return max(Decimal(0), trade.strike - underlying_now)
        # Stop-loss / theta exit: take the live option mid; fall back to the
        # original premium (zero P&L) if no quote is available.
        contract = await self._options.get_contract_quote(trade.occ_symbol)
        mid = _mid_price(contract)
        return mid if mid is not None else trade.premium_per_contract_usd

    async def _settlement_price(
        self, trade: ShadowTrade, now: datetime, underlying_now: Decimal
    ) -> Decimal | None:
        """Underlying close on the expiration date (the settlement reference).

        Returns None to defer while the daily bar isn't published yet; after
        a grace period, falls back to the live price so a long provider
        outage can't keep a position open forever."""
        day_start = datetime.combine(trade.expiration, time.min, tzinfo=_EASTERN)
        try:
            bars = await self._prices.get_ohlc(
                trade.underlying,
                start=day_start.astimezone(UTC),
                end=(day_start + timedelta(days=1)).astimezone(UTC),
            )
        except ProviderUnavailable:
            bars = []
        close = _bar_close_on(bars, trade.expiration)
        if close is not None:
            return close
        if now - expiry_cutoff(trade.expiration) > _SETTLEMENT_GRACE:
            _log.warning(
                "resolver_settlement_fallback_live_price",
                trade_id=trade.id,
                expiration=trade.expiration.isoformat(),
            )
            return underlying_now
        return None


def _outcome_row(thesis: ThesisRow, grade: Grade, now: datetime) -> ThesisOutcome:
    return ThesisOutcome(
        thesis_id=thesis.id,
        evaluated_at=now,
        realized_direction=grade.realized_direction,
        hit=grade.hit,
        underlying_price_at_thesis=grade.price_at_thesis,
        underlying_price_at_eval=grade.price_at_eval,
        pct_move=grade.pct_move,
        notes=grade.notes(thesis.prediction_window_days),
    )


def _bar_close_on(bars: list[OHLCBar], day: date) -> Decimal | None:
    """Close of the daily bar for Eastern-calendar `day`, if present. (Daily
    bars are stamped at 04:00 UTC = midnight ET; comparing UTC dates works
    by accident, so compare in the market's own time zone.)"""
    for b in bars:
        if _eastern_date(b.timestamp) == day:
            return b.close
    return None


def _alert_trade_close(account_kind: str, trade: ShadowTrade) -> None:
    """Fire-and-forget Discord alert for a closed shadow trade (Step 9.1).

    Pulls the post-close fields off the (now-committed) row.
    """
    from app.notify import discord

    if not discord.enabled():
        return
    realized = trade.realized_pnl_usd if trade.realized_pnl_usd is not None else Decimal(0)
    discord.fire_and_forget(
        discord.build_trade_close_embed(
            account_kind=account_kind,
            underlying=trade.underlying,
            occ_symbol=trade.occ_symbol,
            close_reason=trade.close_reason or "unknown",
            total_cost_usd=f"{trade.total_cost_usd:.2f}",
            realized_pnl_usd=f"{realized:.2f}",
        )
    )


@lru_cache(maxsize=1)
def get_outcome_resolver() -> OutcomeResolver:
    """App-wide OutcomeResolver bound to Alpaca + eval DB."""
    from app.data.registry import get_alpaca
    from app.eval.persistence import get_session_factory

    provider = get_alpaca()
    return OutcomeResolver(
        options_provider=provider,
        price_provider=provider,
        session_factory=get_session_factory(),
    )
