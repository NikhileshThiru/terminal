"""Parse free-tier quota errors into a cooldown the router can act on.

Both cloud LLMs tell you how long to back off, just in different dialects:

- Gemini (google-genai ClientError text): ``'retryDelay': '41s'`` plus a
  QuotaFailure whose ``quotaId`` names the window, e.g.
  ``GenerateRequestsPerDayPerProjectPerModel-FreeTier``.
- Groq (HTTP 429 body + ``retry-after`` header): ``Rate limit reached for
  model `openai/gpt-oss-20b` ... on tokens per day (TPD) ... Please try again
  in 15m29.952s.``

A per-minute 429 is worth waiting out inline; a per-day 429 is not — the
quota will not come back for hours, so the right move is to fail over to the
next route immediately and park this one until the reset.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

_PACIFIC = ZoneInfo("America/Los_Angeles")

# "retryDelay': '41s'" / "retryDelay": "41.5s" (Gemini RetryInfo)
_RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"](\d+(?:\.\d+)?)s['\"]")
# "try again in 15m29.952s" / "retry in 41.2s" / "try again in 1h2m" (Groq, Gemini prose)
_TRY_AGAIN = re.compile(
    r"(?:try again|retry) in\s+((?:\d+(?:\.\d+)?h)?(?:\d+(?:\.\d+)?m(?!s))?(?:\d+(?:\.\d+)?s)?)",
    re.IGNORECASE,
)
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)([hms])")
_UNIT_SECONDS = {"h": 3600.0, "m": 60.0, "s": 1.0}

_DAILY_MARKERS = ("perday", "per day", "(tpd)", "(rpd)", "tokens per day", "requests per day")
_RATE_LIMIT_MARKERS = ("429", "resource_exhausted", "rate limit", "quota")


def _parse_duration(text: str) -> float | None:
    parts = _DURATION_PART.findall(text)
    if not parts:
        return None
    return sum(float(n) * _UNIT_SECONDS[u] for n, u in parts)


def parse_retry_after(text: str) -> float | None:
    """Best-effort "seconds until retry" from an upstream error message."""
    m = _RETRY_DELAY.search(text)
    if m:
        return float(m.group(1))
    m = _TRY_AGAIN.search(text)
    if m and m.group(1):
        return _parse_duration(m.group(1))
    return None


def is_daily_quota(text: str) -> bool:
    """True when the exhausted window is a per-day one (won't recover soon)."""
    lowered = text.lower()
    return any(marker in lowered for marker in _DAILY_MARKERS)


def is_rate_limit(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


def seconds_until_pacific_midnight(now: datetime | None = None) -> float:
    """Gemini free-tier daily quotas reset at midnight US/Pacific."""
    current = (now or datetime.now(UTC)).astimezone(_PACIFIC)
    tomorrow = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - current).total_seconds()
