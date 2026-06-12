"""Outcome resolver — closes shadow trades and writes ThesisOutcome rows.

This is the "did the thesis turn out right?" half of the eval loop. The
resolver walks every open shadow trade, decides if it should close, computes
the directional outcome of the underlying, and writes the row that the
scoring functions in `app/eval/scoring.py` aggregate over.

Two concerns kept separate:
1. Trade close — fills `shadow_trades.closed_at + close_reason + realized_pnl`.
   The realized P&L is for the equity-curve story, not the eval.
2. Thesis outcome — fills `thesis_outcomes.realized_direction + hit + pct_move`.
   This is for the calibration plot. It scores the UNDERLYING'S direction
   against the thesis's stated direction, independently of options P&L.
   (A long call can lose money even if the underlying moved up — theta,
   IV crush. Scoring the option's P&L would punish the thesis for the
   trade structure, not the prediction.)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.data.interfaces import OptionsProvider, PriceProvider
from app.data.types import ProviderUnavailable
from app.eval.models import RealizedDirection, ThesisOutcome
from app.eval.models import Thesis as ThesisRow
from app.portfolio.models import (
    CloseReason,
    ShadowTrade,
    ShadowTradeStatus,
)
from app.portfolio.mtm import _mid_price

_log = get_logger(__name__)

_CONTRACT_MULTIPLIER = Decimal(100)
# A move smaller than this (in % terms) is "flat" — neither up nor down.
# Stops random walk noise from being scored as a directional hit/miss.
_FLAT_BAND_PCT = 0.5  # ±0.5% counts as flat


@dataclass(frozen=True)
class ResolveResult:
    """Counts summary for one tick of `OutcomeResolver.resolve_all()`."""

    closed: int
    outcomes_written: int
    skipped: int
    errors: int


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
) -> CloseReason | None:
    """Decide if/why a trade should close. Returns None if it should stay open.

    Order of precedence: expiration first (it's binding), then stop-loss
    (capital preservation), then theta exit (avoid late decay). The thesis's
    exit fields live on the JSON suggested_contract blob; this reads them
    defensively in case they're missing.
    """
    if today >= trade.expiration:
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
    """Walks open shadow trades, closes them, and writes ThesisOutcome rows."""

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
        today = now.date()

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

            closed = outcomes = skipped = errors = 0
            for trade in open_trades:
                try:
                    did_close = await self._maybe_resolve_one(session, trade, now, today)
                except ProviderUnavailable as e:
                    skipped += 1
                    _log.warning(
                        "resolver_skipped",
                        trade_id=trade.id,
                        reason=e.reason.value,
                    )
                    continue
                except Exception:
                    errors += 1
                    _log.exception("resolver_unexpected_error", trade_id=trade.id)
                    continue
                if did_close:
                    closed += 1
                    outcomes += 1

            if closed:
                await session.commit()
                _log.info(
                    "resolver_tick_committed",
                    closed=closed,
                    outcomes=outcomes,
                    skipped=skipped,
                    errors=errors,
                )

            return ResolveResult(
                closed=closed, outcomes_written=outcomes, skipped=skipped, errors=errors
            )

    async def _maybe_resolve_one(
        self,
        session: AsyncSession,
        trade: ShadowTrade,
        now: datetime,
        today: date,
    ) -> bool:
        """Decide if `trade` should close; if so, write ThesisOutcome + update row.

        Returns True if the trade was closed in this tick.
        """
        # Current underlying price (fail-fast on provider issues; the loop will
        # skip + retry next tick).
        underlying_now_q = await self._prices.get_latest_quote(trade.underlying)
        # Zero-guarded (Quote.safe_price): a one-sided after-hours quote must
        # NOT grade a thesis or trip an exit rule — it once read as a phantom
        # -50% move. No usable price → defer to the next tick.
        underlying_now = underlying_now_q.safe_price()
        if underlying_now is None:
            return False

        # Load the thesis row first — close-reason logic reads its
        # exit_if_underlying_* + close_n_days_before_expiry fields.
        thesis = await session.get(ThesisRow, trade.thesis_id)
        if thesis is None:
            _log.warning("resolver_thesis_missing", thesis_id=trade.thesis_id)
            return False

        reason = _choose_close_reason(trade, thesis, today, underlying_now)
        if reason is None:
            return False

        # Determine the option's close price. For EXPIRED, intrinsic value
        # at expiration. For all other reasons, current mid (or last).
        close_price = await self._close_price(trade, reason, underlying_now)
        realized_pnl = (
            (close_price - trade.premium_per_contract_usd)
            * _CONTRACT_MULTIPLIER
            * Decimal(trade.contracts)
        )

        # Look up underlying price at trade-open via daily OHLC.
        underlying_at_open = await self._underlying_at_open(trade)
        if underlying_at_open is None:
            _log.warning(
                "resolver_missing_open_price",
                trade_id=trade.id,
                underlying=trade.underlying,
                opened_at=trade.opened_at.isoformat(),
            )
            return False

        pct_move = float(
            ((underlying_now - underlying_at_open) / underlying_at_open) * Decimal(100)
        )
        realized = _classify_direction(pct_move)
        hit = (thesis.direction == realized) and realized != RealizedDirection.FLAT.value

        # Update the trade row.
        trade.status = ShadowTradeStatus.SHADOW_CLOSED.value
        trade.closed_at = now
        trade.close_reason = reason.value
        trade.close_price_per_contract_usd = close_price
        trade.realized_pnl_usd = realized_pnl

        # Write the outcome.
        outcome = ThesisOutcome(
            thesis_id=trade.thesis_id,
            evaluated_at=now,
            realized_direction=realized,
            hit=hit,
            underlying_price_at_thesis=underlying_at_open,
            underlying_price_at_eval=underlying_now,
            pct_move=pct_move,
            notes=f"shadow_trade_id={trade.id}, close_reason={reason.value}",
        )
        session.add(outcome)

        _log.info(
            "resolver_closed_trade",
            trade_id=trade.id,
            thesis_id=trade.thesis_id,
            reason=reason.value,
            pct_move=pct_move,
            realized=realized,
            hit=hit,
        )
        return True

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

    async def _underlying_at_open(self, trade: ShadowTrade) -> Decimal | None:
        """Look up the underlying close on the day the trade opened (via OHLC)."""
        open_day = trade.opened_at.date()
        # Pull a small window so we get a bar even if open_day was a weekend.
        try:
            bars = await self._prices.get_ohlc(
                trade.underlying,
                start=trade.opened_at - timedelta(days=5),
                end=trade.opened_at + timedelta(days=1),
            )
        except ProviderUnavailable:
            return None
        # Prefer the bar for the exact open day; otherwise the most recent
        # bar at or before open_day.
        exact = [b for b in bars if b.timestamp.date() == open_day]
        if exact:
            return exact[0].close
        prior = [b for b in bars if b.timestamp.date() <= open_day]
        if prior:
            return max(prior, key=lambda b: b.timestamp).close
        return None


@lru_cache(maxsize=1)
def get_outcome_resolver() -> OutcomeResolver:
    """App-wide OutcomeResolver bound to Alpaca + eval DB."""
    from app.data.alpaca import AlpacaProvider
    from app.eval.persistence import get_session_factory

    provider = AlpacaProvider()
    return OutcomeResolver(
        options_provider=provider,
        price_provider=provider,
        session_factory=get_session_factory(),
    )
