"""PaperEngine — turns a Thesis into ShadowTrade rows per account (DESIGN.md §8).

Shadow mode only in Phase 7 MVP: trades are RECORDED but no position actually
moves, no equity is updated. Same plumbing will switch to real paper-trade
execution in a follow-up — graduate from shadow once Brier/calibration look
sane on the shadow log (DESIGN.md §8 go/no-go criteria).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.schemas import Thesis
from app.core.logging import get_logger
from app.portfolio.models import (
    PaperAccount,
    ShadowTrade,
    ShadowTradeStatus,
)
from app.portfolio.risk import AccountState, RiskDecision, RiskEngine

_log = get_logger(__name__)


@dataclass(frozen=True)
class AccountDecision:
    """One account's RiskEngine verdict, with the resulting trade (if any)."""

    account_kind: str
    decision: RiskDecision
    trade: ShadowTrade | None  # populated when decision.approved


class PaperEngine:
    """Routes theses through the risk engine and writes ShadowTrades.

    The engine never raises on per-account errors — it logs them, records
    the rejection, and continues. One bad account doesn't block the other.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        risk_engine: type[RiskEngine] = RiskEngine,
    ) -> None:
        self._session_factory = session_factory
        self._risk = risk_engine

    async def consider_thesis(self, thesis_id: int, thesis: Thesis) -> list[AccountDecision]:
        """Run every enabled account's risk gate over this thesis.

        Returns a verdict per account (so callers can see rejections too).
        Persists only approved trades.
        """
        decisions: list[AccountDecision] = []
        async with self._session_factory() as session:
            accounts = (
                (await session.execute(select(PaperAccount).order_by(PaperAccount.kind)))
                .scalars()
                .all()
            )

            for account in accounts:
                try:
                    decision = await self._evaluate_one(session, thesis_id, thesis, account)
                except Exception as e:
                    _log.exception(
                        "paper_engine_account_error",
                        account=account.kind,
                        thesis_id=thesis_id,
                        error=str(e),
                    )
                    decisions.append(
                        AccountDecision(
                            account_kind=account.kind,
                            decision=RiskDecision(
                                approved=False,
                                contracts=0,
                                total_cost_usd=thesis.suggested_contract.max_risk_usd * 0,
                                reason=f"internal error: {type(e).__name__}: {e}",
                            ),
                            trade=None,
                        )
                    )
                else:
                    decisions.append(decision)
        return decisions

    async def _evaluate_one(
        self,
        session: AsyncSession,
        thesis_id: int,
        thesis: Thesis,
        account: PaperAccount,
    ) -> AccountDecision:
        state = await self._load_state(session, account.id)
        verdict = self._risk.evaluate(thesis, account, state)

        if not verdict.approved:
            _log.info(
                "paper_engine_rejected",
                account=account.kind,
                thesis_id=thesis_id,
                reason=verdict.reason,
            )
            return AccountDecision(account_kind=account.kind, decision=verdict, trade=None)

        trade = ShadowTrade(
            account_id=account.id,
            thesis_id=thesis_id,
            opened_at=datetime.now(UTC),
            underlying=thesis.suggested_contract.underlying,
            occ_symbol=thesis.suggested_contract.occ_symbol,
            option_type=thesis.suggested_contract.option_type,
            strike=thesis.suggested_contract.strike,
            expiration=thesis.suggested_contract.expiration,
            contracts=verdict.contracts,
            premium_per_contract_usd=thesis.suggested_contract.estimated_premium_per_contract,
            total_cost_usd=verdict.total_cost_usd,
            status=ShadowTradeStatus.SHADOW_OPEN.value,
            risk_reason=verdict.reason,
        )
        session.add(trade)
        await session.commit()
        await session.refresh(trade)
        _log.info(
            "paper_engine_shadow_trade",
            account=account.kind,
            thesis_id=thesis_id,
            trade_id=trade.id,
            symbol=thesis.symbol,
            contracts=verdict.contracts,
            cost_usd=str(verdict.total_cost_usd),
        )
        return AccountDecision(account_kind=account.kind, decision=verdict, trade=trade)

    @staticmethod
    async def _load_state(session: AsyncSession, account_id: int) -> AccountState:
        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        trades_today = await session.scalar(
            select(func.count(ShadowTrade.id))
            .where(ShadowTrade.account_id == account_id)
            .where(ShadowTrade.opened_at >= today_start)
        )
        open_positions = await session.scalar(
            select(func.count(ShadowTrade.id))
            .where(ShadowTrade.account_id == account_id)
            .where(ShadowTrade.status == ShadowTradeStatus.SHADOW_OPEN.value)
        )
        return AccountState(
            trades_today=int(trades_today or 0),
            open_positions=int(open_positions or 0),
        )


@lru_cache(maxsize=1)
def get_paper_engine() -> PaperEngine:
    """App-wide PaperEngine bound to the eval-DB session factory."""
    from app.eval.persistence import get_session_factory

    return PaperEngine(get_session_factory())
