"""Eval scoring verified with known-answer synthetic data.

These tests continue to verify Brier / hit-rate correctness through Phase 5
when the harness goes live against real theses.
"""

from __future__ import annotations

import math

import pytest

from app.eval.scoring import (
    ConfidenceHitPair,
    brier_score,
    calibration_buckets,
    hit_rate,
)

# === Brier ===


def test_brier_perfect_prediction_is_zero() -> None:
    pairs = [
        ConfidenceHitPair(confidence=1.0, hit=True),
        ConfidenceHitPair(confidence=0.0, hit=False),
    ]
    assert brier_score(pairs) == 0.0


def test_brier_worst_prediction_is_one() -> None:
    pairs = [
        ConfidenceHitPair(confidence=1.0, hit=False),
        ConfidenceHitPair(confidence=0.0, hit=True),
    ]
    assert brier_score(pairs) == 1.0


def test_brier_5050_baseline_is_quarter() -> None:
    """Always-50% predictions yield 0.25 regardless of outcomes."""
    pairs = [
        ConfidenceHitPair(confidence=0.5, hit=True),
        ConfidenceHitPair(confidence=0.5, hit=False),
        ConfidenceHitPair(confidence=0.5, hit=True),
    ]
    assert brier_score(pairs) == 0.25


def test_brier_empty_raises() -> None:
    with pytest.raises(ValueError):
        brier_score([])


def test_pair_rejects_out_of_range_confidence() -> None:
    with pytest.raises(ValueError):
        ConfidenceHitPair(confidence=1.1, hit=True)
    with pytest.raises(ValueError):
        ConfidenceHitPair(confidence=-0.01, hit=False)


# === Hit rate ===


def test_hit_rate_all_hits() -> None:
    pairs = [ConfidenceHitPair(confidence=0.6, hit=True) for _ in range(5)]
    assert hit_rate(pairs) == 1.0


def test_hit_rate_no_hits() -> None:
    pairs = [ConfidenceHitPair(confidence=0.6, hit=False) for _ in range(5)]
    assert hit_rate(pairs) == 0.0


def test_hit_rate_half_split() -> None:
    pairs = [
        ConfidenceHitPair(confidence=0.5, hit=True),
        ConfidenceHitPair(confidence=0.5, hit=False),
        ConfidenceHitPair(confidence=0.5, hit=True),
        ConfidenceHitPair(confidence=0.5, hit=False),
    ]
    assert hit_rate(pairs) == 0.5


def test_hit_rate_empty_raises() -> None:
    with pytest.raises(ValueError):
        hit_rate([])


# === Calibration ===


def test_calibration_well_calibrated_model() -> None:
    """A model whose stated confidence matches realized hit rate is well-calibrated."""
    # 10 theses at ~92% confidence, 9 hits
    pairs = [ConfidenceHitPair(confidence=0.92, hit=(i < 9)) for i in range(10)]
    # 10 theses at ~51% confidence, 5 hits
    pairs += [ConfidenceHitPair(confidence=0.51, hit=(i < 5)) for i in range(10)]

    buckets = calibration_buckets(pairs, n_buckets=10)
    by_lower = {b.lower: b for b in buckets}

    high = by_lower[0.9]
    assert math.isclose(high.mean_confidence, 0.92, abs_tol=0.01)
    assert math.isclose(high.realized_hit_rate, 0.9, abs_tol=0.01)

    mid = by_lower[0.5]
    assert math.isclose(mid.mean_confidence, 0.51, abs_tol=0.01)
    assert math.isclose(mid.realized_hit_rate, 0.5, abs_tol=0.01)


def test_calibration_empty_buckets_omitted() -> None:
    pairs = [ConfidenceHitPair(confidence=0.95, hit=True)]
    buckets = calibration_buckets(pairs, n_buckets=10)
    assert len(buckets) == 1
    assert buckets[0].lower == 0.9


def test_calibration_overconfident_model_shows_gap() -> None:
    """Model says 90% confident, only hits 50% — calibration shows the gap."""
    pairs = [ConfidenceHitPair(confidence=0.9, hit=(i < 5)) for i in range(10)]
    buckets = calibration_buckets(pairs, n_buckets=10)
    bucket = buckets[0]
    assert math.isclose(bucket.mean_confidence, 0.9)
    assert math.isclose(bucket.realized_hit_rate, 0.5)


def test_calibration_rejects_zero_buckets() -> None:
    with pytest.raises(ValueError):
        calibration_buckets([ConfidenceHitPair(confidence=0.5, hit=True)], n_buckets=0)
