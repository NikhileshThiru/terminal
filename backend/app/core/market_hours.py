"""US equity session awareness (CLAUDE.md §7: no 3am-Sunday theses).

Autonomous thesis generation is gated to the regular NYSE session so
theses are never generated — and shadow trades never filled — against
stale weekend/overnight quotes, which would pollute the eval buckets.

Holidays are deliberately NOT modeled: a handful of stale-quote days a
year is acceptable for a paper sim (Quote.safe_price() already refuses
one-sided quotes), and a hardcoded holiday table is a maintenance
landmine. Weekday + session hours capture ~98% of the value.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

_EASTERN = ZoneInfo("America/New_York")
_SESSION_OPEN = time(9, 30)
_SESSION_CLOSE = time(16, 0)


def is_market_open(now: datetime) -> bool:
    """True during the regular US equity session: weekdays 9:30-16:00 ET.

    `now` must be timezone-aware (UTC everywhere in this codebase); the
    ET conversion is what makes "3am Sunday UTC" vs "Tuesday 14:00 UTC"
    come out right across DST changes.
    """
    local = now.astimezone(_EASTERN)
    if local.weekday() >= 5:  # Saturday / Sunday
        return False
    return _SESSION_OPEN <= local.time() < _SESSION_CLOSE


def eastern_day_start(now: datetime) -> datetime:
    """Midnight US/Eastern of `now`'s trading day, as an aware UTC datetime.

    Every "per day" limit in the trading path (daily trade caps, thesis
    pacing) counts trading days, which roll over at ET midnight — not UTC
    midnight, which lands at 7-8pm ET mid-evening."""
    local_day = now.astimezone(_EASTERN).date()
    return datetime.combine(local_day, time.min, tzinfo=_EASTERN).astimezone(UTC)


def eastern_today() -> date:
    """Today's date on the US equity calendar (containers run on UTC, where
    "today" rolls over at 7-8pm ET)."""
    return datetime.now(UTC).astimezone(_EASTERN).date()
