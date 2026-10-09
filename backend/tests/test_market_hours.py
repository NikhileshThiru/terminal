"""US equity session gate — weekday/hours logic, ET conversion (CLAUDE.md §7)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.core.market_hours import is_market_open

_ET = ZoneInfo("America/New_York")


def test_sunday_3am_is_closed() -> None:
    # The canonical CLAUDE.md example: 2026-07-05 is a Sunday.
    assert not is_market_open(datetime(2026, 7, 5, 3, 0, tzinfo=_ET))


def test_saturday_midday_is_closed() -> None:
    assert not is_market_open(datetime(2026, 7, 4, 12, 0, tzinfo=_ET))


def test_weekday_midsession_is_open() -> None:
    # 2026-07-07 is a Tuesday.
    assert is_market_open(datetime(2026, 7, 7, 11, 0, tzinfo=_ET))


def test_session_boundaries() -> None:
    tue = datetime(2026, 7, 7, 9, 29, tzinfo=_ET)
    assert not is_market_open(tue)
    assert is_market_open(tue.replace(hour=9, minute=30))
    assert is_market_open(tue.replace(hour=15, minute=59))
    assert not is_market_open(tue.replace(hour=16, minute=0))


def test_utc_input_is_converted_to_eastern() -> None:
    # 15:00 UTC on 2026-07-07 == 11:00 ET (EDT) — open.
    assert is_market_open(datetime(2026, 7, 7, 15, 0, tzinfo=UTC))
    # 02:00 UTC Tuesday == 22:00 ET Monday — closed (would look like a
    # weekday "2am" bug if the conversion were missing).
    assert not is_market_open(datetime(2026, 7, 7, 2, 0, tzinfo=UTC))
