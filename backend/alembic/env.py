"""Alembic migrations environment.

Reads DATABASE_URL from the app's settings (which loads from .env / environment)
so we never duplicate connection config between Alembic and the app.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make `app` importable when alembic is run from the backend dir.
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings
from app.discovery import (
    models as _discovery_models,  # noqa: F401 — register tables with Base.metadata
)
from app.eval.models import Base
from app.portfolio import (
    models as _portfolio_models,  # noqa: F401 — register tables with Base.metadata
)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override the sqlalchemy.url at runtime with our settings. Alembic uses a
# sync engine, so drop the async driver qualifier ("+aiosqlite", "+asyncpg")
# if present — psycopg works for both, aiosqlite has a sync sibling at the
# bare "sqlite" driver.
settings = get_settings()
runtime_url = os.environ.get("DATABASE_URL", settings.database_url)
sync_url = runtime_url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg")
config.set_main_option("sqlalchemy.url", sync_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
