"""Joint-asset timestamp-anchored evaluation over the validation engine."""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from statistics import fmean
from typing import Any

from lib_data.bars import Bar
from lib_strategy.signals.pure_strategy import PureSignalStrategy

from .engine import BacktestConfig, BacktestEngine, BacktestReport
from .execution import (
    CostBreakdown,
    ExecutionCostModel,
    adverse_execution_price,
    execution_fill_costs,
    normalize_execution_bar,
)
from .folds import (
    AnchoredFold,
    EventInterval,
    TimestampFoldWindow,
    build_timestamp_anchored_folds,
)
from .metrics import annualized_volatility_pct, max_drawdown_pct, sharpe_ratio, sortino_ratio

ParamStrategyFactory = Callable[[Mapping[str, Any]], PureSignalStrategy]
TerminalExitCostEstimator = Callable[[BacktestReport], float]


@dataclass(frozen=True)
class FoldOOSSeries:
    """One fold's exact expected timestamps and independently based equity."""

    fold_index: int
    expected_timestamps: tuple[datetime, ...]
    equity_curve: tuple[tuple[datetime, float], ...]
    initial_capital: float


@dataclass(frozen=True)
class PooledOOSObservation:
    """One return in the primary chronological pooled-OOS series."""

    timestamp: datetime
    fold_index: int
    daily_return: float

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "fold_index": self.fold_index,
            "daily_return": self.daily_return,
        }


@dataclass(frozen=True)
class PooledOOSMetrics:
    """Metrics calculated once over concatenated non-overlapping OOS returns."""

    observations: int
    folds: int
    compounded_return_pct: float
    annualized_volatility_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_pct: float
    evidence_role: str = field(default="primary_pooled_oos", init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "observations": self.observations,
            "folds": self.folds,
            "compounded_return_pct": self.compounded_return_pct,
            "annualized_volatility_pct": self.annualized_volatility_pct,
            "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio,
            "max_drawdown_pct": self.max_drawdown_pct,
            "evidence_role": self.evidence_role,
        }


@dataclass(frozen=True)
class PooledOOSResult:
    observations: tuple[PooledOOSObservation, ...]
    metrics: PooledOOSMetrics

    def to_dict(self) -> dict[str, object]:
        return {
            "observations": [observation.to_dict() for observation in self.observations],
            "metrics": self.metrics.to_dict(),
        }


@dataclass(frozen=True)
class TerminalExitCostPolicy:
    """Named caller-owned estimate for hypothetically exiting terminal exposure.

    The estimator returns an additional non-negative account-currency cost.  It
    does not create a trade or mutate the raw backtest report.  Selection and
    pooled-OOS series subtract the amount only from their final equity
    observation, making otherwise-flat fold resets economically explicit.
    """

    policy_id: str
    estimator: TerminalExitCostEstimator = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.policy_id.strip():
            msg = "terminal exit-cost policy_id must not be blank"
            raise ValueError(msg)
        if not callable(self.estimator):
            msg = "terminal exit-cost estimator must be callable"
            raise TypeError(msg)


@dataclass(frozen=True)
class TerminalLiquidationEstimate:
    """Exit-only decomposition for marked terminal exposure."""

    symbol: str
    position_side: str
    transaction_side: int
    quantity: float
    mark_price: float
    execution_price: float
    costs: CostBreakdown

    @property
    def total_cost(self) -> float:
        return self.costs.total

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "position_side": self.position_side,
            "transaction_side": self.transaction_side,
            "quantity": self.quantity,
            "mark_price": self.mark_price,
            "execution_price": self.execution_price,
            "costs": self.costs.to_dict(),
            "total_cost": self.total_cost,
        }


def estimate_terminal_liquidation(
    report: BacktestReport,
    cost_model: ExecutionCostModel,
) -> TerminalLiquidationEstimate:
    """Estimate an immediate adverse exit at the terminal mark.

    The raw report and terminal position remain unchanged.  Price and cost
    decomposition call the same helpers as :class:`FillSimulator`.
    """

    terminal = report.terminal_position
    if terminal is None:
        msg = "terminal liquidation requires an open terminal position"
        raise ValueError(msg)
    if cost_model.latency_bars != 0:
        msg = "terminal liquidation at the final mark requires zero latency"
        raise ValueError(msg)
    if terminal.symbol != report.config.symbol:
        msg = "terminal position symbol does not match the backtest report"
        raise ValueError(msg)
    if terminal.side == "long":
        transaction_side = -1
    elif terminal.side == "short":
        transaction_side = 1
    else:
        msg = "terminal position side must be long or short"
        raise ValueError(msg)
    if not math.isfinite(terminal.quantity) or terminal.quantity <= 0.0:
        msg = "terminal position quantity must be finite and positive"
        raise ValueError(msg)
    if not math.isfinite(terminal.mark_price) or terminal.mark_price <= 0.0:
        msg = "terminal mark_price must be finite and positive"
        raise ValueError(msg)

    execution_price = adverse_execution_price(
        terminal.mark_price,
        transaction_side=transaction_side,
        cost_model=cost_model,
    )
    costs = execution_fill_costs(
        reference_price=terminal.mark_price,
        execution_price=execution_price,
        quantity=terminal.quantity,
        cost_model=cost_model,
    )
    return TerminalLiquidationEstimate(
        symbol=terminal.symbol,
        position_side=terminal.side,
        transaction_side=transaction_side,
        quantity=terminal.quantity,
        mark_price=terminal.mark_price,
        execution_price=execution_price,
        costs=costs,
    )


def build_terminal_liquidation_cost_policy(
    cost_model: ExecutionCostModel,
    *,
    policy_id: str | None = None,
) -> TerminalExitCostPolicy:
    """Build a named policy backed by exact simulator liquidation math."""

    resolved_policy_id = policy_id or (
        f"terminal-liquidation:{cost_model.scenario_name}:{cost_model.version}"
    )

    def estimator(report: BacktestReport) -> float:
        return estimate_terminal_liquidation(report, cost_model).total_cost

    return TerminalExitCostPolicy(
        policy_id=resolved_policy_id,
        estimator=estimator,
    )


@dataclass(frozen=True)
class AppliedTerminalExitCost:
    """Auditable hypothetical adjustment applied outside a raw report."""

    asset: str
    fold_index: int
    phase: str
    policy_id: str
    timestamp: datetime
    amount: float
    unadjusted_equity: float
    adjusted_equity: float
    component_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "asset": self.asset,
            "fold_index": self.fold_index,
            "phase": self.phase,
            "policy_id": self.policy_id,
            "timestamp": self.timestamp.isoformat(),
            "amount": self.amount,
            "unadjusted_equity": self.unadjusted_equity,
            "adjusted_equity": self.adjusted_equity,
        }
        if self.component_id is not None:
            payload["component_id"] = self.component_id
        return payload


@dataclass(frozen=True)
class FixedWeightEquityComponent:
    """One normalized component in a fixed-weight portfolio return series."""

    component_id: str
    equity_curve: tuple[tuple[datetime, float], ...]
    initial_capital: float
    weight: float


@dataclass(frozen=True)
class AssetBacktestReport:
    """A deterministic asset label paired with its unmodified raw report."""

    asset: str
    report: BacktestReport

    def to_dict(self) -> dict[str, object]:
        return {
            "asset": self.asset,
            "report": self.report.to_dict(),
            "equity_curve": [
                [timestamp.isoformat(), equity] for timestamp, equity in self.report.equity_curve
            ],
        }


@dataclass(frozen=True)
class JointAssetTrainingScore:
    """One registry candidate's complete joint-asset training evidence."""

    registry_index: int
    params: dict[str, Any]
    annualized_sharpe: float
    observations: int
    return_timestamps: tuple[datetime, ...]
    equal_weight_daily_returns: tuple[float, ...]
    asset_reports: tuple[AssetBacktestReport, ...]
    terminal_exit_costs: tuple[AppliedTerminalExitCost, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "registry_index": self.registry_index,
            "params": dict(self.params),
            "annualized_sharpe": self.annualized_sharpe,
            "observations": self.observations,
            "return_timestamps": [timestamp.isoformat() for timestamp in self.return_timestamps],
            "equal_weight_daily_returns": list(self.equal_weight_daily_returns),
            "asset_reports": [item.to_dict() for item in self.asset_reports],
            "terminal_exit_costs": [item.to_dict() for item in self.terminal_exit_costs],
        }


@dataclass(frozen=True)
class JointAssetAnchoredWindow:
    """One exact-calendar fold selected jointly and evaluated per asset."""

    fold: AnchoredFold
    selected_registry_index: int
    selected_params: dict[str, Any]
    training_score_ledger: tuple[JointAssetTrainingScore, ...]
    oos_asset_reports: tuple[AssetBacktestReport, ...]
    oos_terminal_exit_costs: tuple[AppliedTerminalExitCost, ...]
    equal_weight_oos_series: FoldOOSSeries

    def to_dict(self) -> dict[str, object]:
        return {
            "fold": {
                "index": self.fold.index,
                "train_start": self.fold.train_start_ts.isoformat(),
                "selection_last": (
                    self.fold.selection_last_ts.isoformat()
                    if self.fold.selection_last_ts is not None
                    else None
                ),
                "train_end_exclusive": self.fold.train_end_exclusive_ts.isoformat(),
                "declared_train_end_exclusive": (
                    self.fold.declared_train_end_exclusive_ts.isoformat()
                ),
                "calendar_purge_start": (
                    self.fold.calendar_purge_start_ts.isoformat()
                    if self.fold.calendar_purge_start_ts is not None
                    else None
                ),
                "pre_oos_purge_seconds": self.fold.pre_oos_purge.total_seconds(),
                "embargo_bars": self.fold.embargo_bars,
                "training_indices": list(self.fold.training_indices),
                "calendar_purged_indices": list(self.fold.calendar_purged_training_indices),
                "embargoed_indices": list(self.fold.embargoed_training_indices),
                "overlap_purged_indices": list(self.fold.overlap_purged_training_indices),
                "bootstrap_indices": list(self.fold.bootstrap_indices),
                "test_indices": list(self.fold.test_indices),
                "test_start": self.fold.test_start_ts.isoformat(),
                "test_end_exclusive": (
                    self.fold.test_end_exclusive_ts.isoformat()
                    if self.fold.test_end_exclusive_ts is not None
                    else None
                ),
                "test_observation_timestamps": [
                    timestamp.isoformat() for timestamp in self.fold.test_observation_timestamps
                ],
            },
            "selected_registry_index": self.selected_registry_index,
            "selected_params": dict(self.selected_params),
            "training_score_ledger": [item.to_dict() for item in self.training_score_ledger],
            "oos_asset_reports": [item.to_dict() for item in self.oos_asset_reports],
            "oos_terminal_exit_costs": [item.to_dict() for item in self.oos_terminal_exit_costs],
            "equal_weight_oos_series": {
                "fold_index": self.equal_weight_oos_series.fold_index,
                "initial_capital": self.equal_weight_oos_series.initial_capital,
                "expected_timestamps": [
                    timestamp.isoformat()
                    for timestamp in self.equal_weight_oos_series.expected_timestamps
                ],
                "equity_curve": [
                    [timestamp.isoformat(), equity]
                    for timestamp, equity in self.equal_weight_oos_series.equity_curve
                ],
            },
        }


@dataclass(frozen=True)
class JointAssetAnchoredResult:
    """Primary joint-asset chronological evidence across all OOS folds."""

    asset_order: tuple[str, ...]
    training_evaluation_start: datetime
    selected_arm_tie_tolerance: float
    baseline_registry_index: int
    windows: tuple[JointAssetAnchoredWindow, ...]
    pooled_oos: PooledOOSResult
    evidence_role: str = field(default="retrospective_diagnostic", init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "asset_order": list(self.asset_order),
            "training_evaluation_start": self.training_evaluation_start.isoformat(),
            "selected_arm_tie_tolerance": self.selected_arm_tie_tolerance,
            "baseline_registry_index": self.baseline_registry_index,
            "windows": [window.to_dict() for window in self.windows],
            "pooled_oos": self.pooled_oos.to_dict(),
            "evidence_role": self.evidence_role,
        }


@dataclass(frozen=True)
class _JointAssetPanel:
    asset_order: tuple[str, ...]
    consolidated_by_asset: dict[str, list[Bar]]
    configs_by_asset: Mapping[str, BacktestConfig]
    timestamps: tuple[datetime, ...]
    annualization_factor: float


def pool_fold_oos_returns(
    folds: Sequence[FoldOOSSeries],
    *,
    annualization_factor: float,
) -> PooledOOSResult:
    """Concatenate exact non-overlapping fold returns and calculate once.

    Each fold's first return is measured from that fold's explicit initial
    capital. Expected timestamps must exactly equal the observed equity
    timestamps, so a missing OOS day cannot disappear from the pooled result.
    """

    if not folds:
        msg = "pooled OOS requires at least one fold"
        raise ValueError(msg)
    if not math.isfinite(annualization_factor) or annualization_factor <= 0.0:
        msg = "annualization_factor must be finite and positive"
        raise ValueError(msg)
    fold_ids = [fold.fold_index for fold in folds]
    if len(fold_ids) != len(set(fold_ids)):
        msg = "pooled OOS fold IDs must be unique"
        raise ValueError(msg)

    observations: list[PooledOOSObservation] = []
    previous_timestamp: datetime | None = None
    for fold in folds:
        if not math.isfinite(fold.initial_capital) or fold.initial_capital <= 0.0:
            msg = f"fold {fold.fold_index} initial_capital must be finite and positive"
            raise ValueError(msg)
        observed_timestamps = tuple(timestamp for timestamp, _ in fold.equity_curve)
        if observed_timestamps != fold.expected_timestamps:
            msg = f"fold {fold.fold_index} has missing or unexpected OOS observations"
            raise ValueError(msg)
        if not observed_timestamps:
            msg = f"fold {fold.fold_index} contains no OOS observations"
            raise ValueError(msg)
        if len({timestamp.date() for timestamp in observed_timestamps}) != len(observed_timestamps):
            msg = f"fold {fold.fold_index} is not a one-observation-per-day OOS series"
            raise ValueError(msg)
        if any(
            current <= previous for previous, current in itertools.pairwise(observed_timestamps)
        ):
            msg = f"fold {fold.fold_index} OOS timestamps must be strictly increasing"
            raise ValueError(msg)
        if previous_timestamp is not None and observed_timestamps[0] <= previous_timestamp:
            msg = "pooled OOS folds contain duplicate, overlapping, or out-of-order timestamps"
            raise ValueError(msg)

        previous_equity = fold.initial_capital
        for timestamp, equity in fold.equity_curve:
            if not math.isfinite(equity) or equity <= 0.0:
                msg = f"fold {fold.fold_index} equity must be finite and positive"
                raise ValueError(msg)
            daily_return = equity / previous_equity - 1.0
            observations.append(
                PooledOOSObservation(
                    timestamp=timestamp,
                    fold_index=fold.fold_index,
                    daily_return=daily_return,
                )
            )
            previous_equity = equity
        previous_timestamp = observed_timestamps[-1]

    returns = [observation.daily_return for observation in observations]
    growth = 1.0
    pooled_equity = [1.0]
    for daily_return in returns:
        growth *= 1.0 + daily_return
        pooled_equity.append(growth)
    return PooledOOSResult(
        observations=tuple(observations),
        metrics=PooledOOSMetrics(
            observations=len(observations),
            folds=len(folds),
            compounded_return_pct=(growth - 1.0) * 100.0,
            annualized_volatility_pct=annualized_volatility_pct(
                returns,
                annualization_factor=annualization_factor,
            ),
            sharpe_ratio=sharpe_ratio(
                returns,
                annualization_factor=annualization_factor,
            ),
            sortino_ratio=sortino_ratio(
                returns,
                annualization_factor=annualization_factor,
            ),
            max_drawdown_pct=max_drawdown_pct(pooled_equity),
        ),
    )


def _validate_training_evaluation_start(
    training_evaluation_start: datetime,
    timestamps: Sequence[datetime],
) -> datetime:
    if training_evaluation_start.tzinfo is None or training_evaluation_start.utcoffset() is None:
        msg = "training_evaluation_start must be timezone-aware"
        raise ValueError(msg)
    normalized = training_evaluation_start.astimezone(UTC)
    if normalized not in timestamps:
        msg = "training_evaluation_start must match an exact decision timestamp"
        raise ValueError(msg)
    if not any(timestamp < normalized for timestamp in timestamps):
        msg = "training_evaluation_start requires earlier non-trading bootstrap bars"
        raise ValueError(msg)
    return normalized


def _terminal_exit_cost_amount(
    policy: TerminalExitCostPolicy,
    report: BacktestReport,
) -> float:
    raw_amount = policy.estimator(report)
    if isinstance(raw_amount, bool):
        msg = "terminal exit-cost estimator returned a bool"
        raise TypeError(msg)
    return float(raw_amount)


def prepare_report_equity_evidence(  # noqa: PLR0912, PLR0915 - fail-closed evidence invariants
    report: BacktestReport,
    *,
    expected_symbol: str,
    evidence_id: str,
    fold_index: int | None,
    phase: str,
    expected_timestamps: tuple[datetime, ...],
    terminal_exit_cost_policy: TerminalExitCostPolicy | None,
    liquidate_terminal: bool,
    component_id: str | None = None,
) -> tuple[tuple[tuple[datetime, float], ...], AppliedTerminalExitCost | None]:
    """Validate report evidence and optionally charge terminal liquidation.

    The raw report is never mutated.  Full-panel evidence passes
    ``liquidate_terminal=False`` and retains marked-to-market terminal exposure;
    reset OOS folds pass ``True`` and must supply a policy whenever exposure is
    open at the boundary.
    """

    if not isinstance(expected_symbol, str) or not expected_symbol.strip():
        msg = "expected_symbol must not be blank"
        raise ValueError(msg)
    if not isinstance(evidence_id, str) or not evidence_id.strip():
        msg = "evidence_id must not be blank"
        raise ValueError(msg)
    if not isinstance(phase, str) or not phase.strip():
        msg = "phase must not be blank"
        raise ValueError(msg)
    if component_id is not None and (not isinstance(component_id, str) or not component_id.strip()):
        msg = "component_id must be a non-blank string when supplied"
        raise ValueError(msg)
    if not isinstance(liquidate_terminal, bool):
        msg = "liquidate_terminal must be a bool"
        raise TypeError(msg)
    scope = (
        f"fold {fold_index} {phase} report for {evidence_id}"
        if fold_index is not None
        else f"{phase} report for {evidence_id}"
    )
    if report.config.symbol != expected_symbol:
        msg = f"{scope} symbol does not match expected symbol {expected_symbol}"
        raise ValueError(msg)

    selection_blockers = set(report.selection_blockers)
    if "account_ruined" in selection_blockers:
        msg = f"{scope} ruined the account"
        raise ValueError(msg)
    if "unmatched_signals" in selection_blockers:
        msg = f"{scope} has unmatched signals"
        raise ValueError(msg)
    if "unresolved_model_execution_divergence" in selection_blockers:
        msg = f"{scope} has unresolved divergence"
        raise ValueError(msg)
    if "model_execution_position_lineage_divergence" in selection_blockers:
        msg = f"{scope} has position lineage divergence"
        raise ValueError(msg)
    if not math.isfinite(report.config.initial_capital) or report.config.initial_capital <= 0.0:
        msg = f"{scope} initial capital must be finite and positive"
        raise ValueError(msg)

    equity_curve = tuple(report.equity_curve)
    observed_timestamps = tuple(timestamp for timestamp, _ in equity_curve)
    if observed_timestamps != expected_timestamps:
        msg = f"{scope} has missing or unexpected observations"
        raise ValueError(msg)
    if not equity_curve:
        msg = f"{scope} contains no observations"
        raise ValueError(msg)
    for timestamp in observed_timestamps:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            msg = f"{scope} contains a timezone-naive observation"
            raise ValueError(msg)
    if any(current <= previous for previous, current in itertools.pairwise(observed_timestamps)):
        msg = f"{scope} timestamps must be strictly increasing"
        raise ValueError(msg)
    if len({timestamp.date() for timestamp in observed_timestamps}) != len(observed_timestamps):
        msg = f"{scope} is not daily"
        raise ValueError(msg)
    if any(not math.isfinite(equity) or equity <= 0.0 for _, equity in equity_curve):
        msg = f"{scope} has nonfinite or ruined equity"
        raise ValueError(msg)

    terminal = report.terminal_position
    if terminal is None:
        return equity_curve, None
    if terminal.symbol != expected_symbol:
        msg = f"{scope} terminal position symbol does not match the report"
        raise ValueError(msg)
    if not liquidate_terminal:
        return equity_curve, None
    if fold_index is None:
        msg = f"{scope} terminal liquidation requires a fold index"
        raise ValueError(msg)
    if terminal_exit_cost_policy is None:
        msg = f"{scope} has terminal exposure without an exit-cost policy"
        raise ValueError(msg)
    try:
        amount = _terminal_exit_cost_amount(terminal_exit_cost_policy, report)
    except (TypeError, ValueError, OverflowError) as exc:
        msg = f"{scope} terminal exit-cost estimator did not return a number"
        raise ValueError(msg) from exc
    if not math.isfinite(amount) or amount <= 0.0:
        msg = f"{scope} terminal exit cost must be finite and positive"
        raise ValueError(msg)

    timestamp, unadjusted_equity = equity_curve[-1]
    adjusted_equity = unadjusted_equity - amount
    if not math.isfinite(adjusted_equity) or adjusted_equity <= 0.0:
        msg = f"{scope} terminal exit cost ruins equity"
        raise ValueError(msg)
    adjusted_curve = (*equity_curve[:-1], (timestamp, adjusted_equity))
    return adjusted_curve, AppliedTerminalExitCost(
        asset=expected_symbol,
        fold_index=fold_index,
        phase=phase,
        policy_id=terminal_exit_cost_policy.policy_id,
        timestamp=timestamp,
        amount=amount,
        unadjusted_equity=unadjusted_equity,
        adjusted_equity=adjusted_equity,
        component_id=component_id,
    )


def _validate_report_for_joint_evidence(
    report: BacktestReport,
    *,
    asset: str,
    fold_index: int,
    phase: str,
    expected_timestamps: tuple[datetime, ...],
    terminal_exit_cost_policy: TerminalExitCostPolicy | None,
) -> tuple[tuple[tuple[datetime, float], ...], AppliedTerminalExitCost | None]:
    """Compatibility wrapper over the reusable report-evidence boundary."""

    return prepare_report_equity_evidence(
        report,
        expected_symbol=asset,
        evidence_id=asset,
        fold_index=fold_index,
        phase=phase,
        expected_timestamps=expected_timestamps,
        terminal_exit_cost_policy=terminal_exit_cost_policy,
        liquidate_terminal=True,
    )


def combine_fixed_weight_equity_curves(  # noqa: PLR0912, PLR0915 - fail-closed panel invariants
    components: Sequence[FixedWeightEquityComponent],
    *,
    expected_timestamps: tuple[datetime, ...],
    evidence_id: str,
) -> tuple[tuple[tuple[datetime, float], ...], tuple[float, ...]]:
    """Combine complete aligned curves as a daily-rebalanced fixed mix."""

    if not isinstance(evidence_id, str) or not evidence_id.strip():
        msg = "fixed-weight evidence_id must not be blank"
        raise ValueError(msg)
    if not components:
        msg = f"{evidence_id} requires at least one equity component"
        raise ValueError(msg)
    if not expected_timestamps:
        msg = f"{evidence_id} expected timestamps must not be empty"
        raise ValueError(msg)
    for timestamp in expected_timestamps:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            msg = f"{evidence_id} expected timestamps must be timezone-aware"
            raise ValueError(msg)
    if any(current <= previous for previous, current in itertools.pairwise(expected_timestamps)):
        msg = f"{evidence_id} expected timestamps must be strictly increasing"
        raise ValueError(msg)
    if len({timestamp.date() for timestamp in expected_timestamps}) != len(expected_timestamps):
        msg = f"{evidence_id} requires exactly one observation per day"
        raise ValueError(msg)

    component_ids = [component.component_id for component in components]
    if any(
        not isinstance(component_id, str) or not component_id.strip()
        for component_id in component_ids
    ):
        msg = f"{evidence_id} component IDs must not be blank"
        raise ValueError(msg)
    if len(component_ids) != len(set(component_ids)):
        msg = f"{evidence_id} component IDs must be unique"
        raise ValueError(msg)
    for component in components:
        if (
            isinstance(component.weight, bool)
            or not math.isfinite(component.weight)
            or component.weight <= 0.0
        ):
            msg = f"{evidence_id} weight for {component.component_id} must be positive"
            raise ValueError(msg)
    total_weight = math.fsum(component.weight for component in components)
    if not math.isclose(total_weight, 1.0, rel_tol=0.0, abs_tol=1e-12):
        msg = f"{evidence_id} component weights must sum to 1.0"
        raise ValueError(msg)

    component_returns: list[tuple[float, tuple[float, ...]]] = []
    for component in components:
        if not math.isfinite(component.initial_capital) or component.initial_capital <= 0.0:
            msg = f"{evidence_id} initial capital for {component.component_id} must be positive"
            raise ValueError(msg)
        observed = tuple(timestamp for timestamp, _ in component.equity_curve)
        if observed != expected_timestamps:
            msg = f"{evidence_id} timestamp coverage differs for {component.component_id}"
            raise ValueError(msg)
        previous_equity = component.initial_capital
        returns: list[float] = []
        for _, equity in component.equity_curve:
            if not math.isfinite(equity) or equity <= 0.0:
                msg = f"{evidence_id} has nonfinite or ruined equity for {component.component_id}"
                raise ValueError(msg)
            daily_return = equity / previous_equity - 1.0
            if not math.isfinite(daily_return) or daily_return <= -1.0:
                msg = f"{evidence_id} has nonfinite or ruinous return for {component.component_id}"
                raise ValueError(msg)
            returns.append(daily_return)
            previous_equity = equity
        component_returns.append((component.weight, tuple(returns)))

    equal_weight = 1.0 / len(component_returns)
    if all(weight == equal_weight for weight, _returns in component_returns):
        # Preserve the established joint-selector arithmetic exactly.
        fixed_weight_returns = tuple(
            fmean(returns[index] for _weight, returns in component_returns)
            for index in range(len(expected_timestamps))
        )
    else:
        fixed_weight_returns = tuple(
            math.fsum(weight * returns[index] for weight, returns in component_returns)
            for index in range(len(expected_timestamps))
        )
    growth = 1.0
    portfolio_equity: list[tuple[datetime, float]] = []
    for timestamp, daily_return in zip(
        expected_timestamps,
        fixed_weight_returns,
        strict=True,
    ):
        growth *= 1.0 + daily_return
        if not math.isfinite(growth) or growth <= 0.0:
            msg = f"{evidence_id} fixed-weight portfolio is ruined"
            raise ValueError(msg)
        portfolio_equity.append((timestamp, growth))
    return tuple(portfolio_equity), fixed_weight_returns


def _equal_weight_equity_series(
    asset_curves: Sequence[tuple[str, tuple[tuple[datetime, float], ...], float]],
    *,
    fold_index: int,
    expected_timestamps: tuple[datetime, ...],
) -> tuple[tuple[tuple[datetime, float], ...], tuple[float, ...]]:
    if not asset_curves:
        msg = "equal-weight series requires at least one asset"
        raise ValueError(msg)
    weight = 1.0 / len(asset_curves)
    components = tuple(
        FixedWeightEquityComponent(
            component_id=asset,
            equity_curve=equity_curve,
            initial_capital=initial_capital,
            weight=weight,
        )
        for asset, equity_curve, initial_capital in asset_curves
    )
    return combine_fixed_weight_equity_curves(
        components,
        expected_timestamps=expected_timestamps,
        evidence_id=f"fold {fold_index} equal-weight portfolio",
    )


def _prepare_joint_asset_panel(
    consolidated_bars_by_asset: Mapping[str, Sequence[Bar]],
    configs_by_asset: Mapping[str, BacktestConfig],
) -> _JointAssetPanel:
    if not consolidated_bars_by_asset:
        msg = "consolidated_bars_by_asset must not be empty"
        raise ValueError(msg)
    asset_order = tuple(sorted(consolidated_bars_by_asset))
    if set(configs_by_asset) != set(asset_order):
        msg = "configs_by_asset must exactly match consolidated_bars_by_asset"
        raise ValueError(msg)

    consolidated_by_asset: dict[str, list[Bar]] = {}
    reference_timestamps: tuple[datetime, ...] | None = None
    annualization_factor: float | None = None
    for asset in asset_order:
        config = configs_by_asset[asset]
        if config.fill_policy.timestamp_contract != "explicit_interval":
            msg = (
                "joint timestamp-anchored selection requires an explicit "
                f"execution timestamp contract for {asset}"
            )
            raise ValueError(msg)
        if annualization_factor is None:
            annualization_factor = config.annualization_factor
        elif config.annualization_factor != annualization_factor:
            msg = "joint assets must use one annualization_factor"
            raise ValueError(msg)
        normalized = [normalize_execution_bar(bar) for bar in consolidated_bars_by_asset[asset]]
        timestamps = tuple(bar.timestamp for bar in normalized)
        if reference_timestamps is None:
            reference_timestamps = timestamps
        elif timestamps != reference_timestamps:
            msg = "joint assets must have identical exact timestamp coverage"
            raise ValueError(msg)
        consolidated_by_asset[asset] = normalized

    assert reference_timestamps is not None
    assert annualization_factor is not None
    return _JointAssetPanel(
        asset_order=asset_order,
        consolidated_by_asset=consolidated_by_asset,
        configs_by_asset=configs_by_asset,
        timestamps=reference_timestamps,
        annualization_factor=annualization_factor,
    )


def _run_joint_asset_phase(
    strategy_factory: ParamStrategyFactory,
    params: Mapping[str, Any],
    panel: _JointAssetPanel,
    engine: BacktestEngine,
    *,
    fold: AnchoredFold,
    phase: str,
    indices: tuple[int, ...],
    evaluation_start: datetime,
    evaluation_end: datetime | None,
    expected_timestamps: tuple[datetime, ...],
    terminal_exit_cost_policy: TerminalExitCostPolicy | None,
) -> tuple[
    tuple[AssetBacktestReport, ...],
    tuple[tuple[str, tuple[tuple[datetime, float], ...], float], ...],
    tuple[AppliedTerminalExitCost, ...],
]:
    _require_contiguous_execution_indices(
        indices,
        evidence_id=f"fold {fold.index} {phase}",
    )
    asset_reports: list[AssetBacktestReport] = []
    adjusted_asset_curves: list[tuple[str, tuple[tuple[datetime, float], ...], float]] = []
    terminal_costs: list[AppliedTerminalExitCost] = []
    for asset in panel.asset_order:
        config = replace(
            panel.configs_by_asset[asset],
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
            require_flat_model_boundary=True,
        )
        report = engine.run_consolidated(
            strategy_factory(dict(params)),
            [panel.consolidated_by_asset[asset][index] for index in indices],
            config,
        )
        if not report.flat_model_boundary_applied:
            msg = (
                f"fold {fold.index} {phase} report for {asset} did not apply a flat model boundary"
            )
            raise ValueError(msg)
        adjusted_curve, adjustment = _validate_report_for_joint_evidence(
            report,
            asset=asset,
            fold_index=fold.index,
            phase=phase,
            expected_timestamps=expected_timestamps,
            terminal_exit_cost_policy=terminal_exit_cost_policy,
        )
        asset_reports.append(AssetBacktestReport(asset=asset, report=report))
        adjusted_asset_curves.append((asset, adjusted_curve, config.initial_capital))
        if adjustment is not None:
            terminal_costs.append(adjustment)
    return (
        tuple(asset_reports),
        tuple(adjusted_asset_curves),
        tuple(terminal_costs),
    )


def _require_contiguous_execution_indices(
    indices: tuple[int, ...],
    *,
    evidence_id: str,
) -> None:
    """Reject price-stream holes that would mutate indicator and model state.

    Purging applies to labels or to a contiguous terminal suffix.  Passing a
    stateful strategy a sparse subset of market bars creates a synthetic time
    series and is never an execution-faithful substitute for production.
    """

    if not indices:
        msg = f"{evidence_id} execution indices must not be empty"
        raise ValueError(msg)
    expected = tuple(range(indices[0], indices[-1] + 1))
    if indices != expected:
        msg = (
            f"{evidence_id} execution indices are non-contiguous; purging "
            "individual market bars would alter strategy state"
        )
        raise ValueError(msg)


def _score_joint_candidate(
    strategy_factory: ParamStrategyFactory,
    params: Mapping[str, Any],
    registry_index: int,
    panel: _JointAssetPanel,
    engine: BacktestEngine,
    *,
    fold: AnchoredFold,
    training_evaluation_start: datetime,
    training_expected_timestamps: tuple[datetime, ...],
    terminal_exit_cost_policy: TerminalExitCostPolicy | None,
) -> JointAssetTrainingScore:
    reports, adjusted_curves, terminal_costs = _run_joint_asset_phase(
        strategy_factory,
        params,
        panel,
        engine,
        fold=fold,
        phase="training",
        indices=fold.training_indices,
        evaluation_start=training_evaluation_start,
        evaluation_end=fold.train_end_exclusive_ts,
        expected_timestamps=training_expected_timestamps,
        terminal_exit_cost_policy=terminal_exit_cost_policy,
    )
    _, equal_weight_returns = _equal_weight_equity_series(
        adjusted_curves,
        fold_index=fold.index,
        expected_timestamps=training_expected_timestamps,
    )
    score = sharpe_ratio(
        equal_weight_returns,
        annualization_factor=panel.annualization_factor,
    )
    if not math.isfinite(score):
        msg = f"fold {fold.index} candidate {registry_index} score is nonfinite"
        raise ValueError(msg)
    return JointAssetTrainingScore(
        registry_index=registry_index,
        params=dict(params),
        annualized_sharpe=score,
        observations=len(equal_weight_returns),
        return_timestamps=training_expected_timestamps,
        equal_weight_daily_returns=equal_weight_returns,
        asset_reports=reports,
        terminal_exit_costs=terminal_costs,
    )


def _evaluate_joint_fold_oos(
    strategy_factory: ParamStrategyFactory,
    selected_params: Mapping[str, Any],
    panel: _JointAssetPanel,
    engine: BacktestEngine,
    *,
    fold: AnchoredFold,
    terminal_exit_cost_policy: TerminalExitCostPolicy | None,
) -> tuple[
    tuple[AssetBacktestReport, ...],
    tuple[AppliedTerminalExitCost, ...],
    FoldOOSSeries,
]:
    evaluation_indices = tuple(sorted({*fold.bootstrap_indices, *fold.test_indices}))
    reports, adjusted_curves, terminal_costs = _run_joint_asset_phase(
        strategy_factory,
        selected_params,
        panel,
        engine,
        fold=fold,
        phase="oos",
        indices=evaluation_indices,
        evaluation_start=fold.test_start_ts,
        evaluation_end=fold.test_end_exclusive_ts,
        expected_timestamps=fold.test_observation_timestamps,
        terminal_exit_cost_policy=terminal_exit_cost_policy,
    )
    equal_weight_equity, _ = _equal_weight_equity_series(
        adjusted_curves,
        fold_index=fold.index,
        expected_timestamps=fold.test_observation_timestamps,
    )
    return (
        reports,
        terminal_costs,
        FoldOOSSeries(
            fold_index=fold.index,
            expected_timestamps=fold.test_observation_timestamps,
            equity_curve=equal_weight_equity,
            initial_capital=1.0,
        ),
    )


def run_joint_asset_timestamp_anchored_walk_forward(
    strategy_factory: ParamStrategyFactory,
    candidate_params: Sequence[Mapping[str, Any]],
    consolidated_bars_by_asset: Mapping[str, Sequence[Bar]],
    configs_by_asset: Mapping[str, BacktestConfig],
    *,
    windows: Sequence[TimestampFoldWindow],
    training_evaluation_start: datetime,
    bootstrap_bars: int,
    training_terminal_exit_cost_policy: TerminalExitCostPolicy | None = None,
    oos_terminal_exit_cost_policy: TerminalExitCostPolicy | None = None,
    selected_arm_tie_tolerance: float = 1e-12,
    baseline_registry_index: int = 0,
    pre_oos_purge: timedelta = timedelta(days=60),
    embargo_bars: int = 30,
    event_intervals: Mapping[int, EventInterval] | None = None,
    engine: BacktestEngine | None = None,
) -> JointAssetAnchoredResult:
    """Select one parameter set on an exact, equal-weight multi-asset panel.

    Candidate score is the annualized Sharpe of exact-timestamp-aligned,
    equal-weight daily *net* returns.  Registry order is the deterministic tie
    breaker.  Every candidate is run on every asset using training data only;
    one failure invalidates the fold instead of silently shrinking the panel.

    Inputs are already validated provider-resolution bars and are never
    consolidated a second time.  Period-start provider bars are normalized
    once to decision-close timestamps.  Terminal positions remain untouched in
    each raw report.  A caller-supplied
    phase-specific policy must instead estimate the conservative hypothetical
    exit cost subtracted from the final observation used for selection or OOS
    pooling.  This prevents a flat next-fold reset from receiving a free exit.
    """

    if not candidate_params:
        msg = "candidate_params must not be empty"
        raise ValueError(msg)
    if bootstrap_bars < 0:
        msg = "bootstrap_bars must not be negative"
        raise ValueError(msg)
    if not math.isfinite(selected_arm_tie_tolerance) or selected_arm_tie_tolerance < 0.0:
        msg = "selected_arm_tie_tolerance must be finite and non-negative"
        raise ValueError(msg)
    if isinstance(baseline_registry_index, bool) or not isinstance(baseline_registry_index, int):
        msg = "baseline_registry_index must be an integer"
        raise TypeError(msg)
    if not 0 <= baseline_registry_index < len(candidate_params):
        msg = "baseline_registry_index is outside the candidate registry"
        raise ValueError(msg)

    active_engine = engine or BacktestEngine()
    panel = _prepare_joint_asset_panel(
        consolidated_bars_by_asset,
        configs_by_asset,
    )
    normalized_training_start = _validate_training_evaluation_start(
        training_evaluation_start,
        panel.timestamps,
    )
    folds = build_timestamp_anchored_folds(
        panel.timestamps,
        windows=windows,
        bootstrap_bars=bootstrap_bars,
        pre_oos_purge=pre_oos_purge,
        embargo_bars=embargo_bars,
        event_intervals=event_intervals,
    )

    anchored_windows: list[JointAssetAnchoredWindow] = []
    fold_oos_series: list[FoldOOSSeries] = []
    for fold in folds:
        training_expected_timestamps = tuple(
            panel.timestamps[index]
            for index in fold.training_indices
            if normalized_training_start <= panel.timestamps[index] < fold.train_end_exclusive_ts
        )
        if not training_expected_timestamps:
            msg = f"fold {fold.index} has no scored training observations"
            raise ValueError(msg)

        training_ledger = [
            _score_joint_candidate(
                strategy_factory,
                raw_params,
                registry_index,
                panel,
                active_engine,
                fold=fold,
                training_evaluation_start=normalized_training_start,
                training_expected_timestamps=training_expected_timestamps,
                terminal_exit_cost_policy=training_terminal_exit_cost_policy,
            )
            for registry_index, raw_params in enumerate(candidate_params)
        ]

        best_score = max(item.annualized_sharpe for item in training_ledger)
        tied_scores = tuple(
            item
            for item in training_ledger
            if best_score - item.annualized_sharpe <= selected_arm_tie_tolerance
        )
        selected_score = min(
            tied_scores,
            key=lambda item: (
                item.registry_index != baseline_registry_index,
                item.registry_index,
            ),
        )
        selected_params = dict(selected_score.params)
        oos_reports, oos_terminal_costs, fold_series = _evaluate_joint_fold_oos(
            strategy_factory,
            selected_params,
            panel,
            active_engine,
            fold=fold,
            terminal_exit_cost_policy=oos_terminal_exit_cost_policy,
        )
        fold_oos_series.append(fold_series)
        anchored_windows.append(
            JointAssetAnchoredWindow(
                fold=fold,
                selected_registry_index=selected_score.registry_index,
                selected_params=selected_params,
                training_score_ledger=tuple(training_ledger),
                oos_asset_reports=oos_reports,
                oos_terminal_exit_costs=oos_terminal_costs,
                equal_weight_oos_series=fold_series,
            )
        )

    return JointAssetAnchoredResult(
        asset_order=panel.asset_order,
        training_evaluation_start=normalized_training_start,
        selected_arm_tie_tolerance=selected_arm_tie_tolerance,
        baseline_registry_index=baseline_registry_index,
        windows=tuple(anchored_windows),
        pooled_oos=pool_fold_oos_returns(
            fold_oos_series,
            annualization_factor=panel.annualization_factor,
        ),
    )
