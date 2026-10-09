"""Grounding check (DESIGN.md §4 step 5).

Given a thesis's reasoning text and the tool results that fed it, verify
that every non-trivial number in the reasoning traces (within tolerance) to
a value in the tool data. This catches LLM hallucination of specific figures
— the most damaging kind of model error.

The check is intentionally conservative: it doesn't try to identify
"this is a percent" vs "this is a price". It just extracts numeric tokens,
and checks each appears somewhere in the nested tool output — within a
tolerance derived from the precision the model wrote the number at
(rounding slack only), with percent-style relative tolerance reserved for
large aggregates like share volumes.

False positives are tolerable; false negatives are not.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from typing import Any

from app.agent.tools import ToolResult

_NUMBER_RE = re.compile(r"(?<![A-Za-z_\d])(-?\d+(?:\.\d+)?)")
# Date-like patterns (ISO + slashed) are stripped before number extraction so
# their month/day components don't get flagged as ungrounded.
_DATE_PATTERN_RE = re.compile(
    r"\b\d{4}-\d{1,2}-\d{1,2}\b"
    r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
    r"|\bQ[1-4]\s*20\d{2}\b"
)
# Universal arithmetic constants that show up in valid reasoning without
# corresponding tool data — the OCC contract multiplier (100 shares/contract)
# and common time conversions. Whitelisting these avoids false-positive
# grounding rejections (e.g., "$0.06 x 1 x 100 shares = $6 max risk").
# The exemption covers only the bare-integer arithmetic form: "$100" or
# "52.00" is a price claim and is verified like any other number (otherwise
# an invented $100 target or $30 strike would skip the check entirely).
_WHITELISTED_CONSTANTS: frozenset[float] = frozenset({100.0, 365.0, 52.0, 30.0, 12.0, 7.0})
_MONEY_RE = re.compile(r"\$\s?(\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class GroundingResult:
    passed: bool
    unverified_numbers: list[float]
    tools_used: list[str]
    extracted_count: int
    notes: str | None = None
    # Numbers actually checked (significant + non-year-like).
    significant_count: int = 0
    matched_count: int = 0
    available_sample: list[float] = field(default_factory=list)


def _extract_numbers(text: str) -> list[tuple[float, int]]:
    """Extract (value, decimals-as-written) pairs from reasoning text.

    The written precision drives the matching tolerance: "$312" may be a
    rounding of 312.06, but "$4.60" is a claim precise to the cent.
    """
    cleaned = _DATE_PATTERN_RE.sub(" ", text)
    out: list[tuple[float, int]] = []
    for m in _NUMBER_RE.finditer(cleaned):
        token = m.group(1)
        try:
            value = float(token)
        except ValueError:
            continue
        decimals = len(token.split(".")[1]) if "." in token else 0
        out.append((value, decimals))
    return out


def _collect_numbers(obj: Any) -> set[float]:
    """Recursively pull every numeric leaf from a JSON-ish nested structure."""
    out: set[float] = set()
    if isinstance(obj, bool):
        return out  # bools are not numbers for our purposes
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, str):
        # Decimal/datetime get serialized as strings; try to parse numeric ones.
        with contextlib.suppress(ValueError, TypeError):
            out.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            out |= _collect_numbers(v)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            out |= _collect_numbers(v)
    return out


def _is_year_like(n: float) -> bool:
    return 1900 <= n <= 2100 and n == int(n)


def _money_values(text: str) -> set[float]:
    """Values the reasoning writes as dollar amounts ("$312.06", "$ 30")."""
    out: set[float] = set()
    for m in _MONEY_RE.finditer(_DATE_PATTERN_RE.sub(" ", text)):
        with contextlib.suppress(ValueError):
            out.add(float(m.group(1)))
    return out


def _is_significant(
    n: float, min_significant: float, *, decimals: int = 0, as_money: bool = False
) -> bool:
    if _is_year_like(n):
        return False
    if abs(n) in _WHITELISTED_CONSTANTS and decimals == 0 and not as_money:
        return False
    return abs(n) > min_significant


# Relative tolerance applies ONLY to large aggregates (share volumes, market
# caps) where the model reasonably rounds ("~38 million shares"). Applied to
# price-scale numbers it let almost any invented figure "ground" against a
# dense options chain (every strike/bid/ask within 2% matched).
_LARGE_ABS = 10_000.0
_REL_TOL_LARGE = 0.02
# Floor keeps sub-cent citations from failing on float serialization noise.
_ABS_TOL_FLOOR = 0.005


def _close(cited: float, decimals: int, actual: float) -> bool:
    """Is `actual` a plausible source for the number as the model wrote it?

    Precision-aware: a cited value matches only within half a unit of its
    own last written digit — "312" stands for [311.5, 312.5], but "4.60"
    only for [4.595, 4.605]. The old flat ±0.5 let a fabricated $4.60
    premium "verify" against a real $4.10 quote.
    """
    if cited == actual:
        return True
    diff = abs(cited - actual)
    tol = max(0.5 * 10.0**-decimals, _ABS_TOL_FLOOR)
    if diff <= tol + 1e-9:
        return True
    if max(abs(cited), abs(actual)) >= _LARGE_ABS:
        return diff / max(abs(cited), abs(actual)) <= _REL_TOL_LARGE
    return False


def _matches_any(n: float, decimals: int, candidates: set[float]) -> bool:
    return any(_close(n, decimals, c) for c in candidates)


def check_grounding(
    reasoning: str,
    tool_results: list[ToolResult],
    *,
    min_significant: float = 2.0,
) -> GroundingResult:
    """Verify cited numbers in `reasoning` trace to values in tool output.

    A number is "significant" if abs > `min_significant` and not year-like.
    Significant numbers must match some value in successful tool results,
    within a tolerance set by the precision the model wrote them at (see
    `_close`). `passed` is True iff zero unverified.
    """
    extracted = _extract_numbers(reasoning)
    money = _money_values(reasoning)
    significant = [
        (n, d)
        for n, d in extracted
        if _is_significant(n, min_significant, decimals=d, as_money=n in money)
    ]

    available: set[float] = set()
    tools_used: list[str] = []
    for tr in tool_results:
        if tr.success and tr.data is not None:
            available |= _collect_numbers(tr.data)
            tools_used.append(tr.tool_name)

    unverified = [n for n, d in significant if not _matches_any(n, d, available)]
    matched = len(significant) - len(unverified)
    passed = len(unverified) == 0

    notes: str | None = None
    if not passed:
        sample = unverified[:5]
        notes = (
            f"Unverified numbers in reasoning: {sample}"
            + ("..." if len(unverified) > 5 else "")
            + f". Tools used: {sorted(set(tools_used))}"
        )

    return GroundingResult(
        passed=passed,
        unverified_numbers=unverified,
        tools_used=sorted(set(tools_used)),
        extracted_count=len(extracted),
        significant_count=len(significant),
        matched_count=matched,
        notes=notes,
        available_sample=sorted(available)[:20],
    )
