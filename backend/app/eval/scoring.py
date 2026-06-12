"""Forward-test scoring functions (DESIGN.md §8).

Pure functions on lists of (confidence, hit) pairs. Trivial to test with
synthetic data — same code runs against the live DB in Phase 5.

- Brier score: mean squared error between confidence and realized outcome.
  Lower is better. 0.0 = perfect calibration; 0.25 = always-50% baseline.
- Hit rate: fraction of theses where predicted direction was realized.
- Calibration: per-bucket mean confidence vs realized hit rate. The
  calibration plot is the project's most resume-impressive artifact.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ConfidenceHitPair:
    """One thesis's stated confidence and realized outcome."""

    confidence: float  # in [0, 1]
    hit: bool

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")


def brier_score(pairs: Iterable[ConfidenceHitPair]) -> float:
    """Mean squared error between stated confidence and realized outcome.

    Lower is better. 0.0 = perfect, 0.25 = always-50% baseline.
    Above 0.25 means the model's confidence is anti-informative.
    """
    items = list(pairs)
    if not items:
        raise ValueError("brier_score requires at least one pair")
    total = sum((p.confidence - (1.0 if p.hit else 0.0)) ** 2 for p in items)
    return total / len(items)


def hit_rate(pairs: Iterable[ConfidenceHitPair]) -> float:
    """Fraction of theses where the predicted direction was realized."""
    items = list(pairs)
    if not items:
        raise ValueError("hit_rate requires at least one pair")
    hits = sum(1 for p in items if p.hit)
    return hits / len(items)


@dataclass(frozen=True)
class CalibrationBucket:
    """A bin of theses with similar stated confidence."""

    lower: float
    upper: float
    count: int
    mean_confidence: float
    realized_hit_rate: float


def calibration_buckets(
    pairs: Iterable[ConfidenceHitPair],
    n_buckets: int = 10,
) -> list[CalibrationBucket]:
    """Bin theses by confidence; per bin compute mean confidence vs realized hit rate.

    A perfectly calibrated model has mean_confidence == realized_hit_rate in every
    non-empty bucket (the calibration plot is a 45° line).
    """
    if n_buckets <= 0:
        raise ValueError("n_buckets must be positive")

    items = list(pairs)
    width = 1.0 / n_buckets
    bins: list[list[ConfidenceHitPair]] = [[] for _ in range(n_buckets)]

    for p in items:
        idx = min(int(p.confidence / width), n_buckets - 1)
        bins[idx].append(p)

    out: list[CalibrationBucket] = []
    for i, bin_pairs in enumerate(bins):
        if not bin_pairs:
            continue
        mean_conf = sum(x.confidence for x in bin_pairs) / len(bin_pairs)
        hit_frac = sum(1 for x in bin_pairs if x.hit) / len(bin_pairs)
        out.append(
            CalibrationBucket(
                lower=i * width,
                upper=(i + 1) * width,
                count=len(bin_pairs),
                mean_confidence=mean_conf,
                realized_hit_rate=hit_frac,
            )
        )
    return out
