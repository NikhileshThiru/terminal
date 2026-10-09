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
    # We should pick up 312.06, 10.86, 1.5 at minimum — with written precision.
    assert (312.06, 2) in out
    assert (10.86, 2) in out
    assert (1.5, 1) in out


def test_extract_numbers_skips_dollar_prefix_attached_to_letter_or_id() -> None:
    text = "Ticker$ABC and account_id_3 should be ignored, but 42 counts."
    out = _extract_numbers(text)
    values = [v for v, _ in out]
    assert 42 in values
    # The regex's lookbehind blocks numbers attached to letters/underscores.
    assert 3 not in values  # part of account_id_3


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
    assert _close(5.0, 0, 5.0)


def test_close_tolerance_scales_with_written_precision() -> None:
    # "$5" (0 decimals) can be a rounding of 5.3; "$5.0" cannot.
    assert _close(5.0, 0, 5.3)
    assert not _close(5.0, 1, 5.3)
    # "$4.60" is a claim precise to the cent — 4.10 is NOT its source.
    # (The old flat ±0.5 tolerance matched this: an invented premium passed.)
    assert not _close(4.60, 2, 4.10)
    assert _close(4.60, 2, 4.60)


def test_close_relative_tolerance_only_for_large_aggregates() -> None:
    # Share volumes / market caps: rounding to millions is legitimate.
    assert _close(38_000_000.0, 0, 38_392_499.0)  # ~1% diff, large scale
    # Price-scale numbers get NO percent slack — 2% of a stock price was
    # enough to "ground" invented figures against a dense options chain.
    assert not _close(100.0, 0, 102.0)  # 2% diff at price scale
    assert not _close(152.0, 1, 155.0)


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


def test_invented_premium_fails_against_dense_chain_numbers() -> None:
    """Regression: with the old flat ±0.5 tolerance, a fabricated $4.60
    premium "verified" against a chain quoting 4.10/4.30. Precision-aware
    matching rejects it — the model wrote cents, so it must match to cents."""
    reasoning = "The 315 call premium is $4.60, a bargain."
    tools = [
        _result(
            "get_options_chain",
            {"contracts": [{"strike": 315, "bid": 4.10, "ask": 4.30}]},
        )
    ]
    r = check_grounding(reasoning, tools)
    assert r.passed is False
    assert 4.6 in r.unverified_numbers


def test_whitelisted_constant_written_as_price_is_still_verified() -> None:
    """Regression: 100/30/52/12/7 were exempt everywhere, so an invented
    "$100 target" or "$30 strike" skipped grounding entirely. Only the bare
    arithmetic form ("x 100 shares", "30 days") is exempt now."""
    from datetime import UTC, datetime

    from app.agent.grounding import check_grounding
    from app.agent.tools import ToolResult

    data = [
        ToolResult(
            tool_name="get_quote",
            arguments={"symbol": "AAPL"},
            success=True,
            data={"last": 312.06},
            provider="test",
            fetched_at=datetime.now(UTC),
        )
    ]
    arithmetic = check_grounding("AAPL at $312.06; 1 contract x 100 shares over 30 days.", data)
    assert arithmetic.passed

    invented_target = check_grounding("AAPL at $312.06 with a $100 downside target.", data)
    assert not invented_target.passed
    assert 100.0 in invented_target.unverified_numbers

    invented_decimal = check_grounding("AAPL at $312.06; IV rank 52.00.", data)
    assert not invented_decimal.passed
