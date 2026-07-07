"""Provider registry — one shared instance (limiter/breaker/cache) per source.

Regression: prices/bars/chain routers, the agent's tool registry, and the
MtM/resolver jobs each used to construct their own AlpacaProvider — six
independent 200/min token buckets draining one API key's 200/min quota.
Every consumer must resolve to the registry's singleton.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.data.registry import get_alpaca, get_edgar, get_finnhub


@pytest.fixture
def fake_keys(monkeypatch: pytest.MonkeyPatch):
    """Fake API keys so providers can be constructed without a real .env,
    plus cache hygiene so other tests never see these instances."""
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    get_settings.cache_clear()
    get_alpaca.cache_clear()
    get_edgar.cache_clear()
    get_finnhub.cache_clear()
    yield
    get_alpaca.cache_clear()
    get_edgar.cache_clear()
    get_finnhub.cache_clear()
    get_settings.cache_clear()


def test_each_source_is_a_process_singleton(fake_keys) -> None:
    assert get_alpaca() is get_alpaca()
    assert get_edgar() is get_edgar()
    assert get_finnhub() is get_finnhub()


def test_api_routers_share_the_registry_alpaca(fake_keys) -> None:
    from app.api import bars, chain, prices

    shared = get_alpaca()
    assert prices._provider() is shared
    assert bars._provider() is shared
    assert chain._provider() is shared
