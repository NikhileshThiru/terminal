"""Per-route LLM health: cooldowns after quota errors + counters for the UI.

Keyed by (provider, model) because free-tier quotas are per model: Gemini
2.5 Flash running out says nothing about 2.5 Flash-Lite, and each Groq model
has its own 1,000 RPD. Process-global and shared by every router, so the
triage and thesis steps see each other's 429s.

Cooldown policy (the July 2026 incident: both providers' daily caps were
exhausted mid-session and every subsequent event burned three retries on a
dead quota before failing):

- RATE_LIMITED with an upstream hint  -> cool down for exactly that long.
- RATE_LIMITED without a hint         -> 60s, doubling per consecutive 429,
                                         capped (config).
- MODEL_NOT_FOUND / AUTH_MISSING      -> park for an hour; log loudly. A
                                         retired model or bad key won't fix
                                         itself between events.
- any other failure                   -> no cooldown; the router just tries
                                         the next route for this one call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock

from app.data.types import ProviderUnavailable, ProviderUnavailableReason

_PARK_SECONDS = 3600.0


@dataclass
class RouteHealth:
    provider: str
    model: str
    calls: int = 0
    successes: int = 0
    failures: int = 0
    rate_limited: int = 0
    consecutive_rate_limits: int = 0
    cooldown_until: datetime | None = None
    last_error: str | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None

    def cooling(self, now: datetime | None = None) -> bool:
        return self.cooldown_until is not None and (now or datetime.now(UTC)) < self.cooldown_until

    def to_dict(self, now: datetime | None = None) -> dict[str, object]:
        current = now or datetime.now(UTC)
        return {
            "provider": self.provider,
            "model": self.model,
            "state": "cooling" if self.cooling(current) else "ok",
            "cooldown_until": self.cooldown_until if self.cooling(current) else None,
            "calls": self.calls,
            "successes": self.successes,
            "failures": self.failures,
            "rate_limited": self.rate_limited,
            "last_error": self.last_error,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
        }


class LLMHealthRegistry:
    def __init__(
        self, *, default_cooldown_seconds: float = 60.0, max_cooldown_seconds: float = 1800.0
    ) -> None:
        self._lock = Lock()
        self._routes: dict[tuple[str, str], RouteHealth] = {}
        self.default_cooldown_seconds = default_cooldown_seconds
        self.max_cooldown_seconds = max_cooldown_seconds

    def get(self, provider: str, model: str) -> RouteHealth:
        key = (provider, model)
        with self._lock:
            health = self._routes.get(key)
            if health is None:
                health = RouteHealth(provider=provider, model=model)
                self._routes[key] = health
            return health

    def record_success(self, provider: str, model: str) -> None:
        h = self.get(provider, model)
        with self._lock:
            h.calls += 1
            h.successes += 1
            h.consecutive_rate_limits = 0
            h.cooldown_until = None
            h.last_success_at = datetime.now(UTC)

    def record_failure(self, provider: str, model: str, error: Exception) -> float | None:
        """Record a failed call. Returns the cooldown applied (seconds), if any."""
        h = self.get(provider, model)
        now = datetime.now(UTC)
        cooldown: float | None = None
        if isinstance(error, ProviderUnavailable):
            if error.reason == ProviderUnavailableReason.RATE_LIMITED:
                cooldown = self._rate_limit_cooldown(h, error.retry_after_seconds)
            elif error.reason in (
                ProviderUnavailableReason.MODEL_NOT_FOUND,
                ProviderUnavailableReason.AUTH_MISSING,
            ):
                cooldown = _PARK_SECONDS
        with self._lock:
            h.calls += 1
            h.failures += 1
            h.last_failure_at = now
            h.last_error = f"{type(error).__name__}: {error}"[:300]
            if isinstance(error, ProviderUnavailable) and (
                error.reason == ProviderUnavailableReason.RATE_LIMITED
            ):
                h.rate_limited += 1
                h.consecutive_rate_limits += 1
            if cooldown is not None:
                h.cooldown_until = now + timedelta(seconds=cooldown)
        return cooldown

    def _rate_limit_cooldown(self, h: RouteHealth, hint: float | None) -> float:
        if hint is not None and hint > 0:
            return hint
        # consecutive_rate_limits is incremented after this is computed, so the
        # first 429 cools for the default and each repeat doubles it.
        doubled = self.default_cooldown_seconds * (2.0**h.consecutive_rate_limits)
        return min(doubled, self.max_cooldown_seconds)

    def snapshot(self) -> list[dict[str, object]]:
        now = datetime.now(UTC)
        with self._lock:
            routes = list(self._routes.values())
        return [r.to_dict(now) for r in routes]

    def reset(self) -> None:
        with self._lock:
            self._routes.clear()


_registry: LLMHealthRegistry | None = None


def get_llm_health() -> LLMHealthRegistry:
    global _registry
    if _registry is None:
        from app.core.config import get_settings

        s = get_settings()
        _registry = LLMHealthRegistry(
            default_cooldown_seconds=s.llm_default_cooldown_seconds,
            max_cooldown_seconds=s.llm_max_cooldown_seconds,
        )
    return _registry
