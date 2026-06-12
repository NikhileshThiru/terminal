"""Grounding check (DESIGN.md §4 step 5).

Given a thesis's reasoning text and the tool results that fed it, verify
that every non-trivial number in the reasoning traces (within tolerance) to
a value in the tool data. This catches LLM hallucination of specific figures
— the most damaging kind of model error.

The check is intentionally conservative: it doesn't try to identify
"this is a percent" vs "this is a price". It just extracts numeric tokens,
and checks each appears (within rel + abs tolerance) somewhere in the
nested tool output.

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
_WHITELISTED_CONSTANTS: frozenset[float] = frozenset({100.0, 365.0, 52.0, 30.0, 12.0, 7.0})


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


def _extract_numbers(text: str) -> list[float]:
    cleaned = _DATE_PATTERN_RE.sub(" ", text)
    out: list[float] = []
    for m in _NUMBER_RE.finditer(cleaned):
        try:
            out.append(float(m.group(1)))
        except ValueError:
            continue
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


def _is_significant(n: float, min_significant: float) -> bool:
    if _is_year_like(n):
        return False
    if abs(n) in _WHITELISTED_CONSTANTS:
        return False
    return abs(n) > min_significant


def _close(
    a: float,
    b: float,
    *,
    rel_tol: float = 0.02,
    abs_tol: float = 0.5,
) -> bool:
    if a == b:
        return True
    diff = abs(a - b)
    if diff <= abs_tol:
        return True
    if b != 0 and diff / abs(b) <= rel_tol:
        return True
    return bool(a != 0 and diff / abs(a) <= rel_tol)


def _matches_any(n: float, candidates: set[float]) -> bool:
    return any(_close(n, c) for c in candidates)


def check_grounding(
    reasoning: str,
    tool_results: list[ToolResult],
    *,
    min_significant: float = 2.0,
) -> GroundingResult:
    """Verify cited numbers in `reasoning` trace to values in tool output.

    A number is "significant" if abs > `min_significant` and not year-like.
    Significant numbers must match (within 2% rel or 0.5 abs) some value in
    successful tool results. `passed` is True iff zero unverified.
    """
    extracted = _extract_numbers(reasoning)
    significant = [n for n in extracted if _is_significant(n, min_significant)]

    available: set[float] = set()
    tools_used: list[str] = []
    for tr in tool_results:
        if tr.success and tr.data is not None:
            available |= _collect_numbers(tr.data)
            tools_used.append(tr.tool_name)

    unverified = [n for n in significant if not _matches_any(n, available)]
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
