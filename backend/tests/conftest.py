"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ENVIRONMENT=test and clear the settings cache so each test sees fresh env."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
