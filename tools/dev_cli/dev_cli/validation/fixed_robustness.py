"""Fixed-baseline robustness evidence and shared redesign primitives."""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from dev_cli.validation._diagnostic_common import (
    ECONOMIC_PERIOD_DATE_RULE as _ECONOMIC_PERIOD_DATE_RULE,
)
from dev_cli.validation._diagnostic_common import (
    MIN_DSR_DISPERSION_VALUES as _MIN_DSR_DISPERSION_VALUES,
)
from dev_cli.validation._diagnostic_common import invalid as _invalid
from dev_cli.validation._diagnostic_common import require_finite as _require_finite
from dev_cli.validation._diagnostic_common import require_nonempty as _require_nonempty
from dev_cli.validation.backtest.metrics import (
    max_drawdown_pct,
    recovery_duration_days,
    sharpe_ratio,
    sortino_ratio,
    tail_risk_pct,
)
from dev_cli.validation.backtest.statistics import (
    block_bootstrap_confidence_interval,
    sharpe_diagnostics,
)
from dev_cli.validation.contribution_diagnostics import (
    AttributedContribution,
    ContributionRobustnessSummary,
    TerminalPositionContribution,
    TradeContribution,
    summarize_contribution_robustness,
)
from dev_cli.validation.cscv import (
    TimestampedDailyReturn,
    _complete_calendar_dates,
)
from dev_cli.validation.economic_regimes import EconomicRegimeObservation
from dev_cli.validation.performance_diagnostics import (
    CandidateOOSPerformance,
    CostToleranceSummary,
    FoldAssetPositivitySummary,
    NamedReturn,
    NeighborSupportSummary,
    PerformancePoint,
    RawBenchmarkParetoSummary,
    compare_raw_registered_benchmarks,
    summarize_cost_tolerance,
    summarize_fold_asset_positivity,
    summarize_neighbor_support,
)
from dev_cli.validation.report_evidence import StandardReportEvidence
from dev_cli.validation.robustness_models import (
    AdaptiveSelectorEvidence,
    BootstrapInferenceSummary,
    RegisteredBenchmarkEvidence,
    RegisteredOneFactorChange,
    RobustnessEvidenceConfig,
    SharpeDiagnosticAvailability,
    TrialSharpeDispersion,
    _validated_trial_sharpe_dispersion,
)


@dataclass(frozen=True, slots=True)
class RegimeContributionBucket:
    regime_id: str
    observations: int
    net_contribution: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EconomicRegimeContributionSummary:
    observations: int
    buckets: tuple[RegimeContributionBucket, ...]
    threshold_sources: tuple[str, ...]
    evidence_role: str = field(default="retrospective_descriptive_only", init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "observations": self.observations,
            "buckets": [item.to_dict() for item in self.buckets],
            "threshold_sources": list(self.threshold_sources),
            "evidence_role": self.evidence_role,
        }


@dataclass(frozen=True, slots=True)
class ContributionAttributionReconciliation:
    """Exact partition of the daily-rebalanced portfolio contribution ledger."""

    attribution_policy_id: str
    total_attributed_contribution: float
    closed_trade_attributed_contribution: float
    terminal_position_attributed_contribution: float
    unattributed_residual_contribution: float
    closed_trade_attributed_observations: int
    terminal_position_attributed_observations: int
    unattributed_observations: int
    nonzero_unattributed_contribution_ids: tuple[str, ...]
    closed_trade_contributions: tuple[TradeContribution, ...]
    terminal_position_contributions: tuple[TerminalPositionContribution, ...]
    reconciliation_error: float
    reconciled: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "attribution_policy_id": self.attribution_policy_id,
            "total_attributed_contribution": self.total_attributed_contribution,
            "closed_trade_attributed_contribution": (self.closed_trade_attributed_contribution),
            "terminal_position_attributed_contribution": (
                self.terminal_position_attributed_contribution
            ),
            "unattributed_residual_contribution": (self.unattributed_residual_contribution),
            "closed_trade_attributed_observations": (self.closed_trade_attributed_observations),
            "terminal_position_attributed_observations": (
                self.terminal_position_attributed_observations
            ),
            "unattributed_observations": self.unattributed_observations,
            "nonzero_unattributed_contribution_ids": list(
                self.nonzero_unattributed_contribution_ids
            ),
            "closed_trade_contributions": [
                item.to_dict() for item in self.closed_trade_contributions
            ],
            "terminal_position_contributions": [
                item.to_dict() for item in self.terminal_position_contributions
            ],
            "reconciliation_error": self.reconciliation_error,
            "reconciled": self.reconciled,
        }


@dataclass(frozen=True, slots=True)
class ContributionRobustnessAvailability:
    available: bool
    unavailable_reason: str | None
    summary: ContributionRobustnessSummary | None
    attribution_reconciliation: ContributionAttributionReconciliation

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "summary": self.summary.to_dict() if self.summary is not None else None,
            "attribution_reconciliation": self.attribution_reconciliation.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class AbsoluteRiskSummary:
    """Pre-registered actual-capital tail and drawdown freeze gates."""

    expected_pooled_max_drawdown_pct: float
    expected_pooled_recovery_duration_days: float
    expected_daily_es95_pct: float
    stressed_pooled_max_drawdown_pct: float
    worst_closed_or_terminal_contribution_loss_pct: float
    worst_position_id: str | None
    worst_position_kind: str | None
    maximum_expected_pooled_drawdown_pct: float
    maximum_expected_recovery_duration_days: float
    maximum_expected_daily_es95_pct: float
    maximum_stressed_pooled_drawdown_pct: float
    maximum_closed_or_terminal_contribution_loss_pct: float
    expected_drawdown_requirement_met: bool
    recovery_duration_requirement_met: bool
    expected_daily_es95_requirement_met: bool
    stressed_drawdown_requirement_met: bool
    position_loss_requirement_met: bool
    requirements_met: bool
    evidence_role: str = field(default="retrospective_absolute_risk_gate", init=False)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FixedBaselineRobustnessEvidence:
    """Complete fixed-version robustness payload; adaptive evidence cannot rescue it."""

    disposition_source_arm_id: str
    economic_period_date_rule: str
    benchmark_comparator_contract: dict[str, object]
    source_reports: tuple[dict[str, object], ...]
    source_benchmarks: tuple[dict[str, object], ...]
    expected_pooled_daily_returns: tuple[TimestampedDailyReturn, ...]
    stressed_pooled_daily_returns: tuple[TimestampedDailyReturn, ...]
    expected_total_return_pct: float
    stressed_total_return_pct: float
    expected_sharpe_ratio: float
    expected_sortino_ratio: float
    expected_max_drawdown_pct: float
    expected_exposure_pct: float
    positivity: FoldAssetPositivitySummary
    contribution_robustness: ContributionRobustnessAvailability
    absolute_risk: AbsoluteRiskSummary
    neighbor_support: NeighborSupportSummary
    cost_tolerance: CostToleranceSummary
    benchmark_raw_capital_pareto: RawBenchmarkParetoSummary
    regime_contributions: EconomicRegimeContributionSummary
    bootstrap_inference: BootstrapInferenceSummary
    sharpe_diagnostics: SharpeDiagnosticAvailability
    contribution_share_requirement_met: bool
    contribution_ex_best_closed_trade_positive: bool
    contribution_ex_best_calendar_year_positive: bool
    quantitative_gate_checks: dict[str, bool]
    raw_baseline_zero_trades_or_exposure: bool
    quantitative_robustness_gate_eligible: bool
    blocking_quantitative_gate_ids: tuple[str, ...]
    evidence_complete: bool
    missing_evidence_ids: tuple[str, ...]
    completion_conclusion: str
    adaptive_selector_diagnostic: dict[str, object] | None
    adaptive_selector_can_rescue_baseline: bool = field(default=False, init=False)
    evidence_role: str = field(
        default="retrospective_fixed_version_robustness_diagnostic", init=False
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "disposition_source_arm_id": self.disposition_source_arm_id,
            "economic_period_date_rule": self.economic_period_date_rule,
            "benchmark_comparator_contract": self.benchmark_comparator_contract,
            "source_reports": list(self.source_reports),
            "source_benchmarks": list(self.source_benchmarks),
            "expected_pooled_daily_returns": [
                item.to_dict() for item in self.expected_pooled_daily_returns
            ],
            "stressed_pooled_daily_returns": [
                item.to_dict() for item in self.stressed_pooled_daily_returns
            ],
            "expected_total_return_pct": self.expected_total_return_pct,
            "stressed_total_return_pct": self.stressed_total_return_pct,
            "expected_sharpe_ratio": self.expected_sharpe_ratio,
            "expected_sortino_ratio": self.expected_sortino_ratio,
            "expected_max_drawdown_pct": self.expected_max_drawdown_pct,
            "expected_exposure_pct": self.expected_exposure_pct,
            "positivity": self.positivity.to_dict(),
            "contribution_robustness": self.contribution_robustness.to_dict(),
            "absolute_risk": self.absolute_risk.to_dict(),
            "neighbor_support": self.neighbor_support.to_dict(),
            "cost_tolerance": self.cost_tolerance.to_dict(),
            "benchmark_raw_capital_pareto": self.benchmark_raw_capital_pareto.to_dict(),
            "regime_contributions": self.regime_contributions.to_dict(),
            "bootstrap_inference": self.bootstrap_inference.to_dict(),
            "sharpe_diagnostics": self.sharpe_diagnostics.to_dict(),
            "contribution_share_requirement_met": self.contribution_share_requirement_met,
            "contribution_ex_best_closed_trade_positive": (
                self.contribution_ex_best_closed_trade_positive
            ),
            "contribution_ex_best_calendar_year_positive": (
                self.contribution_ex_best_calendar_year_positive
            ),
            "quantitative_gate_checks": dict(self.quantitative_gate_checks),
            "raw_baseline_zero_trades_or_exposure": self.raw_baseline_zero_trades_or_exposure,
            "quantitative_robustness_gate_eligible": self.quantitative_robustness_gate_eligible,
            "blocking_quantitative_gate_ids": list(self.blocking_quantitative_gate_ids),
            "evidence_complete": self.evidence_complete,
            "missing_evidence_ids": list(self.missing_evidence_ids),
            "completion_conclusion": self.completion_conclusion,
            "adaptive_selector_diagnostic": self.adaptive_selector_diagnostic,
            "adaptive_selector_can_rescue_baseline": self.adaptive_selector_can_rescue_baseline,
            "evidence_role": self.evidence_role,
        }


@dataclass(frozen=True, slots=True)
class RegisteredRedesignCandidateEvidence:
    """Pure quantitative evidence for one registered redesign candidate."""

    candidate_id: str
    registered_change: RegisteredOneFactorChange
    source_reports: tuple[dict[str, object], ...]
    source_benchmarks: tuple[dict[str, object], ...]
    expected_pooled_daily_returns: tuple[TimestampedDailyReturn, ...]
    stressed_pooled_daily_returns: tuple[TimestampedDailyReturn, ...]
    expected_total_return_pct: float
    stressed_total_return_pct: float
    expected_sharpe_ratio: float
    expected_sortino_ratio: float
    expected_exposure_pct: float
    positivity: FoldAssetPositivitySummary
    contribution_robustness: ContributionRobustnessAvailability
    cost_tolerance: CostToleranceSummary
    benchmark_raw_capital_pareto: RawBenchmarkParetoSummary
    absolute_risk: AbsoluteRiskSummary
    regime_contributions: EconomicRegimeContributionSummary
    gate_checks: dict[str, bool]
    blocking_gate_ids: tuple[str, ...]
    raw_zero_trades_or_exposure: bool
    quantitative_candidate_gate_eligible: bool
    evidence_role: str = field(
        default="retrospective_registered_redesign_candidate_diagnostic",
        init=False,
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "registered_change": self.registered_change.to_dict(),
            "source_reports": list(self.source_reports),
            "source_benchmarks": list(self.source_benchmarks),
            "expected_pooled_daily_returns": [
                item.to_dict() for item in self.expected_pooled_daily_returns
            ],
            "stressed_pooled_daily_returns": [
                item.to_dict() for item in self.stressed_pooled_daily_returns
            ],
            "expected_total_return_pct": self.expected_total_return_pct,
            "stressed_total_return_pct": self.stressed_total_return_pct,
            "expected_sharpe_ratio": self.expected_sharpe_ratio,
            "expected_sortino_ratio": self.expected_sortino_ratio,
            "expected_exposure_pct": self.expected_exposure_pct,
            "positivity": self.positivity.to_dict(),
            "contribution_robustness": self.contribution_robustness.to_dict(),
            "cost_tolerance": self.cost_tolerance.to_dict(),
            "benchmark_raw_capital_pareto": self.benchmark_raw_capital_pareto.to_dict(),
            "absolute_risk": self.absolute_risk.to_dict(),
            "regime_contributions": self.regime_contributions.to_dict(),
            "gate_checks": dict(self.gate_checks),
            "blocking_gate_ids": list(self.blocking_gate_ids),
            "raw_zero_trades_or_exposure": self.raw_zero_trades_or_exposure,
            "quantitative_candidate_gate_eligible": (self.quantitative_candidate_gate_eligible),
            "evidence_role": self.evidence_role,
        }


@dataclass(frozen=True, slots=True)
class RegisteredRedesignCandidateEvidenceSet:
    """Complete ordered evidence for every quantitative redesign arm."""

    registered_candidate_order: tuple[str, ...]
    candidates: tuple[RegisteredRedesignCandidateEvidence, ...]
    no_selection_performed: bool = field(default=True, init=False)
    evidence_role: str = field(
        default="retrospective_registered_redesign_candidate_set",
        init=False,
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "registered_candidate_order": list(self.registered_candidate_order),
            "candidates": [item.to_dict() for item in self.candidates],
            "no_selection_performed": self.no_selection_performed,
            "evidence_role": self.evidence_role,
        }


@dataclass(frozen=True, slots=True)
class _PortfolioFoldEvidence:
    timestamps: tuple[datetime, ...]
    returns_by_asset: tuple[tuple[str, tuple[float, ...]], ...]
    portfolio_returns: tuple[float, ...]
    records: tuple[StandardReportEvidence, ...]


def _compounded_return_pct(returns: Sequence[float]) -> float:
    growth = 1.0
    for value in returns:
        if not math.isfinite(value) or value <= -1.0:
            _invalid("daily returns must be finite and greater than -1")
        growth *= 1.0 + value
    return (growth - 1.0) * 100.0


def _equity_from_returns(returns: Sequence[float]) -> tuple[float, ...]:
    growth = 1.0
    values = [growth]
    for value in returns:
        growth *= 1.0 + value
        if not math.isfinite(growth) or growth <= 0.0:
            _invalid("daily return series ruins the portfolio")
        values.append(growth)
    return tuple(values)


def _fold_portfolio(
    records: Sequence[StandardReportEvidence],
    *,
    assets: tuple[str, ...],
    fold_id: str,
    expected_year: int,
) -> _PortfolioFoldEvidence:
    by_asset = {record.asset: record for record in records}
    if len(by_asset) != len(records) or tuple(record.asset for record in records) != assets:
        _invalid(f"{fold_id} report evidence must follow the exact registered asset order")
    if set(by_asset) != set(assets):
        _invalid(f"{fold_id} report evidence does not exactly cover registered assets")
    expected_dates = _complete_calendar_dates((expected_year,))
    decision_grid: tuple[datetime, ...] | None = None
    asset_returns: list[tuple[str, tuple[float, ...]]] = []
    for asset in assets:
        report = by_asset[asset]
        curve = report.adjusted_equity_curve
        timestamps = tuple(timestamp for timestamp, _equity in curve)
        economic_dates = tuple((timestamp - timedelta(days=1)).date() for timestamp in timestamps)
        if economic_dates != expected_dates:
            _invalid(
                f"{fold_id}/{asset} must contain every economic date in "
                f"{expected_year} exactly once"
            )
        if decision_grid is None:
            decision_grid = timestamps
        elif timestamps != decision_grid:
            _invalid(f"{fold_id} asset decision timestamps are not exactly aligned")
        previous = report.initial_capital
        returns: list[float] = []
        for _timestamp, equity in curve:
            daily_return = equity / previous - 1.0
            if not math.isfinite(daily_return) or daily_return <= -1.0:
                _invalid(f"{fold_id}/{asset} contains a nonfinite or ruinous return")
            returns.append(daily_return)
            previous = equity
        asset_returns.append((asset, tuple(returns)))
    assert decision_grid is not None  # assets are validated as non-empty by the caller
    weight = 1.0 / len(assets)
    portfolio_returns = tuple(
        math.fsum(weight * values[index] for _asset, values in asset_returns)
        for index in range(len(decision_grid))
    )
    economic_timestamps = tuple(
        datetime.combine(
            (timestamp - timedelta(days=1)).date(),
            datetime.min.time(),
            tzinfo=timestamp.tzinfo,
        )
        for timestamp in decision_grid
    )
    return _PortfolioFoldEvidence(
        timestamps=economic_timestamps,
        returns_by_asset=tuple(asset_returns),
        portfolio_returns=portfolio_returns,
        records=tuple(records),
    )


def _validate_robustness_config(  # noqa: PLR0912, PLR0915
    config: RobustnessEvidenceConfig,
) -> None:
    if not config.assets:
        _invalid("robustness assets must not be empty")
    if len(set(config.assets)) != len(config.assets):
        _invalid("robustness asset identities must be unique")
    for asset in config.assets:
        _require_nonempty(asset, field_name="robustness asset")

    if not config.fold_years:
        _invalid("robustness folds must not be empty")
    folds = tuple(fold_id for fold_id, _year in config.fold_years)
    years = tuple(year for _fold_id, year in config.fold_years)
    if len(set(folds)) != len(folds):
        _invalid("robustness fold identities must be unique")
    for fold_id in folds:
        _require_nonempty(fold_id, field_name="robustness fold identity")
    if any(isinstance(year, bool) or not isinstance(year, int) for year in years):
        _invalid("robustness fold years must be integers")
    if any(not 1 <= year <= date.max.year for year in years):
        _invalid("robustness fold years must be valid calendar years")
    if len(set(years)) != len(years) or any(
        current <= previous for previous, current in itertools.pairwise(years)
    ):
        _invalid("robustness fold years must be unique and strictly increasing")

    _require_nonempty(config.baseline_arm_id, field_name="baseline_arm_id")
    if not config.neighbor_candidate_order:
        _invalid("neighbor_candidate_order must not be empty")
    if len(set(config.neighbor_candidate_order)) != len(config.neighbor_candidate_order):
        _invalid("neighbor candidate identities must be unique")
    for candidate_id in config.neighbor_candidate_order:
        _require_nonempty(candidate_id, field_name="neighbor candidate identity")
    if config.baseline_arm_id not in config.neighbor_candidate_order:
        _invalid("baseline_arm_id must be in neighbor_candidate_order")

    if not config.benchmark_order:
        _invalid("benchmark_order must not be empty")
    if len(set(config.benchmark_order)) != len(config.benchmark_order):
        _invalid("benchmark identities must be unique")
    for benchmark_id in config.benchmark_order:
        _require_nonempty(benchmark_id, field_name="benchmark identity")

    for field_name, text_value in (
        ("expected_cost_scenario", config.expected_cost_scenario),
        ("stressed_cost_scenario", config.stressed_cost_scenario),
        ("benchmark_comparator_id", config.benchmark_comparator_id),
    ):
        _require_nonempty(text_value, field_name=field_name)
    if config.expected_cost_scenario == config.stressed_cost_scenario:
        _invalid("expected and stressed cost scenarios must be distinct")
    if config.economic_period_date_rule != _ECONOMIC_PERIOD_DATE_RULE:
        _invalid("economic period date rule differs from close-minus-one-calendar-day")

    if config.registered_redesign_changes:
        registered_change_ids = tuple(
            candidate_id for candidate_id, _change in config.registered_redesign_changes
        )
        if len(set(registered_change_ids)) != len(registered_change_ids):
            _invalid("registered redesign candidate identities must be unique")
        for candidate_id, change in config.registered_redesign_changes:
            _require_nonempty(candidate_id, field_name="registered redesign candidate identity")
            if not isinstance(change, RegisteredOneFactorChange):
                _invalid("registered redesign changes must use RegisteredOneFactorChange")
        expected_change_ids = tuple(
            candidate_id
            for candidate_id in config.neighbor_candidate_order
            if candidate_id != config.baseline_arm_id
        )
        if registered_change_ids != expected_change_ids:
            _invalid(
                "registered redesign changes must exactly follow all non-baseline "
                "neighbor candidates"
            )

    for name, numeric_value in (
        ("annualization_factor", config.annualization_factor),
        ("deflated_sharpe_effective_trials", config.deflated_sharpe_effective_trials),
        (
            "stressed_edge_reversal_limit_fraction",
            config.stressed_edge_reversal_limit_fraction,
        ),
        ("maximum_positive_contribution_share", config.maximum_positive_contribution_share),
        (
            "maximum_expected_pooled_drawdown_pct",
            config.maximum_expected_pooled_drawdown_pct,
        ),
        (
            "maximum_expected_recovery_duration_days",
            config.maximum_expected_recovery_duration_days,
        ),
        ("maximum_expected_daily_es95_pct", config.maximum_expected_daily_es95_pct),
        (
            "maximum_stressed_pooled_drawdown_pct",
            config.maximum_stressed_pooled_drawdown_pct,
        ),
        (
            "maximum_closed_or_terminal_contribution_loss_pct",
            config.maximum_closed_or_terminal_contribution_loss_pct,
        ),
    ):
        _require_finite(numeric_value, field_name=name)
    if config.annualization_factor <= 0.0:
        _invalid("annualization must be positive")
    if config.deflated_sharpe_effective_trials < 1.0:
        _invalid("deflated_sharpe_effective_trials must be at least one")
    dsr_counts = (
        config.deflated_sharpe_upstream_attempted_trials,
        config.deflated_sharpe_upstream_finite_count,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in dsr_counts):
        _invalid("DSR upstream trial counts must be integers")
    if any(value < 0 for value in dsr_counts):
        _invalid("DSR upstream trial counts must be non-negative")
    if (
        config.deflated_sharpe_upstream_finite_count
        > config.deflated_sharpe_upstream_attempted_trials
    ):
        _invalid("DSR upstream finite count cannot exceed attempted trials")
    current_candidate_order = config.deflated_sharpe_current_candidate_order
    if not current_candidate_order:
        _invalid("DSR current candidate order must not be empty")
    if len(set(current_candidate_order)) != len(current_candidate_order):
        _invalid("DSR current candidate identities must be unique")
    for candidate_id in current_candidate_order:
        _require_nonempty(candidate_id, field_name="DSR current candidate identity")
    if not set(config.neighbor_candidate_order).issubset(current_candidate_order):
        _invalid("DSR current candidates must include every registered neighbor candidate")
    registered_effective_trials = float(
        config.deflated_sharpe_upstream_attempted_trials + len(current_candidate_order)
    )
    if config.deflated_sharpe_effective_trials != registered_effective_trials:
        _invalid("DSR effective trials must equal upstream attempts plus current candidates")
    if (
        config.deflated_sharpe_upstream_finite_count + len(current_candidate_order)
        < _MIN_DSR_DISPERSION_VALUES
    ):
        _invalid("DSR registry must provide at least two finite dispersion values")
    if not 0.0 < config.confidence_level < 1.0:
        _invalid("confidence_level must be in (0, 1)")
    count_fields = (
        config.bootstrap_block_bars,
        config.bootstrap_samples,
        config.minimum_positive_selected_folds,
        config.minimum_positive_pooled_neighbors,
        config.minimum_positive_candidate_fold_cells_total,
        config.minimum_training_selected_folds_supporting_baseline,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in count_fields):
        _invalid("robustness count and bootstrap settings must be integers")
    if config.bootstrap_block_bars <= 0 or config.bootstrap_samples <= 0:
        _invalid("bootstrap block bars and samples must be positive")
    if isinstance(config.bootstrap_seed, bool) or not isinstance(config.bootstrap_seed, int):
        _invalid("bootstrap_seed must be an integer")
    if any(value < 0 for value in count_fields[2:]):
        _invalid("robustness minimum counts must be non-negative")
    non_baseline_neighbors = len(config.neighbor_candidate_order) - 1
    maximum_fold_cells = len(config.neighbor_candidate_order) * len(config.fold_years)
    if config.minimum_positive_selected_folds > len(config.fold_years):
        _invalid("minimum positive selected folds cannot exceed the registered fold count")
    if config.minimum_positive_pooled_neighbors > non_baseline_neighbors:
        _invalid("minimum positive pooled neighbors cannot exceed the neighbor count")
    if config.minimum_positive_candidate_fold_cells_total > maximum_fold_cells:
        _invalid("minimum positive candidate-fold cells cannot exceed registered cells")
    if (
        not 0
        <= config.minimum_training_selected_folds_supporting_baseline
        <= len(config.fold_years)
    ):
        _invalid("minimum training-selected baseline support must be within the fold count")
    if not 0.0 <= config.maximum_positive_contribution_share <= 1.0:
        _invalid("maximum_positive_contribution_share must be in [0, 1]")
    if config.stressed_edge_reversal_limit_fraction < 0.0:
        _invalid("stressed_edge_reversal_limit_fraction must be non-negative")
    absolute_risk_limits = (
        config.maximum_expected_pooled_drawdown_pct,
        config.maximum_expected_recovery_duration_days,
        config.maximum_expected_daily_es95_pct,
        config.maximum_stressed_pooled_drawdown_pct,
        config.maximum_closed_or_terminal_contribution_loss_pct,
    )
    if any(value < 0.0 for value in absolute_risk_limits):
        _invalid("absolute risk limits must be non-negative")


def _expected_robustness_identities(
    config: RobustnessEvidenceConfig,
) -> tuple[tuple[str, str, str, str], ...]:
    expected = tuple(
        (arm, asset, fold, config.expected_cost_scenario)
        for arm in config.neighbor_candidate_order
        for fold, _year in config.fold_years
        for asset in config.assets
    )
    stressed = tuple(
        (config.baseline_arm_id, asset, fold, config.stressed_cost_scenario)
        for fold, _year in config.fold_years
        for asset in config.assets
    )
    return (*expected, *stressed)


def _summarize_regime_contributions(
    observations: Sequence[EconomicRegimeObservation],
    *,
    config: RobustnessEvidenceConfig,
    attributed_contributions: Sequence[AttributedContribution],
) -> EconomicRegimeContributionSummary:
    rows = tuple(observations)
    expected_identities = tuple(
        (asset, fold_id, day)
        for fold_id, year in config.fold_years
        for asset in config.assets
        for day in _complete_calendar_dates((year,))
    )
    identities = tuple((row.asset, row.fold_id, row.economic_period_date) for row in rows)
    if identities != expected_identities:
        missing = len(set(expected_identities) - set(identities))
        extra = len(set(identities) - set(expected_identities))
        _invalid(
            "economic regime observations must exactly cover and order every asset/fold/date "
            f"(missing={missing}, extra={extra})"
        )
    contribution_by_identity = {
        (row.asset, row.timestamp.date()): row.net_contribution for row in attributed_contributions
    }
    if len(contribution_by_identity) != len(attributed_contributions):
        _invalid("attributed contribution asset/economic-date identities must be unique")
    expected_contribution_keys = {
        (asset, day)
        for _fold_id, year in config.fold_years
        for asset in config.assets
        for day in _complete_calendar_dates((year,))
    }
    if set(contribution_by_identity) != expected_contribution_keys:
        _invalid("regime observations do not align with exact attributed contributions")
    grouped: dict[str, list[float]] = {}
    for row in rows:
        grouped.setdefault(row.regime_id, []).append(
            contribution_by_identity[(row.asset, row.economic_period_date)]
        )
    buckets = tuple(
        RegimeContributionBucket(
            regime_id=regime_id,
            observations=len(grouped[regime_id]),
            net_contribution=math.fsum(grouped[regime_id]),
        )
        for regime_id in sorted(grouped)
    )
    return EconomicRegimeContributionSummary(
        observations=len(rows),
        buckets=buckets,
        threshold_sources=tuple(sorted({row.threshold_source_id for row in rows})),
    )


def _reconcile_position_contributions(  # noqa: PLR0912, PLR0915
    report_contributions: Sequence[
        tuple[StandardReportEvidence, tuple[AttributedContribution, ...]]
    ],
) -> ContributionAttributionReconciliation:
    """Partition exact daily portfolio increments over inclusive fill dates.

    Entry and exit fills occur at a period's open while the corresponding
    contribution is observed at that economic period's close.  The exit date is
    therefore included so its gap and exit costs remain with the closing trade.
    A terminal position owns contributions from its entry through the final
    adjusted mark.  Every other observation is retained as an explicit residual.
    """

    trade_rows: list[TradeContribution] = []
    terminal_rows: list[TerminalPositionContribution] = []
    residual_values: list[float] = []
    all_values: list[float] = []
    closed_observations = 0
    terminal_observations = 0
    residual_observations = 0
    nonzero_residual_ids: list[str] = []

    for report, contributions in report_contributions:
        if any(item.asset != report.asset for item in contributions):
            _invalid("report contribution asset differs from its source report")
        if report.terminal_position_open != (report.terminal_entry_ts is not None):
            _invalid("terminal position state lacks an exact entry timestamp")

        ordered_trades = tuple(sorted(report.trades, key=lambda item: item.entry_ts))
        contribution_intervals = tuple(
            (
                item.entry_ts,
                (
                    item.exit_ts - timedelta(days=1)
                    if item.exit_reason in {"stop_loss", "take_profit"}
                    else item.exit_ts
                ),
            )
            for item in ordered_trades
        )
        if any(end < start for start, end in contribution_intervals):
            _invalid("closed trade has no valid daily economic fill interval")
        if any(
            left_entry <= right_exit and right_entry <= left_exit
            for (left_entry, left_exit), (right_entry, right_exit) in itertools.combinations(
                contribution_intervals, 2
            )
        ):
            _invalid("closed trade fill intervals overlap under inclusive exit attribution")
        if report.terminal_entry_ts is not None and any(
            report.terminal_entry_ts <= interval_end
            for _interval_start, interval_end in contribution_intervals
        ):
            _invalid("terminal and closed trade fill intervals overlap")

        trade_values: list[list[float]] = [[] for _trade in ordered_trades]
        report_terminal_values: list[float] = []
        report_terminal_observations = 0
        for contribution in contributions:
            all_values.append(contribution.net_contribution)
            matching_trade_indexes = tuple(
                index
                for index, (interval_start, interval_end) in enumerate(contribution_intervals)
                if interval_start <= contribution.timestamp <= interval_end
            )
            terminal_matches = bool(
                report.terminal_entry_ts is not None
                and report.terminal_entry_ts <= contribution.timestamp
            )
            if len(matching_trade_indexes) + int(terminal_matches) > 1:
                _invalid("daily portfolio contribution maps to multiple open positions")
            if matching_trade_indexes:
                trade_values[matching_trade_indexes[0]].append(contribution.net_contribution)
                closed_observations += 1
            elif terminal_matches:
                report_terminal_values.append(contribution.net_contribution)
                terminal_observations += 1
                report_terminal_observations += 1
            else:
                residual_values.append(contribution.net_contribution)
                residual_observations += 1
                if contribution.net_contribution != 0.0:
                    nonzero_residual_ids.append(contribution.contribution_id)

        for trade, values in zip(ordered_trades, trade_values, strict=True):
            if not values:
                _invalid("closed trade has no daily portfolio contribution in its fill interval")
            trade_rows.append(
                TradeContribution(
                    trade_id=(
                        f"{report.fold_id}:{report.asset}:{trade.index}:"
                        f"{trade.entry_ts.isoformat()}:{trade.exit_ts.isoformat()}"
                    ),
                    asset=report.asset,
                    exit_ts=trade.exit_ts,
                    net_contribution=math.fsum(values),
                )
            )
        if report.terminal_position_open and report_terminal_observations == 0:
            _invalid("terminal position has no daily portfolio contribution after its fill")
        if report.terminal_entry_ts is not None:
            terminal_rows.append(
                TerminalPositionContribution(
                    position_id=(
                        f"{report.fold_id}:{report.asset}:terminal:"
                        f"{report.terminal_entry_ts.isoformat()}"
                    ),
                    asset=report.asset,
                    entry_ts=report.terminal_entry_ts,
                    final_economic_ts=contributions[-1].timestamp,
                    net_contribution=math.fsum(report_terminal_values),
                )
            )

    closed_total = math.fsum(item.net_contribution for item in trade_rows)
    terminal_total = math.fsum(item.net_contribution for item in terminal_rows)
    residual_total = math.fsum(residual_values)
    total = math.fsum(all_values)
    assigned_total = math.fsum((closed_total, terminal_total, residual_total))
    reconciliation_error = total - assigned_total
    reconciled = math.isclose(total, assigned_total, rel_tol=1e-12, abs_tol=1e-12)
    if not reconciled:
        _invalid("position-attributed contributions do not reconcile to the complete ledger")
    if len({item.trade_id for item in trade_rows}) != len(trade_rows):
        _invalid("position-attributed closed trade ids must be unique")
    if len({item.position_id for item in terminal_rows}) != len(terminal_rows):
        _invalid("position-attributed terminal position ids must be unique")
    return ContributionAttributionReconciliation(
        attribution_policy_id=("daily_rebalanced_portfolio_inclusive_entry_exit_fill_interval_v1"),
        total_attributed_contribution=total,
        closed_trade_attributed_contribution=closed_total,
        terminal_position_attributed_contribution=terminal_total,
        unattributed_residual_contribution=residual_total,
        closed_trade_attributed_observations=closed_observations,
        terminal_position_attributed_observations=terminal_observations,
        unattributed_observations=residual_observations,
        nonzero_unattributed_contribution_ids=tuple(nonzero_residual_ids),
        closed_trade_contributions=tuple(trade_rows),
        terminal_position_contributions=tuple(terminal_rows),
        reconciliation_error=reconciliation_error,
        reconciled=True,
    )


def _build_position_attribution(
    expected_by_fold: Mapping[str, _PortfolioFoldEvidence],
    *,
    config: RobustnessEvidenceConfig,
) -> tuple[
    tuple[AttributedContribution, ...],
    ContributionAttributionReconciliation,
]:
    attributed: list[AttributedContribution] = []
    report_contributions: list[
        tuple[StandardReportEvidence, tuple[AttributedContribution, ...]]
    ] = []
    pooled_growth = 1.0
    weight = 1.0 / len(config.assets)
    for fold, _year in config.fold_years:
        fold_evidence = expected_by_fold[fold]
        contributions_by_asset: dict[str, list[AttributedContribution]] = {
            asset: [] for asset in config.assets
        }
        for index, timestamp in enumerate(fold_evidence.timestamps):
            prior_growth = pooled_growth
            for asset, values in fold_evidence.returns_by_asset:
                contribution = AttributedContribution(
                    contribution_id=f"{fold}:{asset}:{timestamp.date().isoformat()}",
                    asset=asset,
                    timestamp=timestamp,
                    net_contribution=prior_growth * weight * values[index],
                )
                attributed.append(contribution)
                contributions_by_asset[asset].append(contribution)
            pooled_growth *= 1.0 + fold_evidence.portfolio_returns[index]
        for report in fold_evidence.records:
            report_contributions.append((report, tuple(contributions_by_asset[report.asset])))
    return tuple(attributed), _reconcile_position_contributions(report_contributions)


def _summarize_absolute_risk(
    *,
    expected_returns: tuple[float, ...],
    stressed_returns: tuple[float, ...],
    economic_timestamps: tuple[datetime, ...],
    attribution: ContributionAttributionReconciliation,
    config: RobustnessEvidenceConfig,
) -> AbsoluteRiskSummary:
    if not expected_returns or len(expected_returns) != len(economic_timestamps):
        _invalid("absolute risk requires aligned non-empty expected return timestamps")
    if len(stressed_returns) != len(expected_returns):
        _invalid("absolute risk expected and stressed return ledgers must align")
    expected_equity = _equity_from_returns(expected_returns)
    stressed_equity = _equity_from_returns(stressed_returns)
    expected_drawdown = max_drawdown_pct(expected_equity)
    stressed_drawdown = max_drawdown_pct(stressed_equity)
    expected_curve = tuple(
        zip(
            (economic_timestamps[0] - timedelta(days=1), *economic_timestamps),
            expected_equity,
            strict=True,
        )
    )
    recovery_days = recovery_duration_days(expected_curve)
    _expected_var, expected_es95 = tail_risk_pct(expected_returns, confidence=0.95)

    position_contributions = tuple(
        (
            item.trade_id,
            "closed_trade",
            item.net_contribution,
        )
        for item in attribution.closed_trade_contributions
    ) + tuple(
        (
            item.position_id,
            "terminal_position",
            item.net_contribution,
        )
        for item in attribution.terminal_position_contributions
    )
    worst_position = (
        min(
            enumerate(position_contributions),
            key=lambda indexed: (indexed[1][2], indexed[0]),
        )[1]
        if position_contributions
        else None
    )
    worst_loss_pct = max(0.0, -worst_position[2] * 100.0) if worst_position is not None else 0.0
    expected_drawdown_met = expected_drawdown <= config.maximum_expected_pooled_drawdown_pct
    recovery_met = recovery_days <= config.maximum_expected_recovery_duration_days
    expected_es95_met = expected_es95 <= config.maximum_expected_daily_es95_pct
    stressed_drawdown_met = stressed_drawdown <= config.maximum_stressed_pooled_drawdown_pct
    position_loss_met = worst_loss_pct <= config.maximum_closed_or_terminal_contribution_loss_pct
    return AbsoluteRiskSummary(
        expected_pooled_max_drawdown_pct=expected_drawdown,
        expected_pooled_recovery_duration_days=recovery_days,
        expected_daily_es95_pct=expected_es95,
        stressed_pooled_max_drawdown_pct=stressed_drawdown,
        worst_closed_or_terminal_contribution_loss_pct=worst_loss_pct,
        worst_position_id=worst_position[0] if worst_position is not None else None,
        worst_position_kind=worst_position[1] if worst_position is not None else None,
        maximum_expected_pooled_drawdown_pct=config.maximum_expected_pooled_drawdown_pct,
        maximum_expected_recovery_duration_days=(config.maximum_expected_recovery_duration_days),
        maximum_expected_daily_es95_pct=config.maximum_expected_daily_es95_pct,
        maximum_stressed_pooled_drawdown_pct=(config.maximum_stressed_pooled_drawdown_pct),
        maximum_closed_or_terminal_contribution_loss_pct=(
            config.maximum_closed_or_terminal_contribution_loss_pct
        ),
        expected_drawdown_requirement_met=expected_drawdown_met,
        recovery_duration_requirement_met=recovery_met,
        expected_daily_es95_requirement_met=expected_es95_met,
        stressed_drawdown_requirement_met=stressed_drawdown_met,
        position_loss_requirement_met=position_loss_met,
        requirements_met=all(
            (
                expected_drawdown_met,
                recovery_met,
                expected_es95_met,
                stressed_drawdown_met,
                position_loss_met,
            )
        ),
    )


def _adaptive_summary(
    adaptive: AdaptiveSelectorEvidence,
    *,
    config: RobustnessEvidenceConfig,
) -> dict[str, object]:
    expected_folds = tuple(fold for fold, _year in config.fold_years)
    if tuple(item.fold_id for item in adaptive.selections) != expected_folds:
        _invalid("adaptive selections do not follow the registered OOS fold order")
    if tuple(item.label for item in adaptive.fold_returns) != expected_folds:
        _invalid("adaptive fold returns do not follow the registered OOS fold order")
    if tuple(item.label for item in adaptive.asset_returns) != config.assets:
        _invalid("adaptive asset returns do not follow the registered asset order")
    observations = adaptive.pooled_daily_returns
    if not observations:
        _invalid("adaptive pooled daily returns must not be empty")
    if any(
        current.timestamp <= previous.timestamp
        for previous, current in itertools.pairwise(observations)
    ):
        _invalid("adaptive pooled daily returns must be strictly increasing")
    expected_dates = tuple(
        day for _fold_id, year in config.fold_years for day in _complete_calendar_dates((year,))
    )
    if tuple(item.timestamp.date() for item in observations) != expected_dates:
        _invalid("adaptive pooled daily returns must cover every registered OOS date")
    returns = tuple(item.daily_return for item in observations)
    baseline_supporting_folds = tuple(
        item.fold_id for item in adaptive.selections if item.baseline_supported_within_tolerance
    )
    return {
        "source_arm_id": "TRAINING_SELECTED",
        "role": "redesign_diagnostic_only",
        "can_rescue_fixed_baseline": False,
        "selections": [item.to_dict() for item in adaptive.selections],
        "baseline_supported_fold_ids": list(baseline_supporting_folds),
        "minimum_training_selected_folds_supporting_baseline": (
            config.minimum_training_selected_folds_supporting_baseline
        ),
        "baseline_training_support_requirement_met": (
            len(baseline_supporting_folds)
            >= config.minimum_training_selected_folds_supporting_baseline
        ),
        "fold_returns": [item.to_dict() for item in adaptive.fold_returns],
        "asset_returns": [item.to_dict() for item in adaptive.asset_returns],
        "pooled_observations": len(returns),
        "pooled_total_return_pct": _compounded_return_pct(returns),
        "pooled_sharpe_ratio": sharpe_ratio(
            returns, annualization_factor=config.annualization_factor
        ),
    }


def build_fixed_baseline_robustness_evidence(  # noqa: PLR0912, PLR0915
    reports: Sequence[StandardReportEvidence],
    *,
    config: RobustnessEvidenceConfig,
    expected_cost_benchmarks: Sequence[RegisteredBenchmarkEvidence],
    economic_regimes: Sequence[EconomicRegimeObservation],
    sharpe_dispersion: TrialSharpeDispersion | None,
    adaptive_selector: AdaptiveSelectorEvidence | None = None,
) -> FixedBaselineRobustnessEvidence:
    """Build fail-closed baseline disposition evidence from registered reports.

    The adaptive selector is summarized in a separate namespace and has no path
    into any fixed-baseline requirement.  Missing DSR inputs remain explicitly
    unavailable; the selected strategy's own return count is never substituted.
    """

    _validate_robustness_config(config)
    report_rows = tuple(reports)
    identities = tuple(
        (row.arm_id, row.asset, row.fold_id, row.cost_scenario) for row in report_rows
    )
    if len(set(identities)) != len(identities):
        _invalid("robustness report identities must be unique")
    expected_identities = _expected_robustness_identities(config)
    if identities != expected_identities:
        missing = len(set(expected_identities) - set(identities))
        extra = len(set(identities) - set(expected_identities))
        _invalid(
            "robustness reports must exactly cover and order baseline costs and neighbors "
            f"(missing={missing}, extra={extra})"
        )
    if len({row.trial_id for row in report_rows}) != len(report_rows):
        _invalid("robustness source trial IDs must be unique")
    if len({row.timeframe for row in report_rows}) != 1:
        _invalid("robustness reports must share one timeframe")

    by_identity = {
        (row.arm_id, row.asset, row.fold_id, row.cost_scenario): row for row in report_rows
    }
    fold_year = dict(config.fold_years)

    def portfolio(arm: str, cost: str, fold: str) -> _PortfolioFoldEvidence:
        return _fold_portfolio(
            tuple(by_identity[(arm, asset, fold, cost)] for asset in config.assets),
            assets=config.assets,
            fold_id=fold,
            expected_year=fold_year[fold],
        )

    candidate_performance: list[CandidateOOSPerformance] = []
    expected_by_fold: dict[str, _PortfolioFoldEvidence] = {}
    for arm in config.neighbor_candidate_order:
        fold_evidence = tuple(
            portfolio(arm, config.expected_cost_scenario, fold) for fold, _year in config.fold_years
        )
        if arm == config.baseline_arm_id:
            expected_by_fold = dict(
                zip((fold for fold, _year in config.fold_years), fold_evidence, strict=True)
            )
        fold_returns = tuple(
            NamedReturn(fold, _compounded_return_pct(evidence.portfolio_returns))
            for (fold, _year), evidence in zip(config.fold_years, fold_evidence, strict=True)
        )
        candidate_performance.append(
            CandidateOOSPerformance(
                candidate_id=arm,
                pooled_return_pct=_compounded_return_pct(
                    tuple(
                        value for evidence in fold_evidence for value in evidence.portfolio_returns
                    )
                ),
                fold_returns=fold_returns,
            )
        )

    stressed_by_fold = {
        fold: portfolio(config.baseline_arm_id, config.stressed_cost_scenario, fold)
        for fold, _year in config.fold_years
    }
    expected_returns = tuple(
        value
        for fold, _year in config.fold_years
        for value in expected_by_fold[fold].portfolio_returns
    )
    stressed_returns = tuple(
        value
        for fold, _year in config.fold_years
        for value in stressed_by_fold[fold].portfolio_returns
    )
    expected_timestamps = tuple(
        timestamp
        for fold, _year in config.fold_years
        for timestamp in expected_by_fold[fold].timestamps
    )
    stressed_timestamps = tuple(
        timestamp
        for fold, _year in config.fold_years
        for timestamp in stressed_by_fold[fold].timestamps
    )
    if expected_timestamps != stressed_timestamps:
        _invalid("expected and stressed baseline decision grids do not exactly align")
    expected_series = tuple(
        TimestampedDailyReturn(timestamp, value)
        for timestamp, value in zip(expected_timestamps, expected_returns, strict=True)
    )
    stressed_series = tuple(
        TimestampedDailyReturn(timestamp, value)
        for timestamp, value in zip(stressed_timestamps, stressed_returns, strict=True)
    )

    fold_returns = candidate_performance[
        config.neighbor_candidate_order.index(config.baseline_arm_id)
    ].fold_returns
    asset_return_rows: list[NamedReturn] = []
    for asset in config.assets:
        pooled = tuple(
            value
            for fold, _year in config.fold_years
            for observed_asset, values in expected_by_fold[fold].returns_by_asset
            if observed_asset == asset
            for value in values
        )
        asset_return_rows.append(NamedReturn(asset, _compounded_return_pct(pooled)))
    positivity = summarize_fold_asset_positivity(fold_returns, tuple(asset_return_rows))
    neighbor = summarize_neighbor_support(
        tuple(candidate_performance),
        selected_candidate_id=config.baseline_arm_id,
        registered_candidate_order=config.neighbor_candidate_order,
        minimum_positive_selected_folds=config.minimum_positive_selected_folds,
        minimum_positive_pooled_neighbors=config.minimum_positive_pooled_neighbors,
        minimum_positive_fold_results_total=(config.minimum_positive_candidate_fold_cells_total),
    )

    attributed, attribution_reconciliation = _build_position_attribution(
        expected_by_fold,
        config=config,
    )
    closed_trades = attribution_reconciliation.closed_trade_contributions
    concentration_summary = (
        summarize_contribution_robustness(closed_trades, attributed_contributions=attributed)
        if closed_trades
        else None
    )
    concentration = ContributionRobustnessAvailability(
        available=concentration_summary is not None,
        unavailable_reason=(
            None if concentration_summary is not None else "NOT_APPLICABLE_DUE_TO_ZERO_TRADES"
        ),
        summary=concentration_summary,
        attribution_reconciliation=attribution_reconciliation,
    )
    absolute_risk = _summarize_absolute_risk(
        expected_returns=expected_returns,
        stressed_returns=stressed_returns,
        economic_timestamps=expected_timestamps,
        attribution=attribution_reconciliation,
        config=config,
    )
    regime_contributions = _summarize_regime_contributions(
        economic_regimes,
        config=config,
        attributed_contributions=attributed,
    )

    expected_total_return = _compounded_return_pct(expected_returns)
    stressed_total_return = _compounded_return_pct(stressed_returns)
    expected_reports = tuple(
        by_identity[(config.baseline_arm_id, asset, fold, config.expected_cost_scenario)]
        for fold, _year in config.fold_years
        for asset in config.assets
    )
    weight = 1.0 / len(config.assets)
    gross_pnl = math.fsum(weight * row.gross_pnl for row in expected_reports)
    expected_execution_cost = math.fsum(
        weight * (row.aggregate_execution_cost + row.terminal_exit_cost) for row in expected_reports
    )
    turnover = math.fsum(
        weight * (row.turnover_notional + row.terminal_exit_reference_notional)
        for row in expected_reports
    )
    break_even_cost = max(0.0, gross_pnl / turnover * 10_000.0) if turnover > 0.0 else 0.0
    expected_cost = expected_execution_cost / turnover * 10_000.0 if turnover > 0.0 else 0.0
    cost_tolerance = summarize_cost_tolerance(
        expected_return_pct=expected_total_return,
        stressed_return_pct=stressed_total_return,
        break_even_cost_per_traded_notional_bps=break_even_cost,
        expected_cost_per_traded_notional_bps=expected_cost,
        stressed_edge_reversal_limit_fraction=config.stressed_edge_reversal_limit_fraction,
    )

    expected_sortino = sortino_ratio(
        expected_returns, annualization_factor=config.annualization_factor
    )
    expected_drawdown = absolute_risk.expected_pooled_max_drawdown_pct
    exposure = math.fsum(row.exposure_pct for row in expected_reports) / len(expected_reports)
    benchmark_rows = tuple(expected_cost_benchmarks)
    if tuple(row.benchmark_id for row in benchmark_rows) != config.benchmark_order:
        _invalid("benchmark evidence must follow the exact registered benchmark order")
    if len({row.source_trial_id for row in benchmark_rows}) != len(benchmark_rows):
        _invalid("benchmark evidence source trial IDs must be unique")
    for row in benchmark_rows:
        if row.fold_id != "pooled-oos" or row.cost_scenario != config.expected_cost_scenario:
            _invalid("benchmark evidence must be pooled OOS under expected costs")
        if tuple(item.timestamp for item in row.daily_returns) != expected_timestamps:
            _invalid("benchmark and baseline pooled OOS timestamps must align exactly")
    benchmark_points = tuple(
        PerformancePoint(
            row.benchmark_id,
            _compounded_return_pct(tuple(item.daily_return for item in row.daily_returns)),
            max_drawdown_pct(
                _equity_from_returns(tuple(item.daily_return for item in row.daily_returns))
            ),
            sortino_ratio(
                tuple(item.daily_return for item in row.daily_returns),
                annualization_factor=config.annualization_factor,
            ),
            row.average_exposure_pct,
        )
        for row in benchmark_rows
    )
    strategy_point = PerformancePoint(
        config.baseline_arm_id,
        expected_total_return,
        expected_drawdown,
        expected_sortino,
        exposure,
    )
    raw_capital_pareto = compare_raw_registered_benchmarks(strategy_point, benchmark_points)

    return_ci = block_bootstrap_confidence_interval(
        expected_returns,
        _compounded_return_pct,
        block_size=config.bootstrap_block_bars,
        samples=config.bootstrap_samples,
        confidence=config.confidence_level,
        seed=config.bootstrap_seed,
    )
    sharpe_ci = block_bootstrap_confidence_interval(
        expected_returns,
        lambda values: sharpe_ratio(values, annualization_factor=config.annualization_factor),
        block_size=config.bootstrap_block_bars,
        samples=config.bootstrap_samples,
        confidence=config.confidence_level,
        seed=config.bootstrap_seed,
    )
    bootstrap = BootstrapInferenceSummary(
        compounded_return_pct=asdict(return_ci),
        annualized_sharpe=asdict(sharpe_ci),
    )
    if sharpe_dispersion is None:
        sharpe_evidence = SharpeDiagnosticAvailability(
            available=False,
            unavailable_reason="missing_attested_cross_trial_sharpe_dispersion",
            diagnostic=None,
        )
    else:
        effective_trials, trial_sharpe_std = _validated_trial_sharpe_dispersion(
            sharpe_dispersion,
            config=config,
        )
        diagnostic = sharpe_diagnostics(
            expected_returns,
            effective_trials=effective_trials,
            trial_sharpe_std=trial_sharpe_std,
            trial_sharpe_std_source=sharpe_dispersion.source,
            annualization_factor=config.annualization_factor,
            selection_contaminated=True,
        )
        sharpe_evidence = SharpeDiagnosticAvailability(
            available=True,
            unavailable_reason=None,
            diagnostic=asdict(diagnostic),
        )

    adaptive_payload = (
        _adaptive_summary(adaptive_selector, config=config)
        if adaptive_selector is not None
        else None
    )
    adaptive_support_met = bool(
        adaptive_payload is not None
        and adaptive_payload["baseline_training_support_requirement_met"]
    )
    contribution_share_met = bool(
        concentration_summary is not None
        and concentration_summary.maximum_asset_or_year_positive_share
        <= config.maximum_positive_contribution_share
    )
    contribution_ex_best_year_positive = bool(
        concentration_summary is not None
        and concentration_summary.best_calendar_year.contribution_after_removal > 0.0
    )
    contribution_ex_best_trade_positive = bool(
        concentration_summary is not None
        and concentration_summary.best_closed_trade.contribution_after_removal > 0.0
    )
    raw_baseline_zero_trades_or_exposure = not closed_trades or exposure <= 0.0
    ordered_fold_returns = sorted(item.return_pct for item in fold_returns)
    midpoint = len(ordered_fold_returns) // 2
    median_fold_return = (
        ordered_fold_returns[midpoint]
        if len(ordered_fold_returns) % 2
        else math.fsum(ordered_fold_returns[midpoint - 1 : midpoint + 1]) / 2.0
    )
    quantitative_checks = {
        "pooled_expected_cost_oos_return_positive": expected_total_return > 0.0,
        "median_oos_fold_return_positive": median_fold_return > 0.0,
        "all_assets_positive": positivity.all_assets_positive,
        "contribution_ex_best_closed_trade_positive": contribution_ex_best_trade_positive,
        "contribution_ex_best_calendar_year_positive": contribution_ex_best_year_positive,
        "contribution_share_requirement_met": contribution_share_met,
        "neighbor_support_requirements_met": neighbor.requirements_met,
        "cost_tolerance_requirements_met": cost_tolerance.requirements_met,
        "benchmark_pareto_not_dominated": not raw_capital_pareto.any_benchmark_dominates,
        "training_selector_supports_baseline": adaptive_support_met,
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
    }
    blocking_quantitative_gates = tuple(
        gate_id for gate_id, passed in quantitative_checks.items() if not passed
    )
    if raw_baseline_zero_trades_or_exposure:
        blocking_quantitative_gates = (
            "zero_completed_baseline_trades_or_exposure",
            *blocking_quantitative_gates,
        )
    missing_evidence = tuple(
        evidence_id
        for evidence_id, available in (
            ("training_selected_diagnostic", adaptive_payload is not None),
            ("attested_cross_trial_sharpe_dispersion", sharpe_evidence.available),
        )
        if not available
    )
    return FixedBaselineRobustnessEvidence(
        disposition_source_arm_id=config.baseline_arm_id,
        economic_period_date_rule=config.economic_period_date_rule,
        benchmark_comparator_contract={
            "comparator_id": config.benchmark_comparator_id,
            "gate_dimensions": [
                "total_return_pct_higher_is_better",
                "max_drawdown_pct_lower_is_better",
                "sortino_ratio_higher_is_better",
            ],
            "gate_metric_scaling": "none_compare_registered_portfolios_exactly_as_executed",
            "capital_allocation": "use_persisted_registered_portfolio_scaling",
            "dominance_rule": "all_dimensions_no_worse_and_at_least_one_strictly_better",
            "freeze_gate": (
                f"no_registered_{config.expected_cost_scenario}_benchmark_may_dominate_"
                f"{config.baseline_arm_id}"
            ),
        },
        source_reports=tuple(row.source_dict() for row in report_rows),
        source_benchmarks=tuple(row.source_dict() for row in benchmark_rows),
        expected_pooled_daily_returns=expected_series,
        stressed_pooled_daily_returns=stressed_series,
        expected_total_return_pct=expected_total_return,
        stressed_total_return_pct=stressed_total_return,
        expected_sharpe_ratio=sharpe_ratio(
            expected_returns, annualization_factor=config.annualization_factor
        ),
        expected_sortino_ratio=expected_sortino,
        expected_max_drawdown_pct=expected_drawdown,
        expected_exposure_pct=exposure,
        positivity=positivity,
        contribution_robustness=concentration,
        absolute_risk=absolute_risk,
        neighbor_support=neighbor,
        cost_tolerance=cost_tolerance,
        benchmark_raw_capital_pareto=raw_capital_pareto,
        regime_contributions=regime_contributions,
        bootstrap_inference=bootstrap,
        sharpe_diagnostics=sharpe_evidence,
        contribution_share_requirement_met=contribution_share_met,
        contribution_ex_best_closed_trade_positive=contribution_ex_best_trade_positive,
        contribution_ex_best_calendar_year_positive=(contribution_ex_best_year_positive),
        quantitative_gate_checks=quantitative_checks,
        raw_baseline_zero_trades_or_exposure=raw_baseline_zero_trades_or_exposure,
        quantitative_robustness_gate_eligible=not blocking_quantitative_gates,
        blocking_quantitative_gate_ids=blocking_quantitative_gates,
        evidence_complete=not missing_evidence,
        missing_evidence_ids=missing_evidence,
        completion_conclusion=(
            "COMPLETE" if not missing_evidence else "BLOCKED_INSUFFICIENT_EVIDENCE"
        ),
        adaptive_selector_diagnostic=adaptive_payload,
    )
