"""Thesis persistence — write a Thesis row to the eval DB.

The DB engine is async (SQLAlchemy 2.0 + aiosqlite for dev, asyncpg-style
psycopg for Postgres). The session factory lives at module level so callers
just `await write_thesis(thesis)` without engine-management ceremony.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.agent.schemas import Thesis as ThesisDTO
from app.core.config import get_settings
from app.core.logging import get_logger
from app.eval.models import Thesis as ThesisRow

_log = get_logger(__name__)


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return a cached async engine bound to DATABASE_URL.

    Driver in URL determines async backend (sqlite+aiosqlite, postgresql+psycopg).

    For SQLite (dev), enables WAL mode + a 30-second busy timeout so the
    autonomous worker's concurrent writes (EDGAR poller, news WS, MTM job)
    don't trip "database is locked" errors. WAL allows readers to proceed
    while one writer is active; busy_timeout makes SQLite wait on a held
    lock instead of failing immediately.
    """
    url = get_settings().database_url
    connect_args: dict[str, Any] = {}
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        connect_args["timeout"] = 30.0
    engine = create_async_engine(url, future=True, connect_args=connect_args)

    if is_sqlite:
        # PRAGMAs apply per-connection; this event fires on every new
        # connection the pool hands out.
        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection: Any, _conn_record: Any) -> None:
            cur = dbapi_connection.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA synchronous=NORMAL")
            finally:
                cur.close()

    return engine


@lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def write_thesis(thesis: ThesisDTO) -> int:
    """Insert a Thesis row. Returns the new id."""
    factory = get_session_factory()
    async with factory() as session:
        row = ThesisRow(**thesis.to_orm_kwargs())
        session.add(row)
        await session.commit()
        await session.refresh(row)
        _log.info(
            "thesis_persisted",
            thesis_id=row.id,
            symbol=row.symbol,
            direction=row.direction,
            confidence=row.confidence,
            grounding_passed=row.grounding_check_passed,
            funnel_latency_ms=row.funnel_latency_ms,
            correlation_id=row.correlation_id,
        )
        _alert_thesis(row)
        return int(row.id)


def _alert_thesis(row: ThesisRow) -> None:
    """Fire-and-forget Discord alert for a persisted thesis (Step 9.1)."""
    # Local import keeps the eval module free of an httpx dep at import time.
    from app.notify import discord

    if not discord.enabled():
        return
    contract = row.suggested_contract or {}
    discord.fire_and_forget(
        discord.build_thesis_embed(
            symbol=row.symbol,
            direction=row.direction,
            confidence=row.confidence,
            source_bucket=row.source_bucket,
            reasoning=row.reasoning,
            grounding_passed=row.grounding_check_passed,
            occ_symbol=contract.get("occ_symbol"),
            max_risk_usd=contract.get("max_risk_usd"),
        )
    )
