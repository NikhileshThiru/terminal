"""Settings load and behave sensibly."""

from __future__ import annotations

import pytest

from app.core.config import Settings, get_settings


def test_settings_loads_with_defaults() -> None:
    s = Settings()
    assert s.environment in ("development", "test", "production")
    assert s.log_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
    assert len(s.watchlist) > 0
    assert s.funnel_catalyst_budget_seconds > 0
    assert s.funnel_reactive_budget_seconds > s.funnel_catalyst_budget_seconds
    assert 0.0 < s.thesis_min_confidence < 1.0
    assert s.conservative_account_min_confidence > s.aggressive_account_min_confidence, (
        "Conservative account should require higher confidence than aggressive"
    )


def test_get_settings_is_cached() -> None:
    a = get_settings()
    b = get_settings()
    assert a is b


def test_watchlist_accepts_comma_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env-var watchlist (a string) should be parsed into a list."""
    monkeypatch.setenv("WATCHLIST", "aapl, msft,  nvda")
    get_settings.cache_clear()
    s = get_settings()
    assert s.watchlist == ["AAPL", "MSFT", "NVDA"]
