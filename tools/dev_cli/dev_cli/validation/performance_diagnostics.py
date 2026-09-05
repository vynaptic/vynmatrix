"""Fold, neighborhood, cost, and benchmark performance diagnostics."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

from dev_cli.validation._diagnostic_common import invalid, require_finite, require_nonempty


@dataclass(frozen=True, slots=True)
class NamedReturn:
    """A labelled expected-cost return for a fold or asset."""

    label: str
    return_pct: float

    def __post_init__(self) -> None:
        require_nonempty(self.label, field_name="label")
        require_finite(self.return_pct, field_name="return_pct")

    def to_dict(self) -> dict[str, object]:
        return {"label": self.label, "return_pct": self.return_pct}


@dataclass(frozen=True, slots=True)
class CandidateOOSPerformance:
    """One registered candidate's pooled and fold expected-cost results."""

    candidate_id: str
    pooled_return_pct: float
    fold_returns: tuple[NamedReturn, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.candidate_id, field_name="candidate_id")
        require_finite(self.pooled_return_pct, field_name="pooled_return_pct")
        if not self.fold_returns:
            invalid("candidate OOS performance requires fold returns")
        labels = tuple(item.label for item in self.fold_returns)
        if len(set(labels)) != len(labels):
            invalid("candidate OOS fold labels must be unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "pooled_return_pct": self.pooled_return_pct,
            "fold_returns": [item.to_dict() for item in self.fold_returns],
        }


@dataclass(frozen=True, slots=True)
class FoldAssetPositivitySummary:
    """Expected-cost positivity counts without collapsing their labels."""

    fold_returns: tuple[NamedReturn, ...]
    asset_returns: tuple[NamedReturn, ...]
    positive_fold_ids: tuple[str, ...]
    positive_asset_ids: tuple[str, ...]
    all_assets_positive: bool
    evidence_role: str = field(default="retrospective_robustness_diagnostic", init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "fold_returns": [item.to_dict() for item in self.fold_returns],
            "asset_returns": [item.to_dict() for item in self.asset_returns],
            "positive_fold_ids": list(self.positive_fold_ids),
            "positive_asset_ids": list(self.positive_asset_ids),
            "all_assets_positive": self.all_assets_positive,
            "evidence_role": self.evidence_role,
        }


def summarize_fold_asset_positivity(
    fold_returns: Sequence[NamedReturn],
    asset_returns: Sequence[NamedReturn],
) -> FoldAssetPositivitySummary:
    """Preserve and count supplied pooled-fold and per-asset returns."""

    folds = tuple(fold_returns)
    assets = tuple(asset_returns)
    if not folds or not assets:
        invalid("fold and asset positivity inputs must both be non-empty")
    for name, values in (("fold", folds), ("asset", assets)):
        labels = tuple(item.label for item in values)
        if len(set(labels)) != len(labels):
            invalid(f"{name} positivity labels must be unique")
    positive_folds = tuple(item.label for item in folds if item.return_pct > 0.0)
    positive_assets = tuple(item.label for item in assets if item.return_pct > 0.0)
    return FoldAssetPositivitySummary(
        fold_returns=folds,
        asset_returns=assets,
        positive_fold_ids=positive_folds,
        positive_asset_ids=positive_assets,
        all_assets_positive=len(positive_assets) == len(assets),
    )


@dataclass(frozen=True, slots=True)
class NeighborSupportSummary:
    """Protocol-threshold evaluation over a registered neighbor family."""

    selected_candidate_id: str
    registered_candidate_order: tuple[str, ...]
    positive_selected_fold_ids: tuple[str, ...]
    positive_pooled_neighbor_ids: tuple[str, ...]
    positive_fold_results_total: int
    minimum_positive_selected_folds: int
    minimum_positive_pooled_neighbors: int
    minimum_positive_fold_results_total: int
    selected_fold_requirement_met: bool
    pooled_neighbor_requirement_met: bool
    total_fold_result_requirement_met: bool
    requirements_met: bool
    evidence_role: str = field(default="retrospective_robustness_diagnostic", init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "selected_candidate_id": self.selected_candidate_id,
            "registered_candidate_order": list(self.registered_candidate_order),
            "positive_selected_fold_ids": list(self.positive_selected_fold_ids),
            "positive_pooled_neighbor_ids": list(self.positive_pooled_neighbor_ids),
            "positive_fold_results_total": self.positive_fold_results_total,
            "minimum_positive_selected_folds": self.minimum_positive_selected_folds,
            "minimum_positive_pooled_neighbors": self.minimum_positive_pooled_neighbors,
            "minimum_positive_fold_results_total": self.minimum_positive_fold_results_total,
            "selected_fold_requirement_met": self.selected_fold_requirement_met,
            "pooled_neighbor_requirement_met": self.pooled_neighbor_requirement_met,
            "total_fold_result_requirement_met": self.total_fold_result_requirement_met,
            "requirements_met": self.requirements_met,
            "evidence_role": self.evidence_role,
        }


def summarize_neighbor_support(
    candidates: Sequence[CandidateOOSPerformance],
    *,
    selected_candidate_id: str,
    registered_candidate_order: Sequence[str],
    minimum_positive_selected_folds: int,
    minimum_positive_pooled_neighbors: int,
    minimum_positive_fold_results_total: int,
) -> NeighborSupportSummary:
    """Evaluate only the supplied pre-registered candidate neighborhood."""

    performances = tuple(candidates)
    registered = tuple(registered_candidate_order)
    if any(
        value < 0
        for value in (
            minimum_positive_selected_folds,
            minimum_positive_pooled_neighbors,
            minimum_positive_fold_results_total,
        )
    ):
        invalid("neighbor support minimum counts must be non-negative")
    by_id = {item.candidate_id: item for item in performances}
    if len(by_id) != len(performances):
        invalid("neighbor candidate ids must be unique")
    if tuple(item.candidate_id for item in performances) != registered:
        invalid("candidate performances must follow registered_candidate_order exactly")
    selected = by_id.get(selected_candidate_id)
    if selected is None:
        invalid("selected_candidate_id must be in the registered candidates")
    expected_fold_labels = tuple(item.label for item in selected.fold_returns)
    if any(
        tuple(item.label for item in candidate.fold_returns) != expected_fold_labels
        for candidate in performances
    ):
        invalid("all neighbor candidates must have exactly aligned fold labels")
    positive_selected = tuple(item.label for item in selected.fold_returns if item.return_pct > 0.0)
    positive_neighbors = tuple(
        candidate_id
        for candidate_id in registered
        if candidate_id != selected_candidate_id and by_id[candidate_id].pooled_return_pct > 0.0
    )
    positive_fold_results = sum(
        item.return_pct > 0.0 for candidate in performances for item in candidate.fold_returns
    )
    selected_met = len(positive_selected) >= minimum_positive_selected_folds
    neighbors_met = len(positive_neighbors) >= minimum_positive_pooled_neighbors
    total_met = positive_fold_results >= minimum_positive_fold_results_total
    return NeighborSupportSummary(
        selected_candidate_id=selected_candidate_id,
        registered_candidate_order=registered,
        positive_selected_fold_ids=positive_selected,
        positive_pooled_neighbor_ids=positive_neighbors,
        positive_fold_results_total=positive_fold_results,
        minimum_positive_selected_folds=minimum_positive_selected_folds,
        minimum_positive_pooled_neighbors=minimum_positive_pooled_neighbors,
        minimum_positive_fold_results_total=minimum_positive_fold_results_total,
        selected_fold_requirement_met=selected_met,
        pooled_neighbor_requirement_met=neighbors_met,
        total_fold_result_requirement_met=total_met,
        requirements_met=selected_met and neighbors_met and total_met,
    )


@dataclass(frozen=True, slots=True)
class CostToleranceSummary:
    """Expected/stressed edge and break-even-cost checks from the protocol."""

    expected_return_pct: float
    stressed_return_pct: float
    break_even_cost_per_traded_notional_bps: float
    expected_cost_per_traded_notional_bps: float
    stressed_edge_degradation_fraction: float | None
    stressed_edge_reversal_limit_fraction: float
    break_even_exceeds_expected_cost: bool
    stressed_edge_within_reversal_limit: bool
    requirements_met: bool
    evidence_role: str = field(default="retrospective_robustness_diagnostic", init=False)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def summarize_cost_tolerance(
    *,
    expected_return_pct: float,
    stressed_return_pct: float,
    break_even_cost_per_traded_notional_bps: float,
    expected_cost_per_traded_notional_bps: float,
    stressed_edge_reversal_limit_fraction: float,
) -> CostToleranceSummary:
    """Evaluate caller-supplied cost evidence without estimating missing costs."""

    values = {
        "expected_return_pct": expected_return_pct,
        "stressed_return_pct": stressed_return_pct,
        "break_even_cost_per_traded_notional_bps": break_even_cost_per_traded_notional_bps,
        "expected_cost_per_traded_notional_bps": expected_cost_per_traded_notional_bps,
        "stressed_edge_reversal_limit_fraction": stressed_edge_reversal_limit_fraction,
    }
    for name, value in values.items():
        require_finite(value, field_name=name)
    if break_even_cost_per_traded_notional_bps < 0.0 or expected_cost_per_traded_notional_bps < 0.0:
        invalid("per-traded-notional costs must be non-negative")
    if stressed_edge_reversal_limit_fraction < 0.0:
        invalid("stressed_edge_reversal_limit_fraction must be non-negative")
    degradation = (
        max(0.0, expected_return_pct - stressed_return_pct) / expected_return_pct
        if expected_return_pct > 0.0
        else None
    )
    break_even_pass = (
        break_even_cost_per_traded_notional_bps > expected_cost_per_traded_notional_bps
    )
    stressed_pass = (
        expected_return_pct > 0.0
        and degradation is not None
        and degradation <= stressed_edge_reversal_limit_fraction
    )
    return CostToleranceSummary(
        expected_return_pct=expected_return_pct,
        stressed_return_pct=stressed_return_pct,
        break_even_cost_per_traded_notional_bps=break_even_cost_per_traded_notional_bps,
        expected_cost_per_traded_notional_bps=expected_cost_per_traded_notional_bps,
        stressed_edge_degradation_fraction=degradation,
        stressed_edge_reversal_limit_fraction=stressed_edge_reversal_limit_fraction,
        break_even_exceeds_expected_cost=break_even_pass,
        stressed_edge_within_reversal_limit=stressed_pass,
        requirements_met=break_even_pass and stressed_pass,
    )


@dataclass(frozen=True, slots=True)
class PerformancePoint:
    """Expected-cost performance point for exposure-aware Pareto comparison."""

    identifier: str
    total_return_pct: float
    max_drawdown_pct: float
    downside_adjusted_ratio: float
    average_exposure_pct: float

    def __post_init__(self) -> None:
        require_nonempty(self.identifier, field_name="identifier")
        for name, value in (
            ("total_return_pct", self.total_return_pct),
            ("max_drawdown_pct", self.max_drawdown_pct),
            ("downside_adjusted_ratio", self.downside_adjusted_ratio),
            ("average_exposure_pct", self.average_exposure_pct),
        ):
            require_finite(value, field_name=name)
        if self.max_drawdown_pct < 0.0 or self.average_exposure_pct < 0.0:
            invalid("max_drawdown_pct and average_exposure_pct must be non-negative")
        if self.average_exposure_pct == 0.0 and any(
            value != 0.0
            for value in (
                self.total_return_pct,
                self.max_drawdown_pct,
                self.downside_adjusted_ratio,
            )
        ):
            invalid("zero-exposure performance points must be zero-return cash points")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RawBenchmarkParetoComparison:
    """One benchmark compared exactly as executed, with no metric rescaling."""

    benchmark: PerformancePoint
    dominates_strategy: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "benchmark": self.benchmark.to_dict(),
            "dominates_strategy": self.dominates_strategy,
        }


@dataclass(frozen=True, slots=True)
class RawBenchmarkParetoSummary:
    """Registered-capital benchmark Pareto result used by the economic gate."""

    strategy: PerformancePoint
    comparisons: tuple[RawBenchmarkParetoComparison, ...]
    dominating_benchmark_ids: tuple[str, ...]
    any_benchmark_dominates: bool
    metric_scaling: str = field(default="none_exactly_as_executed", init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy.to_dict(),
            "comparisons": [item.to_dict() for item in self.comparisons],
            "dominating_benchmark_ids": list(self.dominating_benchmark_ids),
            "any_benchmark_dominates": self.any_benchmark_dominates,
            "metric_scaling": self.metric_scaling,
        }


def compare_raw_registered_benchmarks(
    strategy: PerformancePoint,
    benchmarks: Sequence[PerformancePoint],
) -> RawBenchmarkParetoSummary:
    """Compare return, drawdown, and Sortino exactly as registered/executed."""

    points = tuple(benchmarks)
    if not points:
        invalid("at least one registered raw benchmark is required")
    identifiers = tuple(item.identifier for item in points)
    if len(set(identifiers)) != len(identifiers):
        invalid("raw benchmark identifiers must be unique")
    if strategy.identifier in set(identifiers):
        invalid("strategy identifier must differ from raw benchmark identifiers")
    comparisons: list[RawBenchmarkParetoComparison] = []
    for benchmark in points:
        no_worse = (
            benchmark.total_return_pct >= strategy.total_return_pct
            and benchmark.max_drawdown_pct <= strategy.max_drawdown_pct
            and benchmark.downside_adjusted_ratio >= strategy.downside_adjusted_ratio
        )
        strictly_better = (
            benchmark.total_return_pct > strategy.total_return_pct
            or benchmark.max_drawdown_pct < strategy.max_drawdown_pct
            or benchmark.downside_adjusted_ratio > strategy.downside_adjusted_ratio
        )
        comparisons.append(
            RawBenchmarkParetoComparison(
                benchmark=benchmark,
                dominates_strategy=no_worse and strictly_better,
            )
        )
    dominating = tuple(item.benchmark.identifier for item in comparisons if item.dominates_strategy)
    return RawBenchmarkParetoSummary(
        strategy=strategy,
        comparisons=tuple(comparisons),
        dominating_benchmark_ids=dominating,
        any_benchmark_dominates=bool(dominating),
    )
