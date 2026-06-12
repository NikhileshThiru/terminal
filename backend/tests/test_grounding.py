"""Grounding check: extract numbers from reasoning, verify they appear in tool data."""

from __future__ import annotations

from datetime import UTC, datetime

from app.agent.grounding import (
    _close,
    _collect_numbers,
    _extract_numbers,
    _is_year_like,
    check_grounding,
)
from app.agent.tools import ToolResult

# === Number extraction ===


def test_extract_numbers_handles_dollars_percent_decimals() -> None:
    text = "AAPL last traded at $312.06, beat earnings by 10.86%, with 1.5 million volume."
    out = _extract_numbers(text)
    # We should pick up 312.06, 10.86, 1.5 at minimum.
    assert 312.06 in out
    assert 10.86 in out
    assert 1.5 in out


def test_extract_numbers_skips_dollar_prefix_attached_to_letter_or_id() -> None:
    text = "Ticker$ABC and account_id_3 should be ignored, but 42 counts."
    out = _extract_numbers(text)
    assert 42 in out
    # The regex's lookbehind blocks numbers attached to letters/underscores.
    assert 3 not in out  # part of account_id_3


def test_is_year_like() -> None:
    assert _is_year_like(2026)
    assert _is_year_like(2026.0)
    assert not _is_year_like(2026.5)
    assert not _is_year_like(312)


# === Number collection from nested data ===


def test_collect_numbers_walks_nested_dict_and_list() -> None:
    data = {
        "symbol": "AAPL",
        "last": "312.06",  # Decimal serialized as string
        "bars": [{"close": 302.25, "volume": 38392499}],
        "active": True,  # bool — should NOT be treated as 1.0
    }
    nums = _collect_numbers(data)
    assert 312.06 in nums
    assert 302.25 in nums
    assert 38392499 in nums
    assert 1.0 not in nums  # bool excluded


# === Tolerance ===


def test_close_matches_exact() -> None:
    assert _close(5.0, 5.0)


def test_close_within_abs_tol() -> None:
    assert _close(5.0, 5.3, abs_tol=0.5)
    assert not _close(5.0, 6.0, abs_tol=0.5, rel_tol=0.0)


def test_close_within_rel_tol() -> None:
    assert _close(100.0, 101.5, rel_tol=0.02)  # 1.5% diff
    assert not _close(100.0, 120.0, rel_tol=0.02)  # 20% diff


# === The check itself ===


def _result(name: str, data: object) -> ToolResult:
    return ToolResult(
        tool_name=name,
        arguments={},
        success=True,
        data=data,
        provider="test",
        fetched_at=datetime.now(UTC),
    )


def test_all_numbers_match_passes() -> None:
    reasoning = "AAPL at $312.06 had earnings surprise of 10.86% and average volume 38392499."
    tools = [
        _result("get_quote", {"last": 312.06}),
        _result("get_earnings_context", {"recent_surprises": [{"surprise_pct": 10.86}]}),
        _result("get_ohlc", {"bars": [{"volume": 38392499}]}),
    ]
    r = check_grounding(reasoning, tools)
    assert r.passed is True
    assert r.unverified_numbers == []
    assert sorted(r.tools_used) == ["get_earnings_context", "get_ohlc", "get_quote"]


def test_hallucinated_number_fails() -> None:
    reasoning = "AAPL at $312.06 had a wild 67.4% surprise."
    tools = [
        _result("get_quote", {"last": 312.06}),
        _result("get_earnings_context", {"recent_surprises": [{"surprise_pct": 10.86}]}),
    ]
    r = check_grounding(reasoning, tools)
    assert r.passed is False
    assert 67.4 in r.unverified_numbers
    assert r.notes is not None and "67.4" in r.notes


def test_year_like_integers_dont_break_passing() -> None:
    """Years like 2026 should be ignored — they're rarely in tool data."""
    reasoning = "In 2026, AAPL has traded at $312.06."
    tools = [_result("get_quote", {"last": 312.06})]
    r = check_grounding(reasoning, tools)
    assert r.passed is True


def test_tiny_numbers_ignored() -> None:
    """Numbers <= min_significant (default 2.0) are too noisy to ground."""
    reasoning = "AAPL has 1 risk factor, but $312.06 is verified."
    tools = [_result("get_quote", {"last": 312.06})]
    r = check_grounding(reasoning, tools)
    assert r.passed is True


def test_close_match_within_tolerance_passes() -> None:
    """LLM rounding: thesis says $312, tool says $312.06 — should match."""
    reasoning = "AAPL at $312, my plan is..."
    tools = [_result("get_quote", {"last": 312.06})]
    r = check_grounding(reasoning, tools)
    assert r.passed is True


def test_whitelisted_constants_dont_trip_grounding() -> None:
    """The OCC contract multiplier (100) and time constants (365/52/30/12/7) are
    universal — they appear in valid reasoning even when no tool emits them.
    Whitelisting them prevents false-positive rejections (Phase 4 followup)."""
    reasoning = (
        "AAPL at $312.06. Standard OCC multiplier is 100 shares/contract. "
        "Holding 30 days, well inside the 365-day window."
    )
    tools = [_result("get_quote", {"last": 312.06})]
    r = check_grounding(reasoning, tools)
    assert r.passed is True, f"unexpected unverified: {r.unverified_numbers}"


def test_whitelisted_constants_do_not_mask_real_hallucinations() -> None:
    """Whitelist must not also mask actually-hallucinated significant numbers."""
    reasoning = "AAPL at $312.06 had a fabricated 67.4% surprise (held 30 days)."
    tools = [_result("get_quote", {"last": 312.06})]
    r = check_grounding(reasoning, tools)
    assert r.passed is False
    assert 67.4 in r.unverified_numbers
    # 30 should NOT be flagged — it's whitelisted.
    assert 30 not in r.unverified_numbers
    assert 30.0 not in r.unverified_numbers


def test_failed_tool_results_do_not_provide_numbers() -> None:
    """If a tool failed, its (None) data should not be a grounding source."""
    failed = ToolResult(
        tool_name="get_quote",
        arguments={},
        success=False,
        error="boom",
        data=None,
        provider="test",
        fetched_at=datetime.now(UTC),
    )
    reasoning = "AAPL is at $312.06"
    r = check_grounding(reasoning, [failed])
    assert r.passed is False
    assert 312.06 in r.unverified_numbers
