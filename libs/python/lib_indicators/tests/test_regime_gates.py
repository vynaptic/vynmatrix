"""Regime-gate primitive tests (§4.1/§4.3 machinery)."""

from __future__ import annotations

import pytest

from lib_indicators import ExpandingPercentileRank, RollingZScore, TrendConfirmGate


def test_trend_gate_starts_off_and_needs_confirmation() -> None:
    gate = TrendConfirmGate(3)
    assert gate.is_on is False
    assert gate.update(True) is False
    assert gate.update(True) is False
    assert gate.update(True) is True  # third consecutive True flips ON
    # A single False does not flip it back...
    assert gate.update(False) is True
    assert gate.update(False) is True
    # ...and an interruption resets the streak.
    assert gate.update(True) is True
    assert gate.update(False) is True
    assert gate.update(False) is True
    assert gate.update(False) is False  # three consecutive False flips OFF


def test_trend_gate_rejects_bad_confirm_days() -> None:
    with pytest.raises(ValueError, match="confirm_days"):
        TrendConfirmGate(0)


def test_rolling_zscore_uses_prior_window_only() -> None:
    z = RollingZScore(4)
    for value in (1.0, 2.0, 3.0, 4.0):
        assert z.update(value) is None  # window not yet full
    # Prior window [1,2,3,4]: mean 2.5, population std sqrt(1.25).
    result = z.update(5.0)
    assert result == pytest.approx((5.0 - 2.5) / 1.25**0.5)


def test_rolling_zscore_degenerate_baseline_returns_none() -> None:
    z = RollingZScore(3)
    for _ in range(3):
        z.update(2.0)
    assert z.update(10.0) is None  # flat baseline cannot classify a move


def test_expanding_percentile_rank_prior_history_only() -> None:
    gate = ExpandingPercentileRank(min_samples=3)
    assert gate.update(10.0) is None
    assert gate.update(20.0) is None
    assert gate.update(30.0) is None
    assert gate.update(25.0) == pytest.approx(2 / 3)  # above 2 of 3 priors
    assert gate.update(40.0) == pytest.approx(1.0)
    assert gate.update(5.0) == pytest.approx(0.0)
