"""One-shot migration from the SQLite dev DB to Postgres.

Walks each ORM model in dependency order, copies every row, preserves
primary keys so foreign-key references and the eval harness's accumulated
correlation IDs stay intact.

Both databases must already have the same schema (alembic upgrade head on
both before running). The script is idempotent on already-migrated rows
via INSERT ... ON CONFLICT DO NOTHING.

Run from `backend/` with:
    SOURCE_URL=sqlite+aiosqlite:///path/to/terminal.db \\
    TARGET_URL=postgresql+psycopg://terminal:terminal@localhost:5432/terminal \\
    uv run python scripts/migrate_sqlite_to_pg.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Make the app package importable when run as `python scripts/migrate_*.py`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.discovery.models import CatalystEvent, SeenDiscoveryEvent
from app.eval.models import Base, Thesis, ThesisOutcome
from app.portfolio.models import (
    PaperAccount,
    PositionMark,
    ShadowTrade,
)

# Migration order respects foreign-key dependencies. Children come last.
MODELS_IN_ORDER = [
    PaperAccount,
    Thesis,
    ThesisOutcome,
    ShadowTrade,
    PositionMark,
    SeenDiscoveryEvent,
    CatalystEvent,
]


async def copy_table(model: type[Base], source_session: Any, target_session: Any) -> int:
    rows = (await source_session.execute(select(model))).scalars().all()
    if not rows:
        return 0
    mapper = sa_inspect(model)
    pk_cols = [c.name for c in mapper.primary_key]
    payloads: list[dict[str, Any]] = []
    for row in rows:
        d = {c.key: getattr(row, c.key) for c in mapper.columns}
        payloads.append(d)
    # Use Postgres' ON CONFLICT DO NOTHING so re-runs are safe.
    table = model.__table__
    stmt = pg_insert(table).values(payloads)
    stmt = stmt.on_conflict_do_nothing(index_elements=pk_cols)
    await target_session.execute(stmt)
    await target_session.commit()
    return len(payloads)


async def reset_postgres_sequences(target_engine: Any) -> None:
    """After bulk INSERT with explicit IDs, Postgres SERIAL sequences are
    stale — they'd hand out IDs that already exist on next INSERT. Set
    each table's sequence to max(id) + 1."""
    async with target_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname AS seq, t.relname AS tbl, a.attname AS col "
                    "FROM pg_class c "
                    "JOIN pg_depend d ON d.objid = c.oid "
                    "JOIN pg_class t ON d.refobjid = t.oid "
                    "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid "
                    "WHERE c.relkind = 'S'"
                )
            )
        ).all()
        for r in rows:
            await conn.execute(
                text(
                    f"SELECT setval('{r.seq}', "
                    f"COALESCE((SELECT MAX({r.col}) FROM {r.tbl}), 0) + 1, false)"
                )
            )
        await conn.commit()


async def main() -> None:
    source_url = os.environ.get(
        "SOURCE_URL",
        "sqlite+aiosqlite:////Users/nikhilesh/Code/Projects/Terminal/backend/terminal.db",
    )
    target_url = os.environ.get(
        "TARGET_URL",
        "postgresql+psycopg://terminal:terminal@localhost:5432/terminal",
    )
    print(f"source: {source_url}")
    print(f"target: {target_url}")

    source = create_async_engine(source_url, future=True)
    target = create_async_engine(target_url, future=True)

    source_session_factory = async_sessionmaker(source, expire_on_commit=False)
    target_session_factory = async_sessionmaker(target, expire_on_commit=False)

    async with source_session_factory() as src, target_session_factory() as tgt:
        for model in MODELS_IN_ORDER:
            try:
                n = await copy_table(model, src, tgt)
                print(f"  {model.__tablename__}: {n} rows")
            except Exception as e:
                print(f"  {model.__tablename__}: FAILED — {e}")
                raise

    print("resetting Postgres sequences…")
    await reset_postgres_sequences(target)
    print("done.")

    await source.dispose()
    await target.dispose()


if __name__ == "__main__":
    asyncio.run(main())
