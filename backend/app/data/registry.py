"""Process-wide provider singletons — one instance per external source.

The rate limiter, circuit breaker, and cache all live on the provider
instance. N instances of a provider means N independent token buckets
draining ONE API key's documented quota (N x 200/min against Alpaca's
200/min), N breakers that don't learn from each other's 429s, and N caches
fetching the same symbol redundantly. Every consumer — API routers, the
agent's tool registry, the MtM/resolver jobs, the discovery worker — must
get its provider from here, never construct one directly.
"""

from __future__ import annotations

from functools import lru_cache

from app.data.alpaca import AlpacaProvider
from app.data.edgar import EdgarFilingsProvider
from app.data.finnhub import FinnhubProvider


@lru_cache(maxsize=1)
def get_alpaca() -> AlpacaProvider:
    return AlpacaProvider()


@lru_cache(maxsize=1)
def get_edgar() -> EdgarFilingsProvider:
    return EdgarFilingsProvider()


@lru_cache(maxsize=1)
def get_finnhub() -> FinnhubProvider:
    return FinnhubProvider()
