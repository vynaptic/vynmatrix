"""Registered redesign evidence and final report-backed CSCV diagnostics."""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from dev_cli.validation._diagnostic_common import (
    ECONOMIC_PERIOD_DATE_RULE as _ECONOMIC_PERIOD_DATE_RULE,
)
from dev_cli.validation._diagnostic_common import EXPECTED_CSCV_YEARS as _EXPECTED_CSCV_YEARS
from dev_cli.validation._diagnostic_common import MIN_PBO_CANDIDATES as _MIN_PBO_CANDIDATES
from dev_cli.validation._diagnostic_common import invalid as _invalid
from dev_cli.validation._diagnostic_common import require_finite as _require_finite
from dev_cli.validation._diagnostic_common import require_nonempty as _require_nonempty
from dev_cli.validation.backtest.metrics import max_drawdown_pct, sharpe_ratio, sortino_ratio
from dev_cli.validation.contribution_diagnostics import summarize_contribution_robustness
from dev_cli.validation.cscv import (
    CalendarYearCSCVPBOResult,
    CandidateDailyReturnSeries,
    TimestampedDailyReturn,
    build_exact_calendar_cscv_pbo,
)
from dev_cli.validation.economic_regimes import EconomicRegimeObservation
from dev_cli.validation.fixed_robustness import (
    ContributionRobustnessAvailability,
    RegisteredRedesignCandidateEvidence,
    RegisteredRedesignCandidateEvidenceSet,
    _build_position_attribution,
    _compounded_return_pct,
    _equity_from_returns,
    _fold_portfolio,
    _PortfolioFoldEvidence,
    _summarize_absolute_risk,
    _summarize_regime_contributions,
    _validate_robustness_config,
)
from dev_cli.validation.performance_diagnostics import (
    NamedReturn,
    PerformancePoint,
    compare_raw_registered_benchmarks,
    summarize_cost_tolerance,
    summarize_fold_asset_positivity,
)
from dev_cli.validation.report_evidence import StandardReportEvidence
from dev_cli.validation.robustness_models import (
    RegisteredBenchmarkEvidence,
    RobustnessEvidenceConfig,
)


def build_registered_redesign_candidate_evidence(  # noqa: PLR0915
    reports: Sequence[StandardReportEvidence],
    *,
    config: RobustnessEvidenceConfig,
    expected_cost_benchmarks: Sequence[RegisteredBenchmarkEvidence],
    economic_regimes: Sequence[EconomicRegimeObservation],
) -> RegisteredRedesignCandidateEvidenceSet:
    """Build complete evidence for every registered one-factor candidate.

    Every registered candidate is returned in protocol order, including failed
    candidates.  This function performs no winner selection and issues no
    redesign or deployment verdict.
    """

    _validate_robustness_config(config)
    changes = config.registered_redesign_changes
    if not changes:
        _invalid("registered redesign changes are required for redesign evidence")
    candidate_order = tuple(candidate_id for candidate_id, _change in changes)
    expected_candidate_order = tuple(
        candidate_id
        for candidate_id in config.neighbor_candidate_order
        if candidate_id != config.baseline_arm_id
    )
    if candidate_order != expected_candidate_order:
        _invalid("redesign candidate order differs from the numeric-neighbor registry")

    rows = tuple(reports)
    expected_identities = tuple(
        (candidate_id, asset, fold_id, cost_scenario)
        for candidate_id in candidate_order
        for cost_scenario in (config.expected_cost_scenario, config.stressed_cost_scenario)
        for fold_id, _year in config.fold_years
        for asset in config.assets
    )
    identities = tuple((row.arm_id, row.asset, row.fold_id, row.cost_scenario) for row in rows)
    if len(set(identities)) != len(identities):
        _invalid("redesign report identities must be unique")
    if identities != expected_identities:
        missing = len(set(expected_identities) - set(identities))
        extra = len(set(identities) - set(expected_identities))
        _invalid(
            "redesign reports must exactly cover and order every registered candidate, "
            "cost scenario, fold, and asset "
            f"(missing={missing}, extra={extra})"
        )
    if len({row.trial_id for row in rows}) != len(rows):
        _invalid("redesign source trial IDs must be unique")
    if len({row.timeframe for row in rows}) != 1:
        _invalid("redesign reports must share one timeframe")
    by_identity = {(row.arm_id, row.asset, row.fold_id, row.cost_scenario): row for row in rows}
    fold_year = dict(config.fold_years)

    benchmark_rows = tuple(expected_cost_benchmarks)
    if tuple(row.benchmark_id for row in benchmark_rows) != config.benchmark_order:
        _invalid("redesign benchmark evidence differs from the frozen registered order")
    if len({row.source_trial_id for row in benchmark_rows}) != len(benchmark_rows):
        _invalid("redesign benchmark source trial IDs must be unique")
    if any(
        row.fold_id != "pooled-oos" or row.cost_scenario != config.expected_cost_scenario
        for row in benchmark_rows
    ):
        _invalid("redesign benchmarks must be pooled OOS under expected costs")

    output: list[RegisteredRedesignCandidateEvidence] = []
    for candidate_id, registered_change in changes:

        def portfolio(
            cost_scenario: str,
            fold_id: str,
            candidate_arm_id: str = candidate_id,
        ) -> _PortfolioFoldEvidence:
            return _fold_portfolio(
                tuple(
                    by_identity[(candidate_arm_id, asset, fold_id, cost_scenario)]
                    for asset in config.assets
                ),
                assets=config.assets,
                fold_id=fold_id,
                expected_year=fold_year[fold_id],
            )

        expected_by_fold = {
            fold_id: portfolio(config.expected_cost_scenario, fold_id)
            for fold_id, _year in config.fold_years
        }
        stressed_by_fold = {
            fold_id: portfolio(config.stressed_cost_scenario, fold_id)
            for fold_id, _year in config.fold_years
        }
        expected_returns = tuple(
            value
            for fold_id, _year in config.fold_years
            for value in expected_by_fold[fold_id].portfolio_returns
        )
        stressed_returns = tuple(
            value
            for fold_id, _year in config.fold_years
            for value in stressed_by_fold[fold_id].portfolio_returns
        )
        expected_timestamps = tuple(
            timestamp
            for fold_id, _year in config.fold_years
            for timestamp in expected_by_fold[fold_id].timestamps
        )
        stressed_timestamps = tuple(
            timestamp
            for fold_id, _year in config.fold_years
            for timestamp in stressed_by_fold[fold_id].timestamps
        )
        if expected_timestamps != stressed_timestamps:
            _invalid(f"{candidate_id} expected and stressed decision grids do not align")
        if any(
            tuple(item.timestamp for item in benchmark.daily_returns) != expected_timestamps
            for benchmark in benchmark_rows
        ):
            _invalid(f"{candidate_id} benchmark and expected OOS timestamps do not align")

        expected_series = tuple(
            TimestampedDailyReturn(timestamp, value)
            for timestamp, value in zip(expected_timestamps, expected_returns, strict=True)
        )
        stressed_series = tuple(
            TimestampedDailyReturn(timestamp, value)
            for timestamp, value in zip(stressed_timestamps, stressed_returns, strict=True)
        )
        fold_returns = tuple(
            NamedReturn(
                fold_id,
                _compounded_return_pct(expected_by_fold[fold_id].portfolio_returns),
            )
            for fold_id, _year in config.fold_years
        )
        asset_returns = tuple(
            NamedReturn(
                asset,
                _compounded_return_pct(
                    tuple(
                        value
                        for fold_id, _year in config.fold_years
                        for observed_asset, values in expected_by_fold[fold_id].returns_by_asset
                        if observed_asset == asset
                        for value in values
                    )
                ),
            )
            for asset in config.assets
        )
        positivity = summarize_fold_asset_positivity(fold_returns, asset_returns)
        attributed, attribution = _build_position_attribution(
            expected_by_fold,
            config=config,
        )
        closed_trades = attribution.closed_trade_contributions
        concentration_summary = (
            summarize_contribution_robustness(
                closed_trades,
                attributed_contributions=attributed,
            )
            if closed_trades
            else None
        )
        concentration = ContributionRobustnessAvailability(
            available=concentration_summary is not None,
            unavailable_reason=(
                None if concentration_summary is not None else "NOT_APPLICABLE_DUE_TO_ZERO_TRADES"
            ),
            summary=concentration_summary,
            attribution_reconciliation=attribution,
        )
        regime_contributions = _summarize_regime_contributions(
            economic_regimes,
            config=config,
            attributed_contributions=attributed,
        )

        expected_total_return = _compounded_return_pct(expected_returns)
        stressed_total_return = _compounded_return_pct(stressed_returns)
        expected_reports = tuple(
            by_identity[
                (
                    candidate_id,
                    asset,
                    fold_id,
                    config.expected_cost_scenario,
                )
            ]
            for fold_id, _year in config.fold_years
            for asset in config.assets
        )
        weight = 1.0 / len(config.assets)
        gross_pnl = math.fsum(weight * row.gross_pnl for row in expected_reports)
        execution_cost = math.fsum(
            weight * (row.aggregate_execution_cost + row.terminal_exit_cost)
            for row in expected_reports
        )
        turnover = math.fsum(
            weight * (row.turnover_notional + row.terminal_exit_reference_notional)
            for row in expected_reports
        )
        break_even_cost = max(0.0, gross_pnl / turnover * 10_000.0) if turnover > 0.0 else 0.0
        expected_cost = execution_cost / turnover * 10_000.0 if turnover > 0.0 else 0.0
        cost_tolerance = summarize_cost_tolerance(
            expected_return_pct=expected_total_return,
            stressed_return_pct=stressed_total_return,
            break_even_cost_per_traded_notional_bps=break_even_cost,
            expected_cost_per_traded_notional_bps=expected_cost,
            stressed_edge_reversal_limit_fraction=(config.stressed_edge_reversal_limit_fraction),
        )
        absolute_risk = _summarize_absolute_risk(
            expected_returns=expected_returns,
            stressed_returns=stressed_returns,
            economic_timestamps=expected_timestamps,
            attribution=attribution,
            config=config,
        )
        expected_sortino = sortino_ratio(
            expected_returns,
            annualization_factor=config.annualization_factor,
        )
        expected_exposure = math.fsum(row.exposure_pct for row in expected_reports) / len(
            expected_reports
        )
        strategy_point = PerformancePoint(
            candidate_id,
            expected_total_return,
            absolute_risk.expected_pooled_max_drawdown_pct,
            expected_sortino,
            expected_exposure,
        )
        benchmark_points = tuple(
            PerformancePoint(
                benchmark.benchmark_id,
                _compounded_return_pct(
                    tuple(item.daily_return for item in benchmark.daily_returns)
                ),
                max_drawdown_pct(
                    _equity_from_returns(
                        tuple(item.daily_return for item in benchmark.daily_returns)
                    )
                ),
                sortino_ratio(
                    tuple(item.daily_return for item in benchmark.daily_returns),
                    annualization_factor=config.annualization_factor,
                ),
                benchmark.average_exposure_pct,
            )
            for benchmark in benchmark_rows
        )
        raw_pareto = compare_raw_registered_benchmarks(
            strategy_point,
            benchmark_points,
        )

        contribution_ex_trade = bool(
            concentration_summary is not None
            and concentration_summary.best_closed_trade.contribution_after_removal > 0.0
        )
        contribution_ex_year = bool(
            concentration_summary is not None
            and concentration_summary.best_calendar_year.contribution_after_removal > 0.0
        )
        contribution_share_met = bool(
            concentration_summary is not None
            and concentration_summary.maximum_asset_or_year_positive_share
            <= config.maximum_positive_contribution_share
        )
        gate_checks = {
            "pooled_expected_cost_oos_return_positive": expected_total_return > 0.0,
            "all_registered_assets_positive": positivity.all_assets_positive,
            "minimum_positive_expected_cost_oos_folds_met": (
                len(positivity.positive_fold_ids) >= config.minimum_positive_selected_folds
            ),
            "contribution_ex_best_closed_trade_positive": contribution_ex_trade,
            "contribution_ex_best_calendar_year_positive": contribution_ex_year,
            "contribution_share_requirement_met": contribution_share_met,
            "cost_tolerance_requirements_met": cost_tolerance.requirements_met,
            "benchmark_pareto_not_dominated": not raw_pareto.any_benchmark_dominates,
            "expected_pooled_max_drawdown_within_limit": (
                absolute_risk.expected_drawdown_requirement_met
            ),
            "expected_pooled_recovery_duration_within_limit": (
                absolute_risk.recovery_duration_requirement_met
            ),
            "expected_daily_es95_within_limit": (absolute_risk.expected_daily_es95_requirement_met),
            "stressed_pooled_max_drawdown_within_limit": (
                absolute_risk.stressed_drawdown_requirement_met
            ),
            "worst_closed_or_terminal_contribution_loss_within_limit": (
                absolute_risk.position_loss_requirement_met
            ),
            "single_registered_economically_defensible_change_confirmed": True,
        }
        blockers = tuple(gate_id for gate_id, passed in gate_checks.items() if not passed)
        raw_zero = not closed_trades or expected_exposure <= 0.0
        if raw_zero:
            blockers = ("zero_completed_trades_or_exposure", *blockers)
        candidate_reports = tuple(
            by_identity[(candidate_id, asset, fold_id, cost_scenario)]
            for cost_scenario in (
                config.expected_cost_scenario,
                config.stressed_cost_scenario,
            )
            for fold_id, _year in config.fold_years
            for asset in config.assets
        )
        output.append(
            RegisteredRedesignCandidateEvidence(
                candidate_id=candidate_id,
                registered_change=registered_change,
                source_reports=tuple(row.source_dict() for row in candidate_reports),
                source_benchmarks=tuple(row.source_dict() for row in benchmark_rows),
                expected_pooled_daily_returns=expected_series,
                stressed_pooled_daily_returns=stressed_series,
                expected_total_return_pct=expected_total_return,
                stressed_total_return_pct=stressed_total_return,
                expected_sharpe_ratio=sharpe_ratio(
                    expected_returns,
                    annualization_factor=config.annualization_factor,
                ),
                expected_sortino_ratio=expected_sortino,
                expected_exposure_pct=expected_exposure,
                positivity=positivity,
                contribution_robustness=concentration,
                cost_tolerance=cost_tolerance,
                benchmark_raw_capital_pareto=raw_pareto,
                absolute_risk=absolute_risk,
                regime_contributions=regime_contributions,
                gate_checks=gate_checks,
                blocking_gate_ids=blockers,
                raw_zero_trades_or_exposure=raw_zero,
                quantitative_candidate_gate_eligible=not blockers,
            )
        )

    return RegisteredRedesignCandidateEvidenceSet(
        registered_candidate_order=candidate_order,
        candidates=tuple(output),
    )


@dataclass(frozen=True, slots=True)
class RegisteredReportCSCVPBO:
    """CSCV/PBO plus exact immutable report sources and date semantics."""

    source_reports: tuple[dict[str, object], ...]
    economic_date_rule: str
    result: CalendarYearCSCVPBOResult
    evidence_role: str = field(default="retrospective_calendar_block_cscv_pbo", init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "source_reports": list(self.source_reports),
            "economic_date_rule": self.economic_date_rule,
            "result": self.result.to_dict(),
            "evidence_role": self.evidence_role,
        }


def build_registered_report_cscv_pbo(  # noqa: PLR0912, PLR0915
    reports: Sequence[StandardReportEvidence],
    *,
    registered_candidate_order: Sequence[str],
    registered_asset_order: Sequence[str],
    calendar_years: Sequence[int],
    registered_economic_dates: Sequence[date],
    annualization_factor: float,
    expected_cost_scenario: str,
    economic_period_date_rule: str,
) -> RegisteredReportCSCVPBO:
    """Build registered expected-cost calendar-block CSCV from full reports.

    Decision timestamps are period-end timestamps.  The frozen economic date is
    therefore exactly one calendar day earlier; no timezone or trading-calendar
    heuristic is permitted. Every date in the frozen registered common calendar
    must be present for every candidate and asset.
    """

    candidates = tuple(registered_candidate_order)
    assets = tuple(registered_asset_order)
    years = tuple(calendar_years)
    rows = tuple(reports)
    if economic_period_date_rule != _ECONOMIC_PERIOD_DATE_RULE:
        _invalid("CSCV economic period date rule differs from close-minus-one-calendar-day")
    if len(candidates) < _MIN_PBO_CANDIDATES:
        _invalid("registered CSCV requires at least two candidates")
    if any(not isinstance(candidate, str) or not candidate.strip() for candidate in candidates):
        _invalid("registered CSCV candidate identities must be non-empty strings")
    if len(set(candidates)) != len(candidates):
        _invalid("registered CSCV candidate identities must be unique")
    if not assets:
        _invalid("registered CSCV asset order must not be empty")
    if any(not isinstance(asset, str) or not asset.strip() for asset in assets):
        _invalid("registered CSCV asset identities must be non-empty strings")
    if len(set(assets)) != len(assets):
        _invalid("registered CSCV asset identities must be unique")
    if any(isinstance(year, bool) or not isinstance(year, int) for year in years):
        _invalid("registered CSCV calendar years must be integers")
    if len(years) != _EXPECTED_CSCV_YEARS or len(set(years)) != len(years):
        _invalid("registered CSCV calendar years must contain exactly six unique years")
    if any(not 1 <= year < date.max.year for year in years):
        _invalid("registered CSCV calendar years exceed the supported date range")
    if years != tuple(range(years[0], years[0] + _EXPECTED_CSCV_YEARS)):
        _invalid("registered CSCV calendar years must be six strictly consecutive years")
    _require_finite(annualization_factor, field_name="CSCV annualization_factor")
    if annualization_factor <= 0.0:
        _invalid("CSCV annualization_factor must be positive")
    _require_nonempty(expected_cost_scenario, field_name="CSCV expected_cost_scenario")
    expected = tuple(
        (candidate, asset, "full-panel", expected_cost_scenario)
        for candidate in candidates
        for asset in assets
    )
    identities = tuple((row.arm_id, row.asset, row.fold_id, row.cost_scenario) for row in rows)
    if len(set(identities)) != len(identities):
        _invalid("CSCV report identities must be unique")
    if identities != expected:
        missing = len(set(expected) - set(identities))
        extra = len(set(identities) - set(expected))
        _invalid(
            "CSCV reports must exactly cover and order every registered candidate and asset "
            f"(missing={missing}, extra={extra})"
        )
    if len({row.trial_id for row in rows}) != len(rows):
        _invalid("CSCV source trial IDs must be unique")
    if len({row.timeframe for row in rows}) != 1:
        _invalid("CSCV reports must share one timeframe")
    if any(row.terminal_exit_cost != 0.0 for row in rows):
        _invalid("full-panel CSCV uses marked terminal equity and no hypothetical exit adjustment")

    by_identity = {(row.arm_id, row.asset): row for row in rows}
    series: list[CandidateDailyReturnSeries] = []
    global_decision_grid: tuple[datetime, ...] | None = None
    expected_dates = tuple(registered_economic_dates)
    if not expected_dates:
        _invalid("CSCV registered_economic_dates must not be empty")
    if any(isinstance(item, datetime) or not isinstance(item, date) for item in expected_dates):
        _invalid("CSCV registered_economic_dates must contain dates")
    if any(current <= previous for previous, current in itertools.pairwise(expected_dates)):
        _invalid("CSCV registered_economic_dates must be strictly increasing and unique")
    if {item.year for item in expected_dates} != set(years):
        _invalid("CSCV registered_economic_dates must cover exactly the registered years")
    for candidate in candidates:
        candidate_rows = tuple(by_identity[(candidate, asset)] for asset in assets)
        decision_grid: tuple[datetime, ...] | None = None
        returns_by_asset: list[tuple[float, ...]] = []
        for report in candidate_rows:
            curve = report.equity_curve
            timestamps = tuple(timestamp for timestamp, _equity in curve)
            economic_dates = tuple(
                (timestamp - timedelta(days=1)).date() for timestamp in timestamps
            )
            if economic_dates != expected_dates:
                expected_count = len(expected_dates)
                _invalid(
                    f"CSCV {candidate}/{report.asset} must contain exactly "
                    f"{expected_count} registered dates"
                )
            if decision_grid is None:
                decision_grid = timestamps
            elif timestamps != decision_grid:
                _invalid(f"CSCV decision timestamps are not aligned for candidate {candidate}")
            previous = report.initial_capital
            returns: list[float] = []
            for _timestamp, equity in curve:
                daily_return = equity / previous - 1.0
                if not math.isfinite(daily_return) or daily_return <= -1.0:
                    _invalid(f"CSCV {candidate}/{report.asset} contains a ruinous return")
                returns.append(daily_return)
                previous = equity
            returns_by_asset.append(tuple(returns))
        assert decision_grid is not None
        if global_decision_grid is None:
            global_decision_grid = decision_grid
        elif decision_grid != global_decision_grid:
            _invalid("CSCV decision timestamps must align across every registered candidate")
        weight = 1.0 / len(assets)
        portfolio_returns = tuple(
            math.fsum(weight * values[index] for values in returns_by_asset)
            for index in range(len(decision_grid))
        )
        observations = tuple(
            TimestampedDailyReturn(
                datetime.combine(
                    (timestamp - timedelta(days=1)).date(),
                    datetime.min.time(),
                    tzinfo=timestamp.tzinfo,
                ),
                daily_return,
            )
            for timestamp, daily_return in zip(decision_grid, portfolio_returns, strict=True)
        )
        series.append(CandidateDailyReturnSeries(candidate, observations))
    result = build_exact_calendar_cscv_pbo(
        tuple(series),
        registered_candidate_order=candidates,
        calendar_years=years,
        annualization_factor=annualization_factor,
        registered_calendar_dates=expected_dates,
    )
    return RegisteredReportCSCVPBO(
        source_reports=tuple(row.source_dict() for row in rows),
        economic_date_rule=economic_period_date_rule,
        result=result,
    )
