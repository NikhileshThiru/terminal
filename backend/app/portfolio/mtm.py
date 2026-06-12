"""Mark-to-market service (DESIGN.md §8 reconciliation job).

Walks every open shadow trade, fetches a current quote for the contract,
and writes a `PositionMark` row with the unrealized P&L. Append-only so we
keep a full history per trade (enables an equity-curve chart later).

The service is intentionally narrow:
- It does NOT close positions (that's the OutcomeResolver's job).
- It does NOT mutate account equity (we recompute on read from marks +
  realized P&L; cheaper than fan-out updates).
- It does NOT crash on provider failures (one bad symbol shouldn't block
  every other position from being marked).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.data.interfaces import OptionsProvider, PriceProvider
from app.data.types import OptionContract, ProviderUnavailable
from app.portfolio.models import (
    PositionMark,
    ShadowTrade,
    ShadowTradeStatus,
)

_log = get_logger(__name__)

_CONTRACT_MULTIPLIER = Decimal(100)


@dataclass(frozen=True)
class MarkResult:
    """Summary of one tick of `MtMService.mark_all_open()`."""

    marks_written: int
    skipped: int  # provider unavailable, no quote, etc.
    errors: int  # unexpected exceptions
    total_open_positions: int


def _mid_price(contract: OptionContract) -> Decimal | None:
    """Mid of bid/ask if both are present and positive; else last; else None."""
    if contract.bid is not None and contract.ask is not None and contract.bid > 0 < contract.ask:
        return (contract.bid + contract.ask) / Decimal(2)
    if contract.last is not None and contract.last > 0:
        return contract.last
    return None


class MtMService:
    """Marks open shadow positions to market."""

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

    async def mark_all_open(self) -> MarkResult:
        """One tick: revalue every open shadow trade. Returns a counts summary."""
        async with self._session_factory() as session:
            open_trades: list[ShadowTrade] = list(
                (
                    await session.execute(
                        select(ShadowTrade).where(
                            ShadowTrade.status == ShadowTradeStatus.SHADOW_OPEN.value
                        )
                    )
                )
                .scalars()
                .all()
            )

            marks = skipped = errors = 0
            for trade in open_trades:
                try:
                    mark = await self._mark_one(trade)
                except ProviderUnavailable as e:
                    skipped += 1
                    _log.warning(
                        "mtm_skipped",
                        trade_id=trade.id,
                        occ=trade.occ_symbol,
                        reason=e.reason.value,
                    )
                    continue
                except Exception:
                    errors += 1
                    _log.exception("mtm_unexpected_error", trade_id=trade.id)
                    continue
                if mark is None:
                    skipped += 1
                    continue
                session.add(mark)
                marks += 1

            if marks:
                await session.commit()
                _log.info("mtm_tick_committed", marks=marks, skipped=skipped, errors=errors)
            return MarkResult(
                marks_written=marks,
                skipped=skipped,
                errors=errors,
                total_open_positions=len(open_trades),
            )

    async def _mark_one(self, trade: ShadowTrade) -> PositionMark | None:
        """Fetch current quote + underlying, build a PositionMark. Returns None
        if the provider gave back nothing usable (no bid/ask/last)."""
        contract = await self._options.get_contract_quote(trade.occ_symbol)
        mid = _mid_price(contract)
        if mid is None:
            return None

        underlying_quote = await self._prices.get_latest_quote(trade.underlying)
        # Zero-guarded: a one-sided after-hours quote yields None and we skip
        # this mark rather than recording a halved price (Quote.safe_price).
        underlying_price = underlying_quote.safe_price()
        if underlying_price is None:
            return None

        unrealized = (
            (mid - trade.premium_per_contract_usd) * _CONTRACT_MULTIPLIER * Decimal(trade.contracts)
        )
        return PositionMark(
            shadow_trade_id=trade.id,
            marked_at=datetime.now(UTC),
            underlying_price_usd=underlying_price,
            option_mid_usd=mid,
            mark_price_per_contract_usd=mid,
            unrealized_pnl_usd=unrealized,
        )


@lru_cache(maxsize=1)
def get_mtm_service() -> MtMService:
    """App-wide MtMService bound to the live Alpaca provider + eval DB."""
    from app.data.alpaca import AlpacaProvider
    from app.eval.persistence import get_session_factory

    provider = AlpacaProvider()
    return MtMService(
        options_provider=provider,
        price_provider=provider,
        session_factory=get_session_factory(),
    )
