"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ENVIRONMENT=test and clear the settings cache so each test sees fresh env.

    DATABASE_URL defaults to a throwaway in-memory SQLite so no test can reach
    the developer's real Postgres through the project-root .env (tests that
    need a DB set their own file-backed URL). LLM router/provider caches and
    the shared health registry are reset so cooldowns never leak across tests.
    """
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    from app.core.config import get_settings
    from app.llm import factory, health

    get_settings.cache_clear()
    factory.get_router.cache_clear()
    factory.get_provider.cache_clear()
    health._registry = None
    yield
    get_settings.cache_clear()
    factory.get_router.cache_clear()
    factory.get_provider.cache_clear()
    health._registry = None
