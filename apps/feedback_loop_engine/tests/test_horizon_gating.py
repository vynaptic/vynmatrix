"""Sub-hour evaluation horizon + per-strategy horizon gating (H5/M8/L4).

A 1-minute scalper could only be evaluated at >=1h (the smallest bucket was H1,
and M1="1m" means one MONTH), so its forward return was a different regime than
its trade — and it was scored at every horizon, inflating the consecutive-wrong
tracker. These tests lock in the new sub-hour MIN15 bucket and the holding-period
gating that keeps a scalp off the 1w/1m buckets.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from feedback_loop_engine.evaluator import eligible_horizons_for
from feedback_loop_engine.models import EvaluationHorizon


def test_min15_is_a_real_sub_hour_bucket() -> None:
    # Distinct, unambiguous key — M1="1m" is one MONTH, not one minute.
    assert EvaluationHorizon.MIN15.value == "15min"
    assert EvaluationHorizon.MIN15.duration == timedelta(minutes=15)
    assert EvaluationHorizon("15min").duration == timedelta(minutes=15)
    assert EvaluationHorizon("1m").duration == timedelta(days=30)  # one month


def test_unknown_horizon_fails_closed() -> None:
    # The old string map silently defaulted unknown horizons to 1 day; the
    # single enum source of truth must raise instead.
    with pytest.raises(ValueError, match="1min"):
        EvaluationHorizon("1min")


def test_eligible_horizons_none_is_ineligible() -> None:
    assert eligible_horizons_for(None) == set()


def test_eligible_horizons_intraday_excludes_long_buckets() -> None:
    scalp = eligible_horizons_for(60.0)  # 1-minute hold
    assert scalp == {EvaluationHorizon.MIN15, EvaluationHorizon.H1}
    # The distortion fix: a scalp is NOT scored at 1w / 1m.
    assert EvaluationHorizon.W1 not in scalp
    assert EvaluationHorizon.M1 not in scalp


def test_eligible_horizons_swing_and_position() -> None:
    assert eligible_horizons_for(3600.0) == {EvaluationHorizon.MIN15, EvaluationHorizon.H1}
    assert eligible_horizons_for(3600.0 + 1) == {EvaluationHorizon.H4, EvaluationHorizon.D1}
    assert eligible_horizons_for(86400.0) == {EvaluationHorizon.H4, EvaluationHorizon.D1}
    assert eligible_horizons_for(86400.0 + 1) == {
        EvaluationHorizon.W1,
        EvaluationHorizon.W2,
        EvaluationHorizon.M1,
    }


def test_signal_performance_constraint_allows_every_evaluation_horizon() -> None:
    """The gating can route a signal to ANY EvaluationHorizon (incl. MIN15='15min'),
    so the signal_performance.ck_signal_perf_horizon DB constraint must accept every
    enum value — else persist_evaluation raises a CheckViolation and the feedback
    engine goes unhealthy. This was latent until the scalpers declared a sub-hour
    horizon (M2) and the first '15min' row hit the (then-too-narrow) constraint."""
    from lib_application.db.models import SignalPerformance

    constraint = next(
        c
        for c in SignalPerformance.__table__.constraints
        if getattr(c, "name", None) == "ck_signal_perf_horizon"
    )
    sqltext = str(constraint.sqltext)
    for horizon in EvaluationHorizon:
        assert f"'{horizon.value}'" in sqltext, (
            f"{horizon.value} missing from ck_signal_perf_horizon"
        )
