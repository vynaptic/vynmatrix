"""Deterministic tests for validation metrics, uncertainty, power, and PBO."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from statistics import fmean

import pytest

from dev_cli.validation.backtest.metrics import compute_metrics
from dev_cli.validation.backtest.optimization import probability_of_backtest_overfitting
from dev_cli.validation.backtest.power import (
    PowerStudyConfig,
    minimum_duration_for_power,
    run_power_study,
)
from dev_cli.validation.backtest.statistics import (
    block_bootstrap_confidence_interval,
    sharpe_diagnostics,
)


def _daily_equity() -> list[tuple[datetime, float]]:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    return [
        (start + timedelta(days=day), equity)
        for day, equity in enumerate([100.0, 110.0, 105.0, 115.0])
    ]


def test_extended_metrics_use_registered_assumptions() -> None:
    metrics = compute_metrics(
        _daily_equity(),
        [10.0, -5.0, 10.0],
        initial_capital=100.0,
        annualization_factor=252.0,
        gross_trade_pnls=[11.0, -4.0, 11.0],
        turnover_notional=400.0,
        exposure_seconds=2 * 86_400.0,
        holding_periods_seconds=[86_400.0, 2 * 86_400.0, 3 * 86_400.0],
    )

    assert metrics.annualization_factor == 252.0
    assert metrics.annualized_volatility_pct > 0.0
    assert metrics.calmar_ratio > 0.0
    assert metrics.recovery_duration_days == 2.0
    assert metrics.expectancy == 5.0
    assert metrics.payoff_ratio == 2.0
    assert metrics.exposure_pct == pytest.approx(200.0 / 3.0)
    assert metrics.turnover_ratio > 3.0
    assert metrics.average_holding_period_days == 2.0
    assert metrics.median_holding_period_days == 2.0
    assert metrics.value_at_risk_95_pct > 0.0
    assert metrics.expected_shortfall_95_pct > 0.0
    assert metrics.break_even_cost_bps == pytest.approx(450.0)

    with pytest.raises(ValueError, match="gross_trade_pnls must align"):
        compute_metrics(
            _daily_equity(),
            [1.0],
            initial_capital=100.0,
            gross_trade_pnls=[],
        )


def test_block_bootstrap_and_sharpe_diagnostics_are_reproducible() -> None:
    returns = [0.01, -0.02, 0.015, 0.004, -0.006, 0.012, 0.003, -0.008] * 5
    first = block_bootstrap_confidence_interval(
        returns,
        fmean,
        block_size=4,
        samples=250,
        confidence=0.90,
        seed=77,
    )
    second = block_bootstrap_confidence_interval(
        returns,
        fmean,
        block_size=4,
        samples=250,
        confidence=0.90,
        seed=77,
    )
    assert first == second
    assert first.lower <= first.observed <= first.upper
    assert first.evidence_role == "retrospective_diagnostic"

    diagnostics = sharpe_diagnostics(
        returns,
        annualization_factor=365.0,
        effective_trials=14,
        trial_sharpe_std=0.35,
        trial_sharpe_std_source="registered_trial_ledger",
        selection_contaminated=True,
    )
    assert 0.0 <= diagnostics.probabilistic_sharpe_probability <= 1.0
    assert 0.0 <= diagnostics.deflated_sharpe_probability <= 1.0
    assert diagnostics.deflated_benchmark_sharpe > 0.0
    assert diagnostics.effective_observations <= len(returns)
    assert diagnostics.trial_sharpe_std_source == "registered_trial_ledger"
    assert diagnostics.selection_contaminated is True

    with pytest.raises(ValueError, match="positive cross-trial dispersion"):
        sharpe_diagnostics(
            returns,
            effective_trials=14,
            trial_sharpe_std=0.0,
            trial_sharpe_std_source="degenerate_registry",
        )


def test_power_study_is_seed_reproducible_and_not_promotion_evidence() -> None:
    config = PowerStudyConfig(
        cadence_per_year=10.0,
        duration_years=2.0,
        minimum_standardized_effect=0.30,
        effective_trials=14,
        independence_ratio=0.80,
        simulations=400,
        seed=123,
    )
    first = run_power_study(config)
    second = run_power_study(config)
    assert first == second
    assert first.config.evidence_role == "prospective_design"
    assert first.decision_count_lower <= first.expected_decisions <= first.decision_count_upper
    assert 0.0 <= first.estimated_power <= 1.0
    assert first.power_confidence_lower <= first.estimated_power
    assert first.estimated_power <= first.power_confidence_upper
    assert 0.0 <= first.estimated_familywise_type_i_error <= 1.0
    assert (
        first.familywise_type_i_error_confidence_lower
        <= first.estimated_familywise_type_i_error
        <= first.familywise_type_i_error_confidence_upper
    )
    assert first.adequately_powered is (
        first.power_confidence_lower >= config.target_power
        and first.familywise_type_i_error_confidence_upper <= config.familywise_alpha
    )


def test_power_duration_requires_stable_conservative_crossing() -> None:
    config = PowerStudyConfig(
        cadence_per_year=80.0,
        duration_years=0.25,
        minimum_standardized_effect=0.80,
        effective_trials=1,
        simulations=2_000,
        seed=91,
    )

    result = minimum_duration_for_power(
        config,
        duration_step_years=0.25,
        maximum_duration_years=2.0,
    )

    assert result.minimum_duration_years is not None
    first_passing_index = next(
        index for index, study in enumerate(result.evaluated) if study.adequately_powered
    )
    assert (
        result.minimum_duration_years == result.evaluated[first_passing_index].config.duration_years
    )
    assert all(study.config.seed == config.seed for study in result.evaluated)
    assert result.evaluated[-1].adequately_powered is True
    assert result.evaluated[-2].adequately_powered is True


def test_pbo_is_explicitly_retrospective() -> None:
    training = [
        {"baseline": 1.0, "neighbor": 0.5},
        {"baseline": 0.2, "neighbor": 0.8},
    ]
    testing = [
        {"baseline": -0.5, "neighbor": 0.4},
        {"baseline": 0.3, "neighbor": -0.2},
    ]
    diagnostic = probability_of_backtest_overfitting(
        training,
        testing,
        registered_candidate_order=("baseline", "neighbor"),
    )
    assert diagnostic.splits == 2
    assert diagnostic.probability_of_backtest_overfitting == 1.0
    assert diagnostic.registered_candidate_order == ("baseline", "neighbor")
    assert diagnostic.selected_candidates == ("baseline", "neighbor")
    assert diagnostic.evidence_role == "retrospective_diagnostic"


def test_pbo_uses_registered_order_for_training_ties() -> None:
    training = [
        {"alpha": 1.0, "zulu": 1.0, "control": 0.0},
        {"zulu": 1.0, "control": 0.0, "alpha": 1.0},
    ]
    testing = [
        {"control": 0.0, "alpha": 0.5, "zulu": 1.0},
        {"alpha": 0.5, "zulu": 1.0, "control": 0.0},
    ]

    diagnostic = probability_of_backtest_overfitting(
        training,
        testing,
        registered_candidate_order=("zulu", "alpha", "control"),
    )

    assert diagnostic.selected_candidates == ("zulu", "zulu")
    assert diagnostic.out_of_sample_rank_percentiles == (1.0, 1.0)


def test_pbo_assigns_average_out_of_sample_rank_to_ties() -> None:
    diagnostic = probability_of_backtest_overfitting(
        [{"selected": 2.0, "lower": 1.0, "equal": 0.0, "higher": 0.0}],
        [{"higher": 3.0, "selected": 2.0, "equal": 2.0, "lower": 1.0}],
        registered_candidate_order=("selected", "lower", "equal", "higher"),
    )

    # selected/equal occupy ranks two and three out of four, averaging to 2.5.
    assert diagnostic.out_of_sample_rank_percentiles == (0.625,)
    assert diagnostic.probability_of_backtest_overfitting == 0.0


@pytest.mark.parametrize(
    ("training", "testing", "error"),
    [
        (
            [{"baseline": 1.0, "neighbor": 0.0}, {"baseline": 1.0, "other": 0.0}],
            [{"baseline": 0.0, "neighbor": 1.0}, {"baseline": 0.0, "other": 1.0}],
            "registered_candidate_order must match every split's candidate set",
        ),
        (
            [{"baseline": 1.0, "neighbor": 0.0}],
            [{"baseline": 0.0, "other": 1.0}],
            "every split must contain the same non-empty candidate set",
        ),
    ],
)
def test_pbo_rejects_inconsistent_candidate_sets(
    training: list[dict[str, float]],
    testing: list[dict[str, float]],
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        probability_of_backtest_overfitting(
            training,
            testing,
            registered_candidate_order=("baseline", "neighbor"),
        )


def test_pbo_rejects_duplicate_registered_candidates() -> None:
    with pytest.raises(ValueError, match="must contain unique candidates"):
        probability_of_backtest_overfitting(
            [{"baseline": 1.0, "neighbor": 0.0}],
            [{"baseline": 0.0, "neighbor": 1.0}],
            registered_candidate_order=("baseline", "neighbor", "baseline"),
        )
