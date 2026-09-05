"""Regression tests for immutable validation diagnostic primitives."""

from __future__ import annotations

import itertools
import json
import math
import statistics
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import pytest

from dev_cli.validation.contribution_diagnostics import (
    AttributedContribution,
    TradeContribution,
    summarize_contribution_robustness,
)
from dev_cli.validation.cscv import (
    CandidateDailyReturnSeries,
    TimestampedDailyReturn,
    build_exact_calendar_cscv_pbo,
)
from dev_cli.validation.economic_regimes import (
    EconomicRegimeCalibrationConfig,
    EconomicRegimeObservation,
    RegimeDecisionClose,
    RegimeDecisionCloseSeries,
    RegimeFoldIndexRegistry,
    RegimeFoldIndices,
    build_no_lookahead_economic_regimes,
)
from dev_cli.validation.fixed_robustness import build_fixed_baseline_robustness_evidence
from dev_cli.validation.performance_diagnostics import (
    CandidateOOSPerformance,
    NamedReturn,
    PerformancePoint,
    compare_raw_registered_benchmarks,
    summarize_cost_tolerance,
    summarize_fold_asset_positivity,
    summarize_neighbor_support,
)
from dev_cli.validation.persistence.backtest_manifest_store import canonical_manifest_bytes
from dev_cli.validation.prospective_power import (
    ClosedTradeOutcome,
    observe_prospective_design_inputs,
    search_prospective_duration_from_observed_trades,
)
from dev_cli.validation.redesign_diagnostics import (
    build_registered_redesign_candidate_evidence,
    build_registered_report_cscv_pbo,
)
from dev_cli.validation.report_evidence import (
    PersistedTradeContribution,
    StandardReportEvidence,
    standard_report_evidence_from_mapping,
)
from dev_cli.validation.robustness_models import (
    AdaptiveFoldSelection,
    AdaptiveSelectorEvidence,
    RegisteredBenchmarkEvidence,
    RegisteredOneFactorChange,
    RobustnessEvidenceConfig,
    TrialSharpeDispersion,
)

_REGISTERED_BENCHMARK_IDS = (
    "cash",
    "buy_and_hold",
    "training_calibrated_volatility_targeted_buy_and_hold",
    "sma_30_long_cash",
    "momentum_90d_long_cash",
)
_REGISTERED_REDESIGN_CHANGES = (
    ("NEIGHBOR_WEAK", RegisteredOneFactorChange("lookback_period", 30, 20)),
    ("NEIGHBOR_STRONG", RegisteredOneFactorChange("lookback_period", 30, 40)),
)


def _calendar_days(start_year: int, end_year_exclusive: int) -> tuple[date, ...]:
    start = date(start_year, 1, 1)
    end = date(end_year_exclusive, 1, 1)
    return tuple(start + timedelta(days=index) for index in range((end - start).days))


def _regime_config(
    *,
    assets: tuple[str, ...] = ("ALPHA", "BETA"),
    folds: tuple[str, ...] = ("fold-a",),
) -> EconomicRegimeCalibrationConfig:
    return EconomicRegimeCalibrationConfig(
        calibration_id="generic-prior-close-regimes-v1",
        asset_order=assets,
        fold_order=folds,
        volatility_lookback_bars=30,
        trend_lookback_bars=90,
        annualization_factor=365.0,
        economic_period_date_rule="normalized_close_timestamp_minus_one_calendar_day",
    )


def _regime_series(
    asset: str,
    *,
    closes: tuple[float, ...] | None = None,
    source_digest_character: str = "a",
) -> RegimeDecisionCloseSeries:
    if closes is None:
        generated = [100.0]
        scale = 1.0 if asset == "ALPHA" else 1.4
        for index in range(1, 120):
            simple_return = scale * (((index % 9) - 4) * 0.0007)
            generated.append(generated[-1] * (1.0 + simple_return))
        closes = tuple(generated)
    start = datetime(2020, 1, 2, tzinfo=UTC)
    return RegimeDecisionCloseSeries(
        asset=asset,
        source_id=f"close-source:{asset}",
        source_sha256=source_digest_character * 64,
        observations=tuple(
            RegimeDecisionClose(index, start + timedelta(days=index), close)
            for index, close in enumerate(closes)
        ),
    )


def _regime_fold_registry(
    *,
    training_indices: tuple[int, ...] = tuple(range(31, 100)),
    test_indices: tuple[int, ...] = tuple(range(100, 110)),
) -> RegimeFoldIndexRegistry:
    return RegimeFoldIndexRegistry(
        source_id="purged-embargoed-fold-registry-v1",
        source_sha256="f" * 64,
        folds=(RegimeFoldIndices("fold-a", training_indices, test_indices),),
    )


def test_no_lookahead_regime_builder_calibrates_exact_training_quartiles() -> None:
    config = _regime_config()
    series = (_regime_series("ALPHA"), _regime_series("BETA", source_digest_character="b"))
    registry = _regime_fold_registry()

    result = build_no_lookahead_economic_regimes(
        series,
        config=config,
        fold_registry=registry,
    )

    assert result.provenance.config == config
    assert result.provenance.feature_lag_policy_id == "strictly_prior_decision_closes"
    assert result.provenance.quantile_method_id == "nearest_rank_ceiling"
    assert result.provenance.volatility_boundary_policy_id.endswith("q4_gt_p75")
    assert len(result.provenance.fold_index_ledger_sha256) == 64
    assert tuple(item.asset for item in result.thresholds) == config.asset_order
    assert len(result.observations) == len(config.asset_order) * 10
    assert tuple(
        (item.fold_id, item.asset, item.decision_index) for item in result.observations
    ) == tuple(
        ("fold-a", asset, index) for asset in config.asset_order for index in range(100, 110)
    )
    assert all(item.unclassified_reason is None for item in result.observations)
    assert all(item.decision_close_ts is not None for item in result.observations)

    alpha_thresholds = result.thresholds[0]
    assert alpha_thresholds.registered_training_indices == tuple(range(31, 100))
    assert alpha_thresholds.volatility_feature_training_indices == tuple(range(31, 100))
    assert alpha_thresholds.registered_test_indices == tuple(range(100, 110))
    assert alpha_thresholds.calibration_test_observations == 0
    assert alpha_thresholds.volatility_training_observations == 69
    closes = tuple(item.close for item in series[0].observations)
    close_returns = (
        None,
        *(current / previous - 1.0 for previous, current in itertools.pairwise(closes)),
    )
    training_volatility = tuple(
        statistics.stdev(float(value) for value in close_returns[index - 30 : index])
        * math.sqrt(365.0)
        for index in range(31, 100)
    )
    ordered = sorted(training_volatility)
    assert alpha_thresholds.p25 == ordered[math.ceil(0.25 * len(ordered)) - 1]
    assert alpha_thresholds.p50 == ordered[math.ceil(0.50 * len(ordered)) - 1]
    assert alpha_thresholds.p75 == ordered[math.ceil(0.75 * len(ordered)) - 1]
    json.dumps(result.to_dict(), allow_nan=False)
    canonical_manifest_bytes({"payload": result.to_dict()})


def test_regime_features_are_prior_only_and_boundaries_are_deterministic() -> None:
    config = _regime_config(assets=("ALPHA",))
    closes = tuple(100.0 if index % 2 == 0 else 101.0 for index in range(120))
    original = _regime_series("ALPHA", closes=closes)
    changed_closes = list(closes)
    changed_closes[100] = 500.0
    changed = _regime_series("ALPHA", closes=tuple(changed_closes), source_digest_character="c")
    registry = _regime_fold_registry()

    original_result = build_no_lookahead_economic_regimes(
        (original,),
        config=config,
        fold_registry=registry,
    )
    changed_result = build_no_lookahead_economic_regimes(
        (changed,),
        config=config,
        fold_registry=registry,
    )

    original_threshold = original_result.thresholds[0]
    changed_threshold = changed_result.thresholds[0]
    assert original_threshold.p25 == original_threshold.p50 == original_threshold.p75
    assert original_threshold == changed_threshold
    assert original_result.observations[0].volatility_bucket == "q1"
    assert original_result.observations[0].trend_bucket == "nonpositive"
    assert original_result.observations[0].to_dict() == changed_result.observations[0].to_dict()
    assert (
        original_result.provenance.close_sources[0].decision_close_ledger_sha256
        != changed_result.provenance.close_sources[0].decision_close_ledger_sha256
    )


def test_regime_builder_unclassifies_only_missing_registered_trend_lag() -> None:
    config = _regime_config(assets=("ALPHA",))
    result = build_no_lookahead_economic_regimes(
        (_regime_series("ALPHA"),),
        config=config,
        fold_registry=_regime_fold_registry(
            training_indices=tuple(range(31, 39)),
            test_indices=tuple(range(39, 42)),
        ),
    )

    assert all(item.volatility_bucket != "unclassified" for item in result.observations)
    assert all(item.trend_bucket == "unclassified" for item in result.observations)
    assert all(
        item.unclassified_reason == "registered_lag_history_unavailable"
        for item in result.observations
    )


def test_regime_builder_accepts_aligned_sparse_daily_bar_grid() -> None:
    config = _regime_config()
    dense = (
        _regime_series("ALPHA"),
        _regime_series("BETA", source_digest_character="b"),
    )
    excluded_index = 104
    sparse = tuple(
        replace(
            series,
            observations=tuple(
                RegimeDecisionClose(index, item.timestamp, item.close)
                for index, item in enumerate(
                    item
                    for original_index, item in enumerate(series.observations)
                    if original_index != excluded_index
                )
            ),
        )
        for series in dense
    )

    sparse_result = build_no_lookahead_economic_regimes(
        sparse,
        config=config,
        fold_registry=_regime_fold_registry(),
    )
    dense_result = build_no_lookahead_economic_regimes(
        dense,
        config=config,
        fold_registry=_regime_fold_registry(),
    )

    excluded_date = (dense[0].observations[excluded_index].timestamp - timedelta(days=1)).date()
    assert excluded_date not in {
        observation.economic_period_date for observation in sparse_result.observations
    }
    assert (
        sparse_result.provenance.close_sources[0].decision_close_ledger_sha256
        != dense_result.provenance.close_sources[0].decision_close_ledger_sha256
    )


def test_regime_builder_rejects_invalid_sources_indices_and_grids() -> None:
    start = datetime(2020, 1, 2, tzinfo=UTC)
    with pytest.raises(ValueError, match="UTC timezone-aware"):
        RegimeDecisionClose(0, datetime(2020, 1, 2), 100.0)
    with pytest.raises(ValueError, match="must be positive"):
        RegimeDecisionClose(0, start, 0.0)
    with pytest.raises(ValueError, match="zero through n-1"):
        RegimeDecisionCloseSeries(
            asset="ALPHA",
            source_id="source",
            source_sha256="a" * 64,
            observations=(
                RegimeDecisionClose(0, start, 100.0),
                RegimeDecisionClose(2, start + timedelta(days=1), 101.0),
            ),
        )
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        RegimeDecisionCloseSeries(
            asset="ALPHA",
            source_id="source",
            source_sha256="not-a-digest",
            observations=(RegimeDecisionClose(0, start, 100.0),),
        )
    with pytest.raises(ValueError, match="one observation per UTC date"):
        RegimeDecisionCloseSeries(
            asset="ALPHA",
            source_id="source",
            source_sha256="a" * 64,
            observations=(
                RegimeDecisionClose(0, start, 100.0),
                RegimeDecisionClose(1, start + timedelta(hours=1), 101.0),
            ),
        )
    with pytest.raises(ValueError, match="share one UTC daily close time"):
        RegimeDecisionCloseSeries(
            asset="ALPHA",
            source_id="source",
            source_sha256="a" * 64,
            observations=(
                RegimeDecisionClose(0, start, 100.0),
                RegimeDecisionClose(1, start + timedelta(days=1, hours=1), 101.0),
            ),
        )
    with pytest.raises(ValueError, match="fold-index source_sha256"):
        RegimeFoldIndexRegistry(
            source_id="folds",
            source_sha256="bad",
            folds=(RegimeFoldIndices("fold-a", (0, 1), (2,)),),
        )
    with pytest.raises(ValueError, match="exclude every test index"):
        RegimeFoldIndices("fold-a", (0, 1, 2), (2, 3))
    with pytest.raises(ValueError, match="complete contiguous grid"):
        RegimeFoldIndices("fold-a", (0, 1), (3, 5))

    config = _regime_config()
    alpha = _regime_series("ALPHA")
    beta = _regime_series("BETA", source_digest_character="b")
    shifted_beta = replace(
        beta,
        observations=tuple(
            replace(item, timestamp=item.timestamp + timedelta(hours=1))
            for item in beta.observations
        ),
    )
    with pytest.raises(ValueError, match="exact decision-index timestamp grid"):
        build_no_lookahead_economic_regimes(
            (alpha, shifted_beta),
            config=config,
            fold_registry=_regime_fold_registry(),
        )
    sparse_beta = replace(
        beta,
        observations=tuple(
            RegimeDecisionClose(index, item.timestamp, item.close)
            for index, item in enumerate(
                item
                for original_index, item in enumerate(beta.observations)
                if original_index != 50
            )
        ),
    )
    with pytest.raises(ValueError, match="exact decision-index timestamp grid"):
        build_no_lookahead_economic_regimes(
            (alpha, sparse_beta),
            config=config,
            fold_registry=_regime_fold_registry(),
        )
    with pytest.raises(ValueError, match="no eligible lagged volatility"):
        build_no_lookahead_economic_regimes(
            (alpha, beta),
            config=config,
            fold_registry=_regime_fold_registry(
                training_indices=tuple(range(31)),
                test_indices=(31,),
            ),
        )


def _daily_series(candidate_id: str, *, multiplier: float) -> CandidateDailyReturnSeries:
    observations = tuple(
        TimestampedDailyReturn(
            datetime.combine(day, time.min, tzinfo=UTC),
            multiplier * (0.001 if day.toordinal() % 3 else -0.0004),
        )
        for day in _calendar_days(2020, 2026)
    )
    return CandidateDailyReturnSeries(candidate_id, observations)


def test_exact_calendar_cscv_builds_all_twenty_splits_and_uses_registered_ties() -> None:
    result = build_exact_calendar_cscv_pbo(
        (
            _daily_series("zulu", multiplier=1.0),
            _daily_series("alpha", multiplier=1.0),
            _daily_series("control", multiplier=-1.0),
        ),
        registered_candidate_order=("zulu", "alpha", "control"),
        calendar_years=(2020, 2021, 2022, 2023, 2024, 2025),
        annualization_factor=365.0,
        registered_calendar_dates=_calendar_days(2020, 2026),
    )

    assert result.aligned_calendar_days == 2_192
    assert len(result.splits) == 20
    assert result.splits[0].test_years == (2020, 2021, 2022)
    assert result.splits[0].training_years == (2023, 2024, 2025)
    assert all(split.selected_candidate_id == "zulu" for split in result.splits)
    assert result.informative_score_profiles == 2
    assert result.probability_of_backtest_overfitting is not None
    assert result.unavailable_reason is None
    json.dumps(result.to_dict(), allow_nan=False)


def test_exact_calendar_cscv_preserves_unavailable_all_tied_result() -> None:
    result = build_exact_calendar_cscv_pbo(
        (
            _daily_series("first", multiplier=1.0),
            _daily_series("second", multiplier=1.0),
        ),
        registered_candidate_order=("first", "second"),
        calendar_years=tuple(range(2020, 2026)),
        annualization_factor=365.0,
        registered_calendar_dates=_calendar_days(2020, 2026),
    )

    assert result.probability_of_backtest_overfitting is None
    assert result.informative_score_profiles == 1
    assert result.unavailable_reason == (
        "fewer_than_two_informative_non_tied_candidate_score_profiles"
    )
    assert all(split.selected_candidate_id is None for split in result.splits)


def test_exact_calendar_cscv_rejects_missing_or_unaligned_days() -> None:
    complete = _daily_series("complete", multiplier=1.0)
    missing = CandidateDailyReturnSeries(
        "missing",
        complete.observations[:-1],
    )
    with pytest.raises(ValueError, match="cover every registered calendar day"):
        build_exact_calendar_cscv_pbo(
            (complete, missing),
            registered_candidate_order=("complete", "missing"),
            calendar_years=tuple(range(2020, 2026)),
            annualization_factor=365.0,
            registered_calendar_dates=_calendar_days(2020, 2026),
        )

    shifted = CandidateDailyReturnSeries(
        "shifted",
        tuple(
            TimestampedDailyReturn(item.timestamp + timedelta(hours=1), item.daily_return)
            for item in complete.observations
        ),
    )
    with pytest.raises(ValueError, match="exactly aligned"):
        build_exact_calendar_cscv_pbo(
            (complete, shifted),
            registered_candidate_order=("complete", "shifted"),
            calendar_years=tuple(range(2020, 2026)),
            annualization_factor=365.0,
            registered_calendar_dates=_calendar_days(2020, 2026),
        )


def _closed_trade(
    trade_id: str,
    asset: str,
    entry: datetime,
    exit_: datetime,
    net_return: float,
) -> ClosedTradeOutcome:
    return ClosedTradeOutcome(trade_id, asset, entry, exit_, net_return)


def test_observed_power_inputs_capture_cadence_dependence_and_holding_overlap() -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    end = datetime(2022, 1, 1, tzinfo=UTC)
    outcomes = (
        _closed_trade("t1", "BTC", start + timedelta(days=5), start + timedelta(days=20), 0.01),
        _closed_trade("t2", "ETH", start + timedelta(days=10), start + timedelta(days=30), 0.02),
        _closed_trade("t3", "BTC", start + timedelta(days=100), start + timedelta(days=110), 0.03),
        _closed_trade("t4", "ETH", start + timedelta(days=400), start + timedelta(days=420), 0.04),
    )

    observed = observe_prospective_design_inputs(
        outcomes,
        observation_start=start,
        observation_end_exclusive=end,
        effective_sample_max_lag=2,
        right_censored_entries=1,
        independence_bootstrap_samples=200,
        independence_bootstrap_seed=20260721,
    )

    assert observed.filled_entries == 5
    assert observed.completed_exits == observed.matured_outcomes == 4
    assert observed.right_censored_entries == 1
    assert observed.filled_entry_cadence_per_year == pytest.approx(5 / (731 / 365))
    assert observed.matured_outcome_cadence_per_year == pytest.approx(4 / (731 / 365))
    assert (
        0.0
        < observed.conservative_matured_outcome_cadence_per_year
        < (observed.matured_outcome_cadence_per_year)
    )
    assert observed.raw_lag1_correlation == pytest.approx(1.0)
    assert observed.capped_lag1_correlation == 0.94
    assert 0.0 < observed.serial_independence_ratio <= 1.0
    assert observed.holding_time_independence_ratio == pytest.approx(55.0 / 65.0)
    assert observed.observed_independence_ratio == min(
        observed.serial_independence_ratio,
        observed.holding_time_independence_ratio,
    )
    assert 0.0 < observed.observed_independence_ratio <= 1.0
    assert 0.0 < observed.conservative_independence_ratio <= (observed.observed_independence_ratio)
    assert observed.holding_overlap.overlapping_pairs == 1
    assert observed.holding_overlap.trades_overlapping_any_other == 2
    assert observed.holding_overlap.maximum_concurrent_trades == 2
    assert observed.holding_overlap.summed_holding_days == 65.0
    assert observed.holding_overlap.union_holding_days == 55.0
    json.dumps(observed.to_dict(), allow_nan=False)

    design = search_prospective_duration_from_observed_trades(
        observed,
        minimum_standardized_effect=0.2,
        effective_trials=7,
        familywise_alpha=0.05,
        familywise_error_allocation=0.5,
        target_power=0.8,
        simulations=50,
        seed=20260721,
        monte_carlo_confidence=0.95,
        duration_step_years=0.25,
        maximum_duration_years=0.5,
        required_consecutive_passes=2,
    )
    assert design.search.evaluated
    first_config = design.search.evaluated[0].config
    assert first_config.cadence_per_year == (observed.conservative_matured_outcome_cadence_per_year)
    assert first_config.independence_ratio == observed.conservative_independence_ratio
    json.dumps(design.to_dict(), allow_nan=False)


def test_holding_overlap_reduces_effective_evidence_and_extends_power_duration() -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    end = datetime(2022, 1, 1, tzinfo=UTC)
    returns = (
        0.01,
        -0.01,
        0.02,
        -0.02,
        0.03,
        -0.03,
        0.015,
        -0.015,
        0.025,
        -0.025,
        0.035,
        -0.035,
    )

    def observed(holding_days: int):
        outcomes = tuple(
            _closed_trade(
                f"trade-{index}",
                "BTC" if index % 2 == 0 else "ETH",
                start + timedelta(days=30 * index),
                start + timedelta(days=30 * index + holding_days),
                net_return,
            )
            for index, net_return in enumerate(returns)
        )
        return observe_prospective_design_inputs(
            outcomes,
            observation_start=start,
            observation_end_exclusive=end,
            effective_sample_max_lag=3,
            independence_bootstrap_samples=200,
            independence_bootstrap_seed=20260721,
        )

    independent = observed(5)
    overlapping = observed(120)
    assert independent.serial_independence_ratio == overlapping.serial_independence_ratio == 1.0
    assert independent.holding_time_independence_ratio == 1.0
    assert overlapping.holding_time_independence_ratio == pytest.approx(0.3125)

    def duration(inputs) -> float | None:
        return search_prospective_duration_from_observed_trades(
            inputs,
            minimum_standardized_effect=0.7,
            effective_trials=1,
            familywise_alpha=0.1,
            familywise_error_allocation=0.5,
            target_power=0.5,
            simulations=500,
            seed=20260721,
            monte_carlo_confidence=0.9,
            duration_step_years=0.25,
            maximum_duration_years=5.0,
            required_consecutive_passes=1,
        ).search.minimum_duration_years

    independent_duration = duration(independent)
    overlapping_duration = duration(overlapping)
    assert independent_duration is not None
    assert overlapping_duration is None or overlapping_duration > independent_duration


def test_observed_power_inputs_reject_duplicates_and_partial_interval_trades() -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    end = datetime(2021, 1, 1, tzinfo=UTC)
    trade = _closed_trade("same", "BTC", start, start + timedelta(days=1), 0.01)
    with pytest.raises(ValueError, match="ids must be unique"):
        observe_prospective_design_inputs(
            (trade, trade),
            observation_start=start,
            observation_end_exclusive=end,
        )
    outside = _closed_trade(
        "outside",
        "BTC",
        start - timedelta(days=1),
        start + timedelta(days=1),
        0.01,
    )
    with pytest.raises(ValueError, match="wholly inside"):
        observe_prospective_design_inputs(
            (outside,),
            observation_start=start,
            observation_end_exclusive=end,
        )


def test_contribution_removals_preserve_asset_year_and_trade_evidence() -> None:
    contributions = (
        TradeContribution("t1", "BTC", datetime(2020, 2, 1, tzinfo=UTC), 10.0),
        TradeContribution("t2", "ETH", datetime(2020, 3, 1, tzinfo=UTC), -3.0),
        TradeContribution("t3", "BTC", datetime(2021, 2, 1, tzinfo=UTC), 2.0),
        TradeContribution("t4", "ETH", datetime(2021, 3, 1, tzinfo=UTC), 1.0),
    )
    attributed = tuple(
        AttributedContribution(
            contribution_id=f"attributed-{item.trade_id}",
            asset=item.asset,
            timestamp=item.exit_ts,
            net_contribution=item.net_contribution,
        )
        for item in contributions
    )
    summary = summarize_contribution_robustness(
        contributions,
        attributed_contributions=attributed,
    )

    assert summary.total_contribution == 10.0
    assert summary.closed_trades == 4
    assert summary.attributed_observations == 4
    assert summary.best_closed_trade.removed_bucket == "t1"
    assert summary.best_closed_trade.contribution_after_removal == 0.0
    assert summary.best_asset_year.removed_bucket == "BTC:2020"
    assert summary.best_calendar_year.removed_bucket == "2020"
    assert summary.best_asset.removed_bucket == "BTC"
    assert summary.best_asset.contribution_after_removal == -2.0
    assert summary.maximum_asset_positive_share == 1.0
    assert summary.maximum_calendar_year_positive_share == pytest.approx(0.7)
    assert {item.removed_bucket for item in summary.all_asset_removals} == {"BTC", "ETH"}
    json.dumps(summary.to_dict(), allow_nan=False)


def test_fold_neighbor_cost_and_benchmark_summaries_apply_only_supplied_protocol_gates() -> None:
    positivity = summarize_fold_asset_positivity(
        (
            NamedReturn("oos-2022", 3.0),
            NamedReturn("oos-2023", -1.0),
            NamedReturn("oos-2024", 2.0),
            NamedReturn("oos-2025", 1.0),
        ),
        (NamedReturn("BTC-USDC", 1.0), NamedReturn("ETH-USDC", 2.0)),
    )
    assert positivity.positive_fold_ids == ("oos-2022", "oos-2024", "oos-2025")
    assert positivity.all_assets_positive is True

    candidates = (
        CandidateOOSPerformance("control", 4.0, positivity.fold_returns),
        CandidateOOSPerformance(
            "neighbor_a",
            2.0,
            tuple(
                NamedReturn(item.label, item.return_pct + 0.5) for item in positivity.fold_returns
            ),
        ),
        CandidateOOSPerformance(
            "neighbor_b",
            1.0,
            tuple(
                NamedReturn(item.label, item.return_pct - 0.5) for item in positivity.fold_returns
            ),
        ),
    )
    neighbor = summarize_neighbor_support(
        candidates,
        selected_candidate_id="control",
        registered_candidate_order=("control", "neighbor_a", "neighbor_b"),
        minimum_positive_selected_folds=3,
        minimum_positive_pooled_neighbors=2,
        minimum_positive_fold_results_total=4,
    )
    assert neighbor.positive_pooled_neighbor_ids == ("neighbor_a", "neighbor_b")
    assert neighbor.requirements_met is True
    canonical_manifest_bytes({"payload": neighbor.to_dict()})

    costs = summarize_cost_tolerance(
        expected_return_pct=5.0,
        stressed_return_pct=0.0,
        break_even_cost_per_traded_notional_bps=300.0,
        expected_cost_per_traded_notional_bps=121.0,
        stressed_edge_reversal_limit_fraction=1.0,
    )
    assert costs.stressed_edge_degradation_fraction == 1.0
    assert costs.requirements_met is True
    failed_costs = summarize_cost_tolerance(
        expected_return_pct=-1.0,
        stressed_return_pct=-2.0,
        break_even_cost_per_traded_notional_bps=300.0,
        expected_cost_per_traded_notional_bps=121.0,
        stressed_edge_reversal_limit_fraction=1.0,
    )
    assert failed_costs.stressed_edge_degradation_fraction is None
    assert failed_costs.requirements_met is False
    json.dumps(failed_costs.to_dict(), allow_nan=False)

    pareto = compare_raw_registered_benchmarks(
        PerformancePoint("strategy", 5.0, 10.0, 0.5, 10.0),
        (
            PerformancePoint("lower_return", 4.0, 4.0, 1.0, 5.0),
            PerformancePoint("cash", 0.0, 0.0, 0.0, 0.0),
            PerformancePoint("dominant", 6.0, 8.0, 0.6, 20.0),
        ),
    )
    assert pareto.dominating_benchmark_ids == ("dominant",)
    assert pareto.any_benchmark_dominates is True
    assert pareto.metric_scaling == "none_exactly_as_executed"
    assert pareto.comparisons[0].benchmark.total_return_pct == 4.0
    assert all("exposure_scale" not in comparison.to_dict() for comparison in pareto.comparisons)
    json.dumps(pareto.to_dict(), allow_nan=False)


def _report_evidence(
    arm_id: str,
    asset: str,
    fold_id: str,
    cost_scenario: str,
    economic_days: tuple[date, ...],
    *,
    daily_return: float,
    include_trade: bool = True,
    exposure_pct: float = 20.0,
    aggregate_execution_cost: float = 50.0,
    turnover_notional: float = 10_000.0,
) -> StandardReportEvidence:
    capital = 10_000.0
    equity = capital
    curve: list[tuple[datetime, float]] = []
    for day in economic_days:
        equity *= 1.0 + daily_return
        curve.append(
            (
                datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC),
                equity,
            )
        )
    trades = (
        (
            PersistedTradeContribution(
                0,
                asset,
                curve[0][0] - timedelta(days=1),
                curve[min(30, len(curve) - 1)][0] - timedelta(days=1),
                10.0,
            ),
        )
        if include_trade
        else ()
    )
    return StandardReportEvidence(
        arm_id=arm_id,
        asset=asset,
        fold_id=fold_id,
        cost_scenario=cost_scenario,
        trial_id=f"trial:{arm_id}:{asset}:{fold_id}:{cost_scenario}",
        report_sha256=(f"{len(arm_id):x}" * 64)[:64],
        timeframe="1440m",
        initial_capital=capital,
        equity_curve=tuple(curve),
        trades=trades,
        total_return_pct=(equity / capital - 1.0) * 100.0,
        max_drawdown_pct=0.0,
        sortino_ratio=1.0,
        exposure_pct=exposure_pct,
        gross_pnl=equity - capital + (100.0 if include_trade else 0.0),
        aggregate_execution_cost=aggregate_execution_cost,
        turnover_notional=turnover_notional,
        terminal_position_open=False,
        terminal_entry_ts=None,
        terminal_exit_cost=0.0,
        terminal_exit_reference_notional=0.0,
        terminal_exit_policy_id=None,
    )


def _robustness_config() -> RobustnessEvidenceConfig:
    return RobustnessEvidenceConfig(
        assets=("ALPHA", "BETA"),
        fold_years=(("fold-2022", 2022), ("fold-2023", 2023)),
        baseline_arm_id="BASELINE",
        neighbor_candidate_order=("BASELINE", "NEIGHBOR_WEAK", "NEIGHBOR_STRONG"),
        benchmark_order=_REGISTERED_BENCHMARK_IDS,
        economic_period_date_rule="normalized_close_timestamp_minus_one_calendar_day",
        benchmark_comparator_id=(
            "registered_10pct_capital_allocation_return_drawdown_sortino_pareto_v1"
        ),
        expected_cost_scenario="expected",
        stressed_cost_scenario="stressed",
        deflated_sharpe_effective_trials=7.0,
        deflated_sharpe_upstream_attempted_trials=4,
        deflated_sharpe_upstream_finite_count=2,
        deflated_sharpe_current_candidate_order=(
            "BASELINE",
            "NEIGHBOR_WEAK",
            "NEIGHBOR_STRONG",
        ),
        annualization_factor=365.0,
        bootstrap_block_bars=30,
        bootstrap_samples=40,
        bootstrap_seed=20260721,
        confidence_level=0.95,
        minimum_positive_selected_folds=1,
        minimum_positive_pooled_neighbors=1,
        minimum_positive_candidate_fold_cells_total=2,
        minimum_training_selected_folds_supporting_baseline=1,
        stressed_edge_reversal_limit_fraction=1.0,
        maximum_positive_contribution_share=0.75,
        maximum_expected_pooled_drawdown_pct=5.0,
        maximum_expected_recovery_duration_days=730.0,
        maximum_expected_daily_es95_pct=1.0,
        maximum_stressed_pooled_drawdown_pct=7.5,
        maximum_closed_or_terminal_contribution_loss_pct=2.0,
        registered_redesign_changes=_REGISTERED_REDESIGN_CHANGES,
    )


def _economic_regimes(config: RobustnessEvidenceConfig) -> tuple[EconomicRegimeObservation, ...]:
    return tuple(
        EconomicRegimeObservation(
            asset=asset,
            fold_id=fold_id,
            economic_period_date=day,
            volatility_bucket="q2",
            trend_bucket="positive",
            threshold_source_id=f"purged-training:{fold_id}:{asset}",
        )
        for fold_id, year in config.fold_years
        for asset in config.assets
        for day in _calendar_days(year, year + 1)
    )


def _benchmark_evidence(
    config: RobustnessEvidenceConfig,
) -> tuple[RegisteredBenchmarkEvidence, ...]:
    daily_returns = tuple(
        TimestampedDailyReturn(datetime.combine(day, time.min, tzinfo=UTC), 0.0)
        for _fold_id, year in config.fold_years
        for day in _calendar_days(year, year + 1)
    )
    return tuple(
        RegisteredBenchmarkEvidence(
            benchmark_id=benchmark_id,
            fold_id="pooled-oos",
            cost_scenario=config.expected_cost_scenario,
            source_trial_id=f"benchmark:{benchmark_id}",
            evidence_sha256=f"{index + 1:x}" * 64,
            execution_definition_id=f"{benchmark_id}:registered-execution-v1",
            daily_returns=daily_returns,
            average_exposure_pct=0.0,
        )
        for index, benchmark_id in enumerate(config.benchmark_order)
    )


def _sharpe_dispersion(
    config: RobustnessEvidenceConfig,
    *,
    upstream_attempted_trials: int | None = None,
) -> TrialSharpeDispersion:
    finite_count = config.deflated_sharpe_upstream_finite_count
    return TrialSharpeDispersion(
        upstream_attempted_trials=(
            config.deflated_sharpe_upstream_attempted_trials
            if upstream_attempted_trials is None
            else upstream_attempted_trials
        ),
        upstream_finite_sharpes=tuple(
            -0.5 + index / max(1, finite_count - 1) for index in range(finite_count)
        ),
        current_candidate_sharpes=tuple(
            (candidate_id, 0.1 + index * 0.01)
            for index, candidate_id in enumerate(config.deflated_sharpe_current_candidate_order)
        ),
        source="attested-upstream-plus-registered-current-ledger",
    )


def test_fixed_baseline_builder_keeps_baseline_and_adaptive_evidence_separate() -> None:
    config = _robustness_config()
    reports: list[StandardReportEvidence] = []
    for arm_index, arm_id in enumerate(config.neighbor_candidate_order):
        for fold_id, year in config.fold_years:
            days = _calendar_days(year, year + 1)
            for asset in config.assets:
                reports.append(
                    _report_evidence(
                        arm_id,
                        asset,
                        fold_id,
                        "expected",
                        days,
                        daily_return=0.0002 - arm_index * 0.000002,
                    )
                )
    terminal_index = next(
        index
        for index, report in enumerate(reports)
        if (report.arm_id, report.asset, report.fold_id)
        == (config.baseline_arm_id, config.assets[0], config.fold_years[0][0])
    )
    terminal_source = reports[terminal_index]
    reports[terminal_index] = replace(
        terminal_source,
        terminal_position_open=True,
        terminal_entry_ts=terminal_source.equity_curve[-10][0] - timedelta(days=1),
        terminal_exit_cost=1.0,
        terminal_exit_reference_notional=1_000.0,
        terminal_exit_policy_id="terminal-liquidation:expected:final-mark-v1",
    )
    for fold_id, year in config.fold_years:
        for asset in config.assets:
            reports.append(
                _report_evidence(
                    config.baseline_arm_id,
                    asset,
                    fold_id,
                    "stressed",
                    _calendar_days(year, year + 1),
                    daily_return=0.00005,
                )
            )

    adaptive = AdaptiveSelectorEvidence(
        selections=tuple(
            AdaptiveFoldSelection(
                fold_id,
                config.baseline_arm_id if index == 0 else config.neighbor_candidate_order[1],
                index == 0,
            )
            for index, (fold_id, _year) in enumerate(config.fold_years)
        ),
        fold_returns=tuple(NamedReturn(fold_id, 1.0) for fold_id, _year in config.fold_years),
        asset_returns=tuple(NamedReturn(asset, 2.0) for asset in config.assets),
        pooled_daily_returns=tuple(
            TimestampedDailyReturn(datetime.combine(day, time.min, tzinfo=UTC), 0.001)
            for _fold_id, year in config.fold_years
            for day in _calendar_days(year, year + 1)
        ),
    )
    result = build_fixed_baseline_robustness_evidence(
        reports,
        config=config,
        expected_cost_benchmarks=_benchmark_evidence(config),
        economic_regimes=_economic_regimes(config),
        sharpe_dispersion=_sharpe_dispersion(config),
        adaptive_selector=adaptive,
    )

    expected_days = sum(len(_calendar_days(year, year + 1)) for _fold_id, year in config.fold_years)
    assert result.disposition_source_arm_id == config.baseline_arm_id
    assert len(result.expected_pooled_daily_returns) == expected_days
    assert result.positivity.positive_fold_ids == tuple(
        fold_id for fold_id, _year in config.fold_years
    )
    assert result.positivity.all_assets_positive is True
    assert result.neighbor_support.requirements_met is True
    assert result.contribution_share_requirement_met is True
    assert result.contribution_ex_best_closed_trade_positive is True
    assert result.contribution_ex_best_calendar_year_positive is True
    reconciliation = result.contribution_robustness.attribution_reconciliation
    assert reconciliation.reconciled is True
    assert reconciliation.reconciliation_error == pytest.approx(0.0, abs=1e-12)
    assert reconciliation.terminal_position_attributed_observations == 10
    assert reconciliation.terminal_position_attributed_contribution != 0.0
    assert len(reconciliation.terminal_position_contributions) == 1
    assert reconciliation.closed_trade_contributions[0].net_contribution == pytest.approx(
        0.5 * ((1.0 + 0.0002) ** 31 - 1.0)
    )
    assert reconciliation.total_attributed_contribution == pytest.approx(
        reconciliation.closed_trade_attributed_contribution
        + reconciliation.terminal_position_attributed_contribution
        + reconciliation.unattributed_residual_contribution
    )
    assert result.sharpe_diagnostics.available is True
    assert result.absolute_risk.requirements_met is True
    assert result.absolute_risk.expected_pooled_max_drawdown_pct <= 5.0
    assert result.absolute_risk.expected_pooled_recovery_duration_days <= 730.0
    assert result.absolute_risk.expected_daily_es95_pct <= 1.0
    assert result.absolute_risk.stressed_pooled_max_drawdown_pct <= 7.5
    assert result.absolute_risk.worst_closed_or_terminal_contribution_loss_pct <= 2.0
    assert (
        result.quantitative_gate_checks["worst_closed_or_terminal_contribution_loss_within_limit"]
        is True
    )
    assert result.adaptive_selector_can_rescue_baseline is False
    assert result.adaptive_selector_diagnostic is not None
    assert result.adaptive_selector_diagnostic["baseline_training_support_requirement_met"] is True
    assert result.quantitative_robustness_gate_eligible is True
    assert result.blocking_quantitative_gate_ids == ()
    assert result.evidence_complete is True
    assert result.completion_conclusion == "COMPLETE"
    assert result.economic_period_date_rule.endswith("minus_one_calendar_day")
    assert result.benchmark_comparator_contract["comparator_id"] == (
        "registered_10pct_capital_allocation_return_drawdown_sortino_pareto_v1"
    )
    assert result.regime_contributions.observations == len(config.assets) * expected_days
    json.dumps(result.to_dict(), allow_nan=False)
    canonical_manifest_bytes({"payload": result.to_dict()})

    with pytest.raises(ValueError, match="upstream attempted trial count differs"):
        build_fixed_baseline_robustness_evidence(
            reports,
            config=config,
            expected_cost_benchmarks=_benchmark_evidence(config),
            economic_regimes=_economic_regimes(config),
            sharpe_dispersion=_sharpe_dispersion(
                config,
                upstream_attempted_trials=config.deflated_sharpe_upstream_attempted_trials + 1,
            ),
            adaptive_selector=adaptive,
        )
    with pytest.raises(ValueError, match="regime observations must exactly cover"):
        build_fixed_baseline_robustness_evidence(
            reports,
            config=config,
            expected_cost_benchmarks=_benchmark_evidence(config),
            economic_regimes=_economic_regimes(config)[:-1],
            sharpe_dispersion=_sharpe_dispersion(config),
            adaptive_selector=adaptive,
        )


def test_fixed_baseline_builder_fails_closed_on_registry_or_dsr_gaps() -> None:
    config = _robustness_config()
    reports = [
        _report_evidence(
            arm_id,
            asset,
            fold_id,
            "expected",
            _calendar_days(year, year + 1),
            daily_return=0.0001,
        )
        for arm_id in config.neighbor_candidate_order
        for fold_id, year in config.fold_years
        for asset in config.assets
    ]
    reports.extend(
        _report_evidence(
            config.baseline_arm_id,
            asset,
            fold_id,
            "stressed",
            _calendar_days(year, year + 1),
            daily_return=0.0001,
        )
        for fold_id, year in config.fold_years
        for asset in config.assets
    )
    result = build_fixed_baseline_robustness_evidence(
        reports,
        config=config,
        expected_cost_benchmarks=_benchmark_evidence(config),
        economic_regimes=_economic_regimes(config),
        sharpe_dispersion=None,
        adaptive_selector=None,
    )
    assert result.sharpe_diagnostics.available is False
    assert "attested_cross_trial_sharpe_dispersion" in result.missing_evidence_ids
    assert "training_selected_diagnostic" in result.missing_evidence_ids
    assert "training_selector_supports_baseline" in result.blocking_quantitative_gate_ids
    assert result.completion_conclusion == "BLOCKED_INSUFFICIENT_EVIDENCE"

    with pytest.raises(ValueError, match="exactly cover"):
        build_fixed_baseline_robustness_evidence(
            reports[:-1],
            config=config,
            expected_cost_benchmarks=_benchmark_evidence(config),
            economic_regimes=_economic_regimes(config),
            sharpe_dispersion=None,
        )


def test_zero_trade_baseline_is_complete_not_applicable_and_ineligible() -> None:
    config = _robustness_config()
    reports: list[StandardReportEvidence] = []
    for arm_id in config.neighbor_candidate_order:
        for fold_id, year in config.fold_years:
            for asset in config.assets:
                is_baseline = arm_id == config.baseline_arm_id
                reports.append(
                    _report_evidence(
                        arm_id,
                        asset,
                        fold_id,
                        "expected",
                        _calendar_days(year, year + 1),
                        daily_return=0.0 if is_baseline else 0.0001,
                        include_trade=not is_baseline,
                        exposure_pct=0.0 if is_baseline else 20.0,
                        aggregate_execution_cost=0.0 if is_baseline else 50.0,
                        turnover_notional=0.0 if is_baseline else 10_000.0,
                    )
                )
    reports.extend(
        _report_evidence(
            config.baseline_arm_id,
            asset,
            fold_id,
            "stressed",
            _calendar_days(year, year + 1),
            daily_return=0.0,
            include_trade=False,
            exposure_pct=0.0,
            aggregate_execution_cost=0.0,
            turnover_notional=0.0,
        )
        for fold_id, year in config.fold_years
        for asset in config.assets
    )
    timestamps = tuple(
        TimestampedDailyReturn(datetime.combine(day, time.min, tzinfo=UTC), 0.0)
        for _fold_id, year in config.fold_years
        for day in _calendar_days(year, year + 1)
    )
    adaptive = AdaptiveSelectorEvidence(
        selections=tuple(
            AdaptiveFoldSelection(fold_id, config.baseline_arm_id, True)
            for fold_id, _year in config.fold_years
        ),
        fold_returns=tuple(NamedReturn(fold_id, 0.0) for fold_id, _year in config.fold_years),
        asset_returns=tuple(NamedReturn(asset, 0.0) for asset in config.assets),
        pooled_daily_returns=timestamps,
    )
    result = build_fixed_baseline_robustness_evidence(
        reports,
        config=config,
        expected_cost_benchmarks=_benchmark_evidence(config),
        economic_regimes=_economic_regimes(config),
        sharpe_dispersion=_sharpe_dispersion(config),
        adaptive_selector=adaptive,
    )
    assert result.evidence_complete is True
    assert result.completion_conclusion == "COMPLETE"
    assert result.raw_baseline_zero_trades_or_exposure is True
    assert result.blocking_quantitative_gate_ids[0] == (
        "zero_completed_baseline_trades_or_exposure"
    )
    assert result.quantitative_robustness_gate_eligible is False
    assert result.contribution_robustness.available is False
    assert result.contribution_robustness.unavailable_reason == (
        "NOT_APPLICABLE_DUE_TO_ZERO_TRADES"
    )


def test_absolute_risk_gates_block_severe_drawdown_and_position_loss() -> None:
    config = _robustness_config()
    reports = [
        _report_evidence(
            arm_id,
            asset,
            fold_id,
            "expected",
            _calendar_days(year, year + 1),
            daily_return=-0.01 if arm_id == config.baseline_arm_id else 0.0001,
        )
        for arm_id in config.neighbor_candidate_order
        for fold_id, year in config.fold_years
        for asset in config.assets
    ]
    reports.extend(
        _report_evidence(
            config.baseline_arm_id,
            asset,
            fold_id,
            "stressed",
            _calendar_days(year, year + 1),
            daily_return=-0.02,
        )
        for fold_id, year in config.fold_years
        for asset in config.assets
    )
    result = build_fixed_baseline_robustness_evidence(
        reports,
        config=config,
        expected_cost_benchmarks=_benchmark_evidence(config),
        economic_regimes=_economic_regimes(config),
        sharpe_dispersion=None,
        adaptive_selector=None,
    )

    assert result.absolute_risk.expected_drawdown_requirement_met is False
    assert result.absolute_risk.stressed_drawdown_requirement_met is False
    assert result.absolute_risk.position_loss_requirement_met is False
    assert result.absolute_risk.requirements_met is False
    assert result.absolute_risk.worst_position_kind == "closed_trade"
    assert result.quantitative_gate_checks["expected_pooled_max_drawdown_within_limit"] is False
    assert "expected_pooled_max_drawdown_within_limit" in (result.blocking_quantitative_gate_ids)
    assert "worst_closed_or_terminal_contribution_loss_within_limit" in (
        result.blocking_quantitative_gate_ids
    )


def test_registered_redesign_builder_returns_all_candidates_without_selection() -> None:
    config = _robustness_config()
    candidate_order = config.neighbor_candidate_order[1:]
    weak_candidate_id = candidate_order[0]
    reports = [
        _report_evidence(
            candidate_id,
            asset,
            fold_id,
            cost_scenario,
            _calendar_days(year, year + 1),
            daily_return=(
                -0.001
                if candidate_id == weak_candidate_id and cost_scenario == "expected"
                else -0.002
                if candidate_id == weak_candidate_id
                else 0.0002
                if cost_scenario == "expected"
                else 0.00005
            ),
        )
        for candidate_id in candidate_order
        for cost_scenario in ("expected", "stressed")
        for fold_id, year in config.fold_years
        for asset in config.assets
    ]
    result = build_registered_redesign_candidate_evidence(
        reports,
        config=config,
        expected_cost_benchmarks=_benchmark_evidence(config),
        economic_regimes=_economic_regimes(config),
    )

    assert result.registered_candidate_order == candidate_order
    assert tuple(item.candidate_id for item in result.candidates) == candidate_order
    assert result.no_selection_performed is True
    assert len(result.candidates) == 2
    weak = result.candidates[0]
    assert weak.registered_change.to_dict() == {
        "parameter_name": "lookback_period",
        "baseline_value": 30,
        "candidate_value": 20,
    }
    assert len(weak.source_reports) == 8
    assert weak.quantitative_candidate_gate_eligible is False
    assert "pooled_expected_cost_oos_return_positive" in weak.blocking_gate_ids
    strong = result.candidates[1]
    assert strong.registered_change.to_dict() == {
        "parameter_name": "lookback_period",
        "baseline_value": 30,
        "candidate_value": 40,
    }
    assert strong.quantitative_candidate_gate_eligible is True
    assert strong.blocking_gate_ids == ()
    assert all(
        item.gate_checks["single_registered_economically_defensible_change_confirmed"]
        for item in result.candidates
    )
    json.dumps(result.to_dict(), allow_nan=False)

    with pytest.raises(ValueError, match="exactly cover and order every registered candidate"):
        build_registered_redesign_candidate_evidence(
            reports[:-1],
            config=config,
            expected_cost_benchmarks=_benchmark_evidence(config),
            economic_regimes=_economic_regimes(config),
        )


def test_robustness_and_redesign_builders_accept_alternate_registry() -> None:
    config = RobustnessEvidenceConfig(
        assets=("SPY",),
        fold_years=(("validation-2023", 2023), ("holdout-2025", 2025)),
        baseline_arm_id="control",
        neighbor_candidate_order=("neighbor", "control"),
        benchmark_order=("cash_proxy",),
        economic_period_date_rule="normalized_close_timestamp_minus_one_calendar_day",
        benchmark_comparator_id="generic_raw_capital_pareto_v2",
        expected_cost_scenario="base_cost",
        stressed_cost_scenario="stress_cost",
        deflated_sharpe_effective_trials=5.0,
        deflated_sharpe_upstream_attempted_trials=3,
        deflated_sharpe_upstream_finite_count=2,
        deflated_sharpe_current_candidate_order=("control", "neighbor"),
        annualization_factor=252.0,
        bootstrap_block_bars=7,
        bootstrap_samples=20,
        bootstrap_seed=91,
        confidence_level=0.90,
        minimum_positive_selected_folds=1,
        minimum_positive_pooled_neighbors=1,
        minimum_positive_candidate_fold_cells_total=2,
        minimum_training_selected_folds_supporting_baseline=0,
        stressed_edge_reversal_limit_fraction=0.5,
        maximum_positive_contribution_share=0.8,
        maximum_expected_pooled_drawdown_pct=10.0,
        maximum_expected_recovery_duration_days=1_000.0,
        maximum_expected_daily_es95_pct=2.0,
        maximum_stressed_pooled_drawdown_pct=15.0,
        maximum_closed_or_terminal_contribution_loss_pct=4.0,
        registered_redesign_changes=(("neighbor", RegisteredOneFactorChange("lookback", 20, 25)),),
    )
    baseline_reports = [
        _report_evidence(
            candidate_id,
            asset,
            fold_id,
            config.expected_cost_scenario,
            _calendar_days(year, year + 1),
            daily_return=0.0002,
        )
        for candidate_id in config.neighbor_candidate_order
        for fold_id, year in config.fold_years
        for asset in config.assets
    ]
    baseline_reports.extend(
        _report_evidence(
            config.baseline_arm_id,
            asset,
            fold_id,
            config.stressed_cost_scenario,
            _calendar_days(year, year + 1),
            daily_return=0.0001,
        )
        for fold_id, year in config.fold_years
        for asset in config.assets
    )

    baseline = build_fixed_baseline_robustness_evidence(
        baseline_reports,
        config=config,
        expected_cost_benchmarks=_benchmark_evidence(config),
        economic_regimes=_economic_regimes(config),
        sharpe_dispersion=_sharpe_dispersion(config),
    )
    assert baseline.disposition_source_arm_id == "control"
    assert baseline.positivity.positive_asset_ids == ("SPY",)
    assert baseline.neighbor_support.registered_candidate_order == ("neighbor", "control")
    assert baseline.benchmark_comparator_contract["comparator_id"] == (
        "generic_raw_capital_pareto_v2"
    )

    redesign_reports = [
        _report_evidence(
            "neighbor",
            asset,
            fold_id,
            cost_scenario,
            _calendar_days(year, year + 1),
            daily_return=0.0002 if cost_scenario == "base_cost" else 0.0001,
        )
        for cost_scenario in (config.expected_cost_scenario, config.stressed_cost_scenario)
        for fold_id, year in config.fold_years
        for asset in config.assets
    ]
    redesign = build_registered_redesign_candidate_evidence(
        redesign_reports,
        config=config,
        expected_cost_benchmarks=_benchmark_evidence(config),
        economic_regimes=_economic_regimes(config),
    )
    assert redesign.registered_candidate_order == ("neighbor",)
    assert redesign.candidates[0].registered_change.to_dict() == {
        "parameter_name": "lookback",
        "baseline_value": 20,
        "candidate_value": 25,
    }
    assert set(redesign.candidates[0].gate_checks) >= {
        "all_registered_assets_positive",
        "minimum_positive_expected_cost_oos_folds_met",
    }
    canonical_manifest_bytes({"payload": redesign.to_dict()})


def test_standard_report_extractor_cross_checks_identity_and_terminal_cost() -> None:
    start = datetime(2022, 1, 2, tzinfo=UTC)
    evidence = {
        "report_evidence_schema_id": "vynmatrix.backtest.report-evidence",
        "report_evidence_schema_version": 1,
        "trial_id": "trial-1",
        "report_sha256": "a" * 64,
        "symbol": "BTC-USDC",
        "timeframe": "1440m",
        "meta_context": {"arm_id": "control", "runner_kind": "production_core_direct"},
        "equity_curve": [[start.isoformat(), 10_010.0]],
        "trades": [],
        "report": {
            "symbol": "BTC-USDC",
            "timeframe": "1440m",
            "cost_scenario": "expected",
            "account_ruined": False,
            "selection_blockers": ["unresolved_terminal_position"],
            "initial_capital": 10_000.0,
            "final_equity": 10_010.0,
            "total_trades": 0,
            "terminal_position": {
                "symbol": "BTC-USDC",
                "side": "long",
                "quantity": 0.1,
                "entry_ts": "2022-01-01T00:00:00+00:00",
                "mark_price": 10_000.0,
            },
            "total_return_pct": 0.1,
            "max_drawdown_pct": 0.0,
            "sortino_ratio": 1.0,
            "exposure_pct": 1.0,
            "gross_pnl": 10.0,
            "cost_breakdown": {
                "commission": 1.0,
                "half_spread": 0.1,
                "slippage": 0.0,
                "impact": 0.0,
                "total": 1.1,
            },
            "turnover_notional": 1_000.0,
        },
    }
    extracted = standard_report_evidence_from_mapping(
        evidence,
        arm_id="control",
        fold_id="oos-2022",
        cost_scenario="expected",
        terminal_exit_cost=1.0,
        terminal_exit_policy_id="terminal-liquidation:expected:BTC-USDC:final-mark-v1",
    )
    assert extracted.adjusted_equity_curve[-1][1] == 10_009.0
    assert extracted.terminal_exit_reference_notional == 1_000.0
    assert extracted.terminal_exit_policy_id == (
        "terminal-liquidation:expected:BTC-USDC:final-mark-v1"
    )
    with pytest.raises(ValueError, match="arm_id differs"):
        standard_report_evidence_from_mapping(
            evidence,
            arm_id="different-arm",
            fold_id="oos-2022",
            cost_scenario="expected",
            terminal_exit_cost=1.0,
            terminal_exit_policy_id="terminal-liquidation:expected:BTC-USDC:final-mark-v1",
        )
    with pytest.raises(ValueError, match="requires a registered positive exit cost"):
        standard_report_evidence_from_mapping(
            evidence,
            arm_id="control",
            fold_id="oos-2022",
            cost_scenario="expected",
        )

    mismatched_terminal = deepcopy(evidence)
    mismatched_terminal["report"]["terminal_position"]["symbol"] = "ETH-USDC"
    with pytest.raises(ValueError, match="terminal position symbol differs"):
        standard_report_evidence_from_mapping(
            mismatched_terminal,
            arm_id="control",
            fold_id="oos-2022",
            cost_scenario="expected",
            terminal_exit_cost=1.0,
            terminal_exit_policy_id=("terminal-liquidation:expected:BTC-USDC:final-mark-v1"),
        )

    flat = deepcopy(evidence)
    flat["report"]["terminal_position"] = None
    flat["report"]["selection_blockers"] = []
    extracted_flat = standard_report_evidence_from_mapping(
        flat,
        arm_id="control",
        fold_id="oos-2022",
        cost_scenario="expected",
    )
    assert extracted_flat.terminal_exit_cost == 0.0
    assert extracted_flat.terminal_exit_reference_notional == 0.0
    assert extracted_flat.terminal_exit_policy_id is None

    missing_blocker = deepcopy(evidence)
    missing_blocker["report"]["selection_blockers"] = []
    with pytest.raises(ValueError, match="selection blocker disagree"):
        standard_report_evidence_from_mapping(
            missing_blocker,
            arm_id="control",
            fold_id="oos-2022",
            cost_scenario="expected",
            terminal_exit_cost=1.0,
            terminal_exit_policy_id=("terminal-liquidation:expected:BTC-USDC:final-mark-v1"),
        )


def test_registered_report_cscv_accepts_alternate_registry() -> None:
    candidates = ("control", "neighbor", "alternate")
    assets = ("SPY",)
    years = tuple(range(2014, 2020))
    excluded_days = {date(2015, 4, 1), date(2018, 9, 1)}
    days = tuple(day for day in _calendar_days(years[0], years[-1] + 1) if day not in excluded_days)
    reports = tuple(
        _report_evidence(
            candidate_id,
            asset,
            "full-panel",
            "net_cost",
            days,
            daily_return=0.00001 * (candidate_index + 1),
            include_trade=False,
        )
        for candidate_index, candidate_id in enumerate(candidates)
        for asset in assets
    )

    result = build_registered_report_cscv_pbo(
        reports,
        registered_candidate_order=candidates,
        registered_asset_order=assets,
        calendar_years=years,
        registered_economic_dates=days,
        annualization_factor=252.0,
        expected_cost_scenario="net_cost",
        economic_period_date_rule="normalized_close_timestamp_minus_one_calendar_day",
    )

    assert result.result.registered_candidate_order == candidates
    assert result.result.calendar_years == years
    assert result.result.aligned_calendar_days == len(days)
    assert len(result.result.splits) == 20
    assert result.economic_date_rule.endswith("minus_one_calendar_day")
    json.dumps(result.to_dict(), allow_nan=False)

    with pytest.raises(ValueError, match="exactly cover"):
        build_registered_report_cscv_pbo(
            reports[:-1],
            registered_candidate_order=candidates,
            registered_asset_order=assets,
            calendar_years=years,
            registered_economic_dates=days,
            annualization_factor=252.0,
            expected_cost_scenario="net_cost",
            economic_period_date_rule="normalized_close_timestamp_minus_one_calendar_day",
        )

    mismatched_reports = (
        replace(reports[0], equity_curve=reports[0].equity_curve[:-1]),
        *reports[1:],
    )
    with pytest.raises(ValueError, match="registered dates"):
        build_registered_report_cscv_pbo(
            mismatched_reports,
            registered_candidate_order=candidates,
            registered_asset_order=assets,
            calendar_years=years,
            registered_economic_dates=days,
            annualization_factor=252.0,
            expected_cost_scenario="net_cost",
            economic_period_date_rule="normalized_close_timestamp_minus_one_calendar_day",
        )

    with pytest.raises(ValueError, match="registered years"):
        build_registered_report_cscv_pbo(
            reports,
            registered_candidate_order=candidates,
            registered_asset_order=assets,
            calendar_years=tuple(year - 1 for year in years),
            registered_economic_dates=days,
            annualization_factor=252.0,
            expected_cost_scenario="net_cost",
            economic_period_date_rule="normalized_close_timestamp_minus_one_calendar_day",
        )
