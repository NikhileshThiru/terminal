"""Dialect-aware upsert helper.

Both Postgres and SQLite support `INSERT ... ON CONFLICT`, but the
syntax lives in different dialect modules. Hard-coding one breaks the
other: a SQLite-built `Insert` against a Postgres session raises
`AttributeError: 'OnConflictDoNothing' object has no attribute
'constraint_target'`, and a Postgres-built `Insert` against SQLite is
similarly mismatched.

Pick the correct dialect at runtime based on the session's bind. Tests
use SQLite in-memory, prod uses Postgres — both keep working.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession


def dialect_insert(session: AsyncSession) -> Any:
    """Return the dialect-appropriate `insert()` callable for `session`.

    Usage:
        insert = dialect_insert(session)
        stmt = insert(SomeTable).values(...).on_conflict_do_nothing(
            index_elements=["col_a", "col_b"]
        )
        await session.execute(stmt)
    """
    bind = session.bind
    name = bind.dialect.name if bind is not None else "postgresql"
    if name == "postgresql":
        return pg_insert
    return sqlite_insert
