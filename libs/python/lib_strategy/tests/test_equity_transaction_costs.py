"""Deterministic tests for the reconstructable daily-bar cost estimator."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import pytest

from lib_strategy.equity_transaction_costs import (
    DailyBarCostInputError,
    DailyBarCostModelPolicy,
    DailyBarCostObservation,
    estimate_daily_bar_transaction_costs,
)


def _bar(
    session: date,
    *,
    high: float,
    low: float,
    close: float,
    identity: str,
    volume: float = 50_000_000.0,
    split_factor: float = 1.0,
) -> DailyBarCostObservation:
    observed = datetime.combine(session, datetime.min.time(), tzinfo=UTC) + timedelta(hours=21)
    return DailyBarCostObservation(
        security_id="US0378331005",
        symbol="AAPL",
        session_date=session,
        observed_at=observed,
        available_at=observed,
        raw_high=high,
        raw_low=low,
        raw_close=close,
        split_adjusted_high=high * split_factor,
        split_adjusted_low=low * split_factor,
        split_adjusted_close=close * split_factor,
        split_adjusted_volume=volume,
        split_adjustment_factor=split_factor,
        source_observation_id=identity * 64,
        source_content_sha256=identity * 64,
    )


def test_daily_bar_cost_estimate_is_exact_and_explicitly_modeled() -> None:
    policy = DailyBarCostModelPolicy()
    previous = _bar(
        date(2024, 6, 27),
        high=102.0,
        low=98.0,
        close=100.0,
        identity="1",
    )
    current = _bar(
        date(2024, 6, 28),
        high=103.0,
        low=99.0,
        close=101.0,
        identity="2",
    )

    first = estimate_daily_bar_transaction_costs(
        (previous, current), policy, cost_context_sha256="a" * 64
    )[0]
    second = estimate_daily_bar_transaction_costs(
        (previous, current), policy, cost_context_sha256="a" * 64
    )[0]

    current_range = math.log(103.0 / 99.0)
    expected_volatility = current_range / math.sqrt(4.0 * math.log(2.0))
    expected_participation = 50_000.0 / (101.0 * (50_000_000.0 - 1.0))
    expected_nonspread = 2.5 + expected_volatility * math.sqrt(expected_participation) * 10_000.0
    assert first == second
    assert first.daily_range_volatility == pytest.approx(expected_volatility)
    assert first.estimated_one_way_nonspread_cost_bps == pytest.approx(expected_nonspread)
    assert first.estimated_round_trip_spread_bps >= 0.0
    assert first.cost_context_sha256 == "a" * 64
    assert first.to_payload()["evidence_kind"] == "modeled_from_observed_daily_bars"
    assert "quote" not in first.to_payload()


def test_daily_bar_cost_estimate_is_invariant_to_split_scaling() -> None:
    policy = DailyBarCostModelPolicy()
    original = (
        _bar(date(2024, 6, 27), high=102.0, low=98.0, close=100.0, identity="1"),
        _bar(date(2024, 6, 28), high=103.0, low=99.0, close=101.0, identity="2"),
    )
    scaled = (
        _bar(
            date(2024, 6, 27),
            high=51.0,
            low=49.0,
            close=50.0,
            identity="3",
            volume=100_000_000.0,
        ),
        _bar(
            date(2024, 6, 28),
            high=51.5,
            low=49.5,
            close=50.5,
            identity="4",
            volume=100_000_000.0,
        ),
    )

    original_estimate = estimate_daily_bar_transaction_costs(
        original, policy, cost_context_sha256="a" * 64
    )[0]
    scaled_estimate = estimate_daily_bar_transaction_costs(
        scaled, policy, cost_context_sha256="a" * 64
    )[0]

    assert scaled_estimate.estimated_round_trip_spread_bps == pytest.approx(
        original_estimate.estimated_round_trip_spread_bps
    )
    assert scaled_estimate.estimated_one_way_nonspread_cost_bps == pytest.approx(
        original_estimate.estimated_one_way_nonspread_cost_bps,
        rel=1e-7,
    )


def test_daily_bar_cost_estimate_uses_one_split_coordinate_across_split_session() -> None:
    policy = DailyBarCostModelPolicy()
    continuous = (
        _bar(
            date(2024, 6, 27),
            high=25.5,
            low=24.5,
            close=25.0,
            identity="1",
            volume=200_000_000.0,
        ),
        _bar(
            date(2024, 6, 28),
            high=25.75,
            low=24.75,
            close=25.25,
            identity="2",
            volume=200_000_000.0,
        ),
    )
    split_crossing = (
        _bar(
            date(2024, 6, 27),
            high=102.0,
            low=98.0,
            close=100.0,
            identity="3",
            volume=200_000_000.0,
            split_factor=0.25,
        ),
        _bar(
            date(2024, 6, 28),
            high=25.75,
            low=24.75,
            close=25.25,
            identity="4",
            volume=200_000_000.0,
        ),
    )

    continuous_estimate = estimate_daily_bar_transaction_costs(
        continuous, policy, cost_context_sha256="a" * 64
    )[0]
    split_crossing_estimate = estimate_daily_bar_transaction_costs(
        split_crossing, policy, cost_context_sha256="a" * 64
    )[0]

    assert split_crossing_estimate.estimated_round_trip_spread_bps == pytest.approx(
        continuous_estimate.estimated_round_trip_spread_bps
    )
    assert split_crossing_estimate.estimated_one_way_nonspread_cost_bps == pytest.approx(
        continuous_estimate.estimated_one_way_nonspread_cost_bps
    )


def test_daily_bar_cost_estimator_rejects_reordered_or_mixed_security_inputs() -> None:
    policy = DailyBarCostModelPolicy()
    previous = _bar(date(2024, 6, 27), high=102.0, low=98.0, close=100.0, identity="1")
    current = _bar(date(2024, 6, 28), high=103.0, low=99.0, close=101.0, identity="2")

    with pytest.raises(DailyBarCostInputError, match="strictly session ordered"):
        estimate_daily_bar_transaction_costs(
            (current, previous), policy, cost_context_sha256="a" * 64
        )

    mixed = DailyBarCostObservation(
        security_id="US5949181045",
        symbol="MSFT",
        session_date=current.session_date,
        observed_at=current.observed_at,
        available_at=current.available_at,
        raw_high=current.raw_high,
        raw_low=current.raw_low,
        raw_close=current.raw_close,
        split_adjusted_high=current.split_adjusted_high,
        split_adjusted_low=current.split_adjusted_low,
        split_adjusted_close=current.split_adjusted_close,
        split_adjusted_volume=current.split_adjusted_volume,
        split_adjustment_factor=current.split_adjustment_factor,
        source_observation_id="3" * 64,
        source_content_sha256="3" * 64,
    )
    with pytest.raises(DailyBarCostInputError, match="one security"):
        estimate_daily_bar_transaction_costs(
            (previous, mixed), policy, cost_context_sha256="a" * 64
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"reference_order_notional_usd": 0.0}, "greater than"),
        ({"impact_coefficient": 0.0}, "greater than"),
        ({"fixed_one_way_nonspread_bps": -1.0}, "at least"),
    ],
)
def test_daily_bar_cost_policy_rejects_invalid_assumptions(
    overrides: dict[str, float],
    message: str,
) -> None:
    with pytest.raises(DailyBarCostInputError, match=message):
        DailyBarCostModelPolicy(**overrides)
