"""Default account seeding (DESIGN.md §8).

Runs once on app startup. Idempotent — only inserts accounts that don't
already exist. Constants come from DESIGN.md §8's conservative/aggressive
contrast description.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.logging import get_logger
from app.portfolio.models import AccountKind, PaperAccount

_log = get_logger(__name__)


def _default_accounts() -> list[PaperAccount]:
    """Build the two default accounts from settings (DESIGN.md §2.7 config-driven)."""
    settings = get_settings()
    now = datetime.now(UTC)
    return [
        PaperAccount(
            kind=AccountKind.CONSERVATIVE.value,
            name="Conservative",
            starting_balance_usd=Decimal("100000.00"),
            equity_usd=Decimal("100000.00"),
            # Higher confidence threshold, smaller positions, fewer trades.
            min_confidence=settings.conservative_account_min_confidence,
            max_trade_cost_usd=Decimal(str(settings.conservative_account_max_trade_cost_usd)),
            max_trades_per_day=settings.conservative_account_max_trades_per_day,
            max_concurrent_positions=settings.conservative_account_max_concurrent_positions,
            kill_switch=False,
            created_at=now,
        ),
        PaperAccount(
            kind=AccountKind.AGGRESSIVE.value,
            name="Aggressive",
            starting_balance_usd=Decimal("100000.00"),
            equity_usd=Decimal("100000.00"),
            # Lower threshold, bigger positions, more trades.
            min_confidence=settings.aggressive_account_min_confidence,
            max_trade_cost_usd=Decimal(str(settings.aggressive_account_max_trade_cost_usd)),
            max_trades_per_day=settings.aggressive_account_max_trades_per_day,
            max_concurrent_positions=settings.aggressive_account_max_concurrent_positions,
            kill_switch=False,
            created_at=now,
        ),
    ]


async def seed_default_accounts(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[str]:
    """Ensure default accounts exist. Returns the kinds that were newly created."""
    async with session_factory() as session:
        existing = set((await session.execute(select(PaperAccount.kind))).scalars().all())
        created: list[str] = []
        for account in _default_accounts():
            if account.kind in existing:
                continue
            session.add(account)
            created.append(account.kind)
        if created:
            await session.commit()
            _log.info("paper_accounts_seeded", created=created)
        else:
            _log.info("paper_accounts_already_seeded")
        return created
