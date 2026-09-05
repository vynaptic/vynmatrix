"""Deterministic, executable benchmarks for strategy-validation campaigns.

These comparators deliberately use the production ``PureSignalStrategy`` and
``BacktestEngine`` contracts.  Decisions are made only after a completed bar
and the engine executes them at the next tradable open.  No benchmark has a
private fill or accounting implementation.

The volatility-targeted comparator calibrates one fixed exposure from eligible
training bars.  It does not resize during evaluation, so evaluation prices can
never influence its allocation.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from itertools import pairwise
from typing import Final, NoReturn

from dev_cli.validation.backtest.engine import BacktestConfig, BacktestEngine, BacktestReport
from dev_cli.validation.backtest.execution import ExecutionCostModel
from dev_cli.validation.backtest.walk_forward import (
    AppliedTerminalExitCost,
    FixedWeightEquityComponent,
    FoldOOSSeries,
    PooledOOSResult,
    TerminalExitCostPolicy,
    combine_fixed_weight_equity_curves,
    pool_fold_oos_returns,
    prepare_report_equity_evidence,
)
from dev_cli.validation.evidence import canonical_json_bytes, evidence_sha256
from dev_cli.validation.position_sizing import SIZING_EVIDENCE_SCHEMA
from lib_data.bars import Bar
from lib_strategy.position_sizing import PositionSizingPolicy
from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy

_BENCHMARK_VERSION: Final = "executable-benchmark-v1"
_DECISION_TIMING: Final = "completed_bar_decision_next_tradable_open"
_TERMINAL_TREATMENT: Final = "mark_to_market_unliquidated"
_MIN_LOOKBACK_BARS: Final = 2
_SHA256_HEX_LENGTH: Final = 64


def _invalid(message: str) -> NoReturn:
    raise ValueError(message)


def _invalid_type(message: str) -> NoReturn:
    raise TypeError(message)


def _invalid_runtime(message: str) -> NoReturn:
    raise RuntimeError(message)


class BenchmarkKind(StrEnum):
    """Supported executable comparator families."""

    CASH = "cash"
    BUY_AND_HOLD = "buy_and_hold"
    SMA_LONG_CASH = "sma_long_cash"
    MOMENTUM_LONG_CASH = "momentum_long_cash"
    VOL_TARGET_BUY_AND_HOLD = "vol_target_buy_and_hold"


@dataclass(frozen=True, slots=True)
class VolatilityTargetConfig:
    """Pre-registered fixed-exposure volatility-target calibration settings."""

    maximum_lookback_bars: int
    minimum_observations: int
    target_annualized_volatility: float
    exposure_floor: float
    exposure_cap: float = 0.95

    def __post_init__(self) -> None:
        for name in ("maximum_lookback_bars", "minimum_observations"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                _invalid_type(f"{name} must be an integer")
            if value < _MIN_LOOKBACK_BARS:
                _invalid(f"{name} must be at least {_MIN_LOOKBACK_BARS}")
        if self.minimum_observations > self.maximum_lookback_bars:
            _invalid("minimum_observations must not exceed maximum_lookback_bars")
        for name in ("target_annualized_volatility", "exposure_floor", "exposure_cap"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0.0:
                _invalid(f"{name} must be finite and positive")
        if self.exposure_floor > self.exposure_cap:
            _invalid("exposure_floor must not exceed exposure_cap")
        if self.exposure_cap > 1.0:
            _invalid("exposure_cap must not exceed 1.0 (no leverage)")

    def to_manifest_dict(self) -> dict[str, object]:
        """Return exact, JSON-safe calibration semantics."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class VolatilityCalibration:
    """Immutable fixed exposure estimated only from the supplied training bars."""

    config: VolatilityTargetConfig
    annualization_factor: float
    eligible_training_bars: int
    observations_used: int
    training_start: datetime
    training_end: datetime
    trailing_start: datetime
    realized_annualized_volatility: float
    fixed_exposure: float
    training_data_hash: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.annualization_factor) or self.annualization_factor <= 0.0:
            _invalid("annualization_factor must be finite and positive")
        available_returns = self.eligible_training_bars - 1
        expected_observations = min(
            available_returns,
            self.config.maximum_lookback_bars,
        )
        if available_returns < self.config.minimum_observations:
            _invalid("eligible_training_bars does not meet minimum_observations")
        if self.observations_used != expected_observations:
            _invalid("observations_used does not match the registered trailing-window rule")
        for name, timestamp in (
            ("training_start", self.training_start),
            ("training_end", self.training_end),
            ("trailing_start", self.trailing_start),
        ):
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                _invalid(f"{name} must be timezone-aware")
        if not self.training_start <= self.trailing_start < self.training_end:
            _invalid("calibration timestamps are not chronological")
        if (
            not math.isfinite(self.realized_annualized_volatility)
            or self.realized_annualized_volatility < 0.0
        ):
            _invalid("realized_annualized_volatility must be finite and non-negative")
        if (
            not math.isfinite(self.fixed_exposure)
            or not self.config.exposure_floor <= self.fixed_exposure <= self.config.exposure_cap
        ):
            _invalid("fixed exposure must remain within the registered floor and cap")
        if len(self.training_data_hash) != _SHA256_HEX_LENGTH or any(
            char not in "0123456789abcdef" for char in self.training_data_hash
        ):
            _invalid("training_data_hash must be a lowercase SHA-256 digest")

    @property
    def calibration_id(self) -> str:
        """Stable content identifier for manifests and trial registries."""

        digest = evidence_sha256(self.to_manifest_dict(include_id=False))
        return f"vol-calibration-{digest[:20]}"

    def to_manifest_dict(self, *, include_id: bool = True) -> dict[str, object]:
        """Return the complete calibration record without evaluation data."""

        payload: dict[str, object] = {
            "method": "trailing_sample_std_of_log_returns",
            "config": self.config.to_manifest_dict(),
            "annualization_factor": self.annualization_factor,
            "eligible_training_bars": self.eligible_training_bars,
            "observations_used": self.observations_used,
            "training_start": self.training_start.isoformat(),
            "training_end": self.training_end.isoformat(),
            "trailing_start": self.trailing_start.isoformat(),
            "realized_annualized_volatility": self.realized_annualized_volatility,
            "fixed_exposure": self.fixed_exposure,
            "training_data_hash": self.training_data_hash,
            "uses_evaluation_data": False,
        }
        if include_id:
            payload["calibration_id"] = self.calibration_id
        return payload

    def to_fixed_position_sizing_policy(
        self,
        *,
        policy_id: str,
        maximum_position_pct: float,
        minimum_position_notional: float,
    ) -> PositionSizingPolicy:
        """Carry one training calibration unchanged through evaluation sizing.

        This is intentionally a fixed-percent policy. It does not invoke or
        claim equivalence to the execution engine's dynamic ``VOLATILITY``
        sizing method.
        """

        return PositionSizingPolicy.fixed_percent(
            policy_id=policy_id,
            fixed_pct=self.fixed_exposure,
            max_position_pct=maximum_position_pct,
            minimum_position_notional=minimum_position_notional,
        )


@dataclass(frozen=True, slots=True)
class BenchmarkDefinition:
    """Exact executable semantics for one benchmark comparator."""

    kind: BenchmarkKind
    exposure: float
    lookback_bars: int | None = None
    volatility_calibration: VolatilityCalibration | None = None
    version: str = _BENCHMARK_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.kind, BenchmarkKind):
            _invalid_type("kind must be a BenchmarkKind")
        if not self.version.strip():
            _invalid("version must not be blank")
        if not math.isfinite(self.exposure) or not 0.0 <= self.exposure <= 1.0:
            _invalid("exposure must be finite and in [0, 1]")

        requires_lookback = self.kind in {
            BenchmarkKind.SMA_LONG_CASH,
            BenchmarkKind.MOMENTUM_LONG_CASH,
        }
        if requires_lookback:
            if (
                isinstance(self.lookback_bars, bool)
                or not isinstance(self.lookback_bars, int)
                or self.lookback_bars < _MIN_LOOKBACK_BARS
            ):
                _invalid(
                    f"SMA and momentum benchmarks require lookback_bars >= {_MIN_LOOKBACK_BARS}"
                )
        elif self.lookback_bars is not None:
            _invalid(f"{self.kind.value} does not accept lookback_bars")

        if self.kind is BenchmarkKind.CASH:
            if self.exposure != 0.0:
                _invalid("cash benchmark exposure must be zero")
        elif self.exposure <= 0.0:
            _invalid("invested benchmarks require positive exposure")

        if self.kind is BenchmarkKind.VOL_TARGET_BUY_AND_HOLD:
            calibration = self.volatility_calibration
            if calibration is None:
                _invalid("vol-target benchmark requires a training calibration")
            if self.exposure != calibration.fixed_exposure:
                _invalid("vol-target exposure must equal its frozen calibration")
        elif self.volatility_calibration is not None:
            _invalid(f"{self.kind.value} does not accept a volatility calibration")

    @classmethod
    def cash(cls) -> BenchmarkDefinition:
        return cls(kind=BenchmarkKind.CASH, exposure=0.0)

    @classmethod
    def buy_and_hold(cls, *, exposure: float = 0.95) -> BenchmarkDefinition:
        return cls(kind=BenchmarkKind.BUY_AND_HOLD, exposure=exposure)

    @classmethod
    def sma_long_cash(
        cls,
        *,
        period: int = 30,
        exposure: float = 0.95,
    ) -> BenchmarkDefinition:
        return cls(
            kind=BenchmarkKind.SMA_LONG_CASH,
            exposure=exposure,
            lookback_bars=period,
        )

    @classmethod
    def momentum_long_cash(
        cls,
        *,
        lookback_bars: int = 90,
        exposure: float = 0.95,
    ) -> BenchmarkDefinition:
        return cls(
            kind=BenchmarkKind.MOMENTUM_LONG_CASH,
            exposure=exposure,
            lookback_bars=lookback_bars,
        )

    @classmethod
    def volatility_targeted_buy_and_hold(
        cls,
        calibration: VolatilityCalibration,
    ) -> BenchmarkDefinition:
        return cls(
            kind=BenchmarkKind.VOL_TARGET_BUY_AND_HOLD,
            exposure=calibration.fixed_exposure,
            volatility_calibration=calibration,
        )

    @property
    def benchmark_id(self) -> str:
        """Stable semantic identifier, independent of runtime UUIDs."""

        digest = evidence_sha256(self.to_manifest_dict(include_id=False))
        return f"benchmark-{self.kind.value}-{digest[:20]}"

    def to_manifest_dict(self, *, include_id: bool = True) -> dict[str, object]:
        """Return sufficient metadata to reproduce the comparator exactly."""

        payload: dict[str, object] = {
            "kind": self.kind.value,
            "version": self.version,
            "decision_timing": _DECISION_TIMING,
            "terminal_treatment": _TERMINAL_TREATMENT,
            "direction": "cash" if self.kind is BenchmarkKind.CASH else "long_cash",
            "exposure": self.exposure,
            "lookback_bars": self.lookback_bars,
            "volatility_calibration": (
                self.volatility_calibration.to_manifest_dict()
                if self.volatility_calibration is not None
                else None
            ),
        }
        if include_id:
            payload["benchmark_id"] = self.benchmark_id
        return payload


@dataclass(frozen=True, slots=True)
class BenchmarkRun:
    """One benchmark report tied to its immutable executable definition."""

    definition: BenchmarkDefinition
    report: BacktestReport

    @property
    def terminal_marked_to_market(self) -> bool:
        """Whether the run ended invested and was explicitly marked, not closed."""

        return self.report.terminal_position is not None

    def registration_metadata(self) -> dict[str, object]:
        """Return definition and engine policy fields for a trial manifest."""

        size_pct_execution_relevant = self.definition.kind is not BenchmarkKind.CASH
        return {
            **self.definition.to_manifest_dict(),
            "strategy_id": self.definition.benchmark_id,
            "execution_policy_id": self.report.execution_policy_id,
            "cost_scenario": self.report.cost_scenario,
            "backtest_config": _registration_config(
                self.report.config,
                size_pct_execution_relevant=size_pct_execution_relevant,
            ),
            "terminal_marked_to_market": self.terminal_marked_to_market,
            "terminal_position": (
                self.report.terminal_position.to_dict()
                if self.report.terminal_position is not None
                else None
            ),
        }


class BenchmarkPortfolioMode(StrEnum):
    """Terminal-accounting role for a fixed-weight benchmark portfolio."""

    FULL_PANEL = "full_panel"
    OOS_FOLD = "oos_fold"


@dataclass(frozen=True, slots=True)
class BenchmarkPortfolioComponent:
    """One weighted benchmark/report with an explicit expected symbol."""

    component_id: str
    expected_symbol: str
    weight: float
    source: BenchmarkRun | BacktestReport = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.component_id, str) or not self.component_id.strip():
            _invalid("benchmark portfolio component_id must not be blank")
        if not isinstance(self.expected_symbol, str) or not self.expected_symbol.strip():
            _invalid("benchmark portfolio expected_symbol must not be blank")
        if isinstance(self.weight, bool) or not math.isfinite(self.weight) or self.weight <= 0.0:
            _invalid("benchmark portfolio component weight must be finite and positive")
        if not isinstance(self.source, (BenchmarkRun, BacktestReport)):
            _invalid_type("benchmark portfolio source must be BenchmarkRun or BacktestReport")
        if self.report.config.symbol != self.expected_symbol:
            _invalid(
                f"benchmark portfolio component {self.component_id} expected symbol "
                f"{self.expected_symbol}, observed {self.report.config.symbol}"
            )

    @property
    def report(self) -> BacktestReport:
        """Return the unmodified component report."""

        return self.source.report if isinstance(self.source, BenchmarkRun) else self.source

    def to_dict(self) -> dict[str, object]:
        """Return complete JSON-safe source evidence."""

        benchmark_definition = (
            self.source.definition.to_manifest_dict()
            if isinstance(self.source, BenchmarkRun)
            else None
        )
        return {
            "component_id": self.component_id,
            "expected_symbol": self.expected_symbol,
            "weight": self.weight,
            "source_kind": (
                "benchmark_run" if isinstance(self.source, BenchmarkRun) else "backtest_report"
            ),
            "benchmark_definition": benchmark_definition,
            "report": self.report.to_dict(),
            "equity_curve": [
                [timestamp.isoformat(), equity] for timestamp, equity in self.report.equity_curve
            ],
        }


@dataclass(frozen=True, slots=True)
class BenchmarkPortfolioMetrics:
    """Calculated portfolio metrics directly mappable to derived evidence."""

    observations: int
    annualization_factor: float
    initial_capital: float
    final_equity: float
    peak_equity: float
    total_return_pct: float
    annualized_volatility_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_pct: float
    total_component_trades: int
    total_component_signals: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BenchmarkPortfolioEvidence:
    """Immutable fixed-weight portfolio plus preserved component reports."""

    portfolio_id: str
    mode: BenchmarkPortfolioMode
    fold_index: int | None
    execution_policy_id: str
    cost_scenario: str
    expected_timestamps: tuple[datetime, ...]
    equity_curve: tuple[tuple[datetime, float], ...]
    daily_returns: tuple[float, ...]
    components: tuple[BenchmarkPortfolioComponent, ...]
    terminal_exit_costs: tuple[AppliedTerminalExitCost, ...]
    metrics: BenchmarkPortfolioMetrics
    weighting: str = field(default="fixed_weight_daily_rebalanced", init=False)

    @property
    def terminal_treatment(self) -> str:
        return (
            "mark_to_market_unliquidated"
            if self.mode is BenchmarkPortfolioMode.FULL_PANEL
            else "stressed_terminal_exit_cost_applied_where_open"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "portfolio_id": self.portfolio_id,
            "mode": self.mode.value,
            "fold_index": self.fold_index,
            "execution_policy_id": self.execution_policy_id,
            "cost_scenario": self.cost_scenario,
            "weighting": self.weighting,
            "terminal_treatment": self.terminal_treatment,
            "expected_timestamps": [
                timestamp.isoformat() for timestamp in self.expected_timestamps
            ],
            "equity_curve": [
                [timestamp.isoformat(), equity] for timestamp, equity in self.equity_curve
            ],
            "daily_returns": list(self.daily_returns),
            "components": [component.to_dict() for component in self.components],
            "terminal_exit_costs": [
                adjustment.to_dict() for adjustment in self.terminal_exit_costs
            ],
            "metrics": self.metrics.to_dict(),
        }


def aggregate_benchmark_portfolio(
    portfolio_id: str,
    components: Sequence[BenchmarkPortfolioComponent],
    *,
    expected_timestamps: Sequence[datetime],
    annualization_factor: float,
    mode: BenchmarkPortfolioMode,
    fold_index: int | None = None,
    terminal_exit_cost_policy: TerminalExitCostPolicy | None = None,
) -> BenchmarkPortfolioEvidence:
    """Aggregate aligned benchmark/report curves as a fixed daily mix."""

    if not isinstance(portfolio_id, str) or not portfolio_id.strip():
        _invalid("benchmark portfolio_id must not be blank")
    if not isinstance(mode, BenchmarkPortfolioMode):
        _invalid_type("benchmark portfolio mode must be a BenchmarkPortfolioMode")
    component_tuple = tuple(components)
    if not component_tuple:
        _invalid("benchmark portfolio requires at least one component")
    timestamp_tuple = tuple(expected_timestamps)
    if not math.isfinite(annualization_factor) or annualization_factor <= 0.0:
        _invalid("benchmark portfolio annualization_factor must be finite and positive")
    reference_execution_policy = component_tuple[0].report.execution_policy_id
    reference_cost_scenario = component_tuple[0].report.cost_scenario
    for component in component_tuple:
        report = component.report
        if report.config.annualization_factor != annualization_factor:
            _invalid("benchmark portfolio components must use the declared annualization_factor")
        if report.execution_policy_id != reference_execution_policy:
            _invalid("benchmark portfolio components must use one execution policy")
        if report.cost_scenario != reference_cost_scenario:
            _invalid("benchmark portfolio components must use one cost scenario")

    if mode is BenchmarkPortfolioMode.FULL_PANEL:
        if fold_index is not None:
            _invalid("full-panel benchmark portfolio must not declare a fold index")
        if terminal_exit_cost_policy is not None:
            _invalid("full-panel benchmark portfolio must remain unliquidated")
        liquidate_terminal = False
        phase = "full_panel"
    else:
        if isinstance(fold_index, bool) or not isinstance(fold_index, int) or fold_index < 0:
            _invalid("OOS benchmark portfolio requires a non-negative fold index")
        liquidate_terminal = True
        phase = "oos"

    adjusted_components: list[FixedWeightEquityComponent] = []
    terminal_costs: list[AppliedTerminalExitCost] = []
    for component in component_tuple:
        curve, adjustment = prepare_report_equity_evidence(
            component.report,
            expected_symbol=component.expected_symbol,
            evidence_id=component.component_id,
            fold_index=fold_index,
            phase=phase,
            expected_timestamps=timestamp_tuple,
            terminal_exit_cost_policy=terminal_exit_cost_policy,
            liquidate_terminal=liquidate_terminal,
            component_id=component.component_id,
        )
        adjusted_components.append(
            FixedWeightEquityComponent(
                component_id=component.component_id,
                equity_curve=curve,
                initial_capital=component.report.config.initial_capital,
                weight=component.weight,
            )
        )
        if adjustment is not None:
            terminal_costs.append(adjustment)

    equity_curve, _ = combine_fixed_weight_equity_curves(
        adjusted_components,
        expected_timestamps=timestamp_tuple,
        evidence_id=portfolio_id,
    )
    summary = pool_fold_oos_returns(
        [
            FoldOOSSeries(
                fold_index=fold_index if fold_index is not None else 0,
                expected_timestamps=timestamp_tuple,
                equity_curve=equity_curve,
                initial_capital=1.0,
            )
        ],
        annualization_factor=annualization_factor,
    )
    pooled_metrics = summary.metrics
    metrics = BenchmarkPortfolioMetrics(
        observations=pooled_metrics.observations,
        annualization_factor=annualization_factor,
        initial_capital=1.0,
        final_equity=equity_curve[-1][1],
        peak_equity=max(equity for _timestamp, equity in equity_curve),
        total_return_pct=pooled_metrics.compounded_return_pct,
        annualized_volatility_pct=pooled_metrics.annualized_volatility_pct,
        sharpe_ratio=pooled_metrics.sharpe_ratio,
        sortino_ratio=pooled_metrics.sortino_ratio,
        max_drawdown_pct=pooled_metrics.max_drawdown_pct,
        total_component_trades=sum(
            component.report.metrics.total_trades for component in component_tuple
        ),
        total_component_signals=sum(
            component.report.signals_emitted for component in component_tuple
        ),
    )
    evidence = BenchmarkPortfolioEvidence(
        portfolio_id=portfolio_id,
        mode=mode,
        fold_index=fold_index,
        execution_policy_id=reference_execution_policy,
        cost_scenario=reference_cost_scenario,
        expected_timestamps=timestamp_tuple,
        equity_curve=equity_curve,
        daily_returns=tuple(observation.daily_return for observation in summary.observations),
        components=component_tuple,
        terminal_exit_costs=tuple(terminal_costs),
        metrics=metrics,
    )
    canonical_json_bytes(evidence.to_dict())
    return evidence


def pool_benchmark_portfolio_folds(
    folds: Sequence[BenchmarkPortfolioEvidence],
    *,
    annualization_factor: float,
) -> PooledOOSResult:
    """Pool chronological non-overlapping OOS portfolio folds once."""

    fold_tuple = tuple(folds)
    if not fold_tuple:
        _invalid("benchmark portfolio fold pooling requires at least one fold")
    reference = fold_tuple[0]
    if reference.mode is not BenchmarkPortfolioMode.OOS_FOLD:
        _invalid("benchmark portfolio pooling accepts OOS folds only")
    reference_composition = tuple(
        (component.component_id, component.expected_symbol, component.weight)
        for component in reference.components
    )
    series: list[FoldOOSSeries] = []
    for fold in fold_tuple:
        if fold.mode is not BenchmarkPortfolioMode.OOS_FOLD or fold.fold_index is None:
            _invalid("benchmark portfolio pooling accepts OOS folds only")
        if fold.portfolio_id != reference.portfolio_id:
            _invalid("benchmark portfolio fold IDs do not identify one portfolio")
        if fold.execution_policy_id != reference.execution_policy_id:
            _invalid("benchmark portfolio execution policy differs across folds")
        if fold.cost_scenario != reference.cost_scenario:
            _invalid("benchmark portfolio cost scenario differs across folds")
        if fold.metrics.annualization_factor != annualization_factor:
            _invalid("benchmark portfolio annualization_factor differs across folds")
        composition = tuple(
            (component.component_id, component.expected_symbol, component.weight)
            for component in fold.components
        )
        if composition != reference_composition:
            _invalid("benchmark portfolio weights or components differ across folds")
        series.append(
            FoldOOSSeries(
                fold_index=fold.fold_index,
                expected_timestamps=fold.expected_timestamps,
                equity_curve=fold.equity_curve,
                initial_capital=1.0,
            )
        )
    return pool_fold_oos_returns(
        series,
        annualization_factor=annualization_factor,
    )


def calibrate_fixed_volatility_exposure(
    eligible_training_bars: list[Bar],
    config: VolatilityTargetConfig,
    *,
    annualization_factor: float,
) -> VolatilityCalibration:
    """Calibrate a no-leverage exposure from trailing eligible training data.

    Only ``eligible_training_bars`` are accepted by this boundary.  The number
    of log returns is ``min(available, maximum_lookback_bars)`` and the run
    fails closed below ``minimum_observations``.  The fixed, floor/cap-clamped
    result can then be carried unchanged through OOS.
    """

    if not math.isfinite(annualization_factor) or annualization_factor <= 0.0:
        _invalid("annualization_factor must be finite and positive")
    available_returns = len(eligible_training_bars) - 1
    if available_returns < config.minimum_observations:
        _invalid(
            "insufficient eligible training returns: "
            f"required {config.minimum_observations}, observed {max(0, available_returns)}"
        )
    observations_used = min(available_returns, config.maximum_lookback_bars)
    required_prices = observations_used + 1

    previous_ts: datetime | None = None
    for index, bar in enumerate(eligible_training_bars):
        timestamp = bar.timestamp
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            _invalid(f"eligible_training_bars[{index}].timestamp must be aware")
        if previous_ts is not None and timestamp <= previous_ts:
            _invalid("eligible training bars must be strictly chronological")
        previous_ts = timestamp
        if not math.isfinite(bar.close) or bar.close <= 0.0:
            _invalid(f"eligible_training_bars[{index}].close must be positive")

    trailing = eligible_training_bars[-required_prices:]
    returns = [math.log(current.close / previous.close) for previous, current in pairwise(trailing)]
    realized = statistics.stdev(returns) * math.sqrt(annualization_factor)
    if not math.isfinite(realized) or realized < 0.0:
        _invalid_runtime("volatility calibration produced invalid realized volatility")
    unconstrained_exposure = (
        config.exposure_cap if realized == 0.0 else config.target_annualized_volatility / realized
    )
    fixed_exposure = max(
        config.exposure_floor,
        min(config.exposure_cap, unconstrained_exposure),
    )
    if not math.isfinite(fixed_exposure) or not 0.0 < fixed_exposure <= 1.0:
        _invalid_runtime("volatility calibration produced an invalid exposure")

    training_hash_payload = [
        {
            "timestamp": bar.timestamp.isoformat(),
            "close": format(bar.close, ".17g"),
        }
        for bar in trailing
    ]
    return VolatilityCalibration(
        config=config,
        annualization_factor=float(annualization_factor),
        eligible_training_bars=len(eligible_training_bars),
        observations_used=len(returns),
        training_start=eligible_training_bars[0].timestamp,
        training_end=eligible_training_bars[-1].timestamp,
        trailing_start=trailing[0].timestamp,
        realized_annualized_volatility=realized,
        fixed_exposure=fixed_exposure,
        training_data_hash=evidence_sha256(training_hash_payload),
    )


class _BenchmarkStrategy(PureSignalStrategy):
    """Shared long/cash transition and flat-boundary behavior."""

    def __init__(self, definition: BenchmarkDefinition) -> None:
        super().__init__(
            strategy_id=definition.benchmark_id,
            strategy_type="indicator",
            config={"strategy_version": definition.version, "signal_source": "backtest"},
        )
        self.definition = definition
        if definition.lookback_bars is not None:
            self.warmup_bars_needed = definition.lookback_bars

    @property
    def supports_flat_model_boundary(self) -> bool:
        return True

    def initialize(self) -> None:
        self._positions: dict[str, int] = {}
        self._initialize_rule()

    def _initialize_rule(self) -> None:
        """Initialize comparator-specific indicator state."""

    def reset_model_to_flat_for_evaluation(self, symbols: tuple[str, ...]) -> None:
        for symbol in symbols:
            self._positions[symbol] = 0

    def _set_long_cash_target(
        self,
        state: MarketState,
        *,
        should_be_long: bool,
        reason: str,
    ) -> None:
        desired = 1 if should_be_long else 0
        current = self._positions.get(state.symbol, 0)
        if current == desired:
            return
        if not self._bootstrapping:
            metadata = {
                "benchmark_id": self.definition.benchmark_id,
                "benchmark_kind": self.definition.kind.value,
                "decision_rule": reason,
                "decision_timing": _DECISION_TIMING,
                "terminal_treatment": _TERMINAL_TREATMENT,
            }
            if desired == 1:
                self.emit_long(
                    state.symbol,
                    entry_price=state.close,
                    timestamp=state.timestamp,
                    size_hint=self.definition.exposure,
                    metadata=metadata,
                )
            else:
                self.emit_close(
                    state.symbol,
                    exit_price=state.close,
                    timestamp=state.timestamp,
                    reason=reason,
                    metadata=metadata,
                )
        self._positions[state.symbol] = desired


class CashBenchmarkStrategy(_BenchmarkStrategy):
    """Always-flat cash comparator."""

    def on_data(self, state: MarketState) -> None:
        self._positions.setdefault(state.symbol, 0)


class BuyAndHoldBenchmarkStrategy(_BenchmarkStrategy):
    """Enter long after the first eligible completed bar and retain exposure."""

    def on_data(self, state: MarketState) -> None:
        self._set_long_cash_target(
            state,
            should_be_long=True,
            reason="first_eligible_completed_bar",
        )


class SmaLongCashBenchmarkStrategy(_BenchmarkStrategy):
    """Long when completed close is strictly above its trailing simple average."""

    def _initialize_rule(self) -> None:
        assert self.definition.lookback_bars is not None
        self._closes: dict[str, deque[float]] = {}

    def on_data(self, state: MarketState) -> None:
        assert self.definition.lookback_bars is not None
        closes = self._closes.setdefault(
            state.symbol,
            deque(maxlen=self.definition.lookback_bars),
        )
        closes.append(state.close)
        if len(closes) < self.definition.lookback_bars:
            return
        average = sum(closes) / len(closes)
        self._set_long_cash_target(
            state,
            should_be_long=state.close > average,
            reason=f"close_strictly_above_sma_{self.definition.lookback_bars}",
        )


class MomentumLongCashBenchmarkStrategy(_BenchmarkStrategy):
    """Long when the completed close has a positive trailing bar return."""

    def _initialize_rule(self) -> None:
        assert self.definition.lookback_bars is not None
        self._closes: dict[str, deque[float]] = {}

    def on_data(self, state: MarketState) -> None:
        assert self.definition.lookback_bars is not None
        closes = self._closes.setdefault(
            state.symbol,
            deque(maxlen=self.definition.lookback_bars + 1),
        )
        closes.append(state.close)
        if len(closes) < self.definition.lookback_bars + 1:
            return
        self._set_long_cash_target(
            state,
            should_be_long=closes[-1] > closes[0],
            reason=f"positive_{self.definition.lookback_bars}_bar_momentum",
        )


def build_benchmark_strategy(definition: BenchmarkDefinition) -> PureSignalStrategy:
    """Build the canonical signal core for a registered benchmark definition."""

    if definition.kind is BenchmarkKind.CASH:
        return CashBenchmarkStrategy(definition)
    if definition.kind in {
        BenchmarkKind.BUY_AND_HOLD,
        BenchmarkKind.VOL_TARGET_BUY_AND_HOLD,
    }:
        return BuyAndHoldBenchmarkStrategy(definition)
    if definition.kind is BenchmarkKind.SMA_LONG_CASH:
        return SmaLongCashBenchmarkStrategy(definition)
    if definition.kind is BenchmarkKind.MOMENTUM_LONG_CASH:
        return MomentumLongCashBenchmarkStrategy(definition)
    _invalid(f"unsupported benchmark kind: {definition.kind!r}")


def run_consolidated_benchmark(
    definition: BenchmarkDefinition,
    consolidated_bars: list[Bar],
    config: BacktestConfig,
    *,
    engine: BacktestEngine | None = None,
) -> BenchmarkRun:
    """Execute over prevalidated bars while retaining engine accounting/fills."""

    effective_exposure = definition.exposure or config.size_pct
    effective_config = replace(config, size_pct=effective_exposure)
    report = (engine or BacktestEngine()).run_consolidated(
        build_benchmark_strategy(definition),
        consolidated_bars,
        effective_config,
    )
    return BenchmarkRun(definition=definition, report=report)


def _registration_config(
    config: BacktestConfig,
    *,
    size_pct_execution_relevant: bool,
) -> dict[str, object]:
    effective_costs = config.execution_costs or ExecutionCostModel.flat_fee(config.fee_bps)
    return {
        "symbol": config.symbol,
        "consolidation_minutes": config.consolidation_minutes,
        "initial_capital": config.initial_capital,
        "size_pct": config.size_pct,
        "sizing_evidence_schema": SIZING_EVIDENCE_SCHEMA,
        "sizing_policy": (
            config.sizing_policy.to_dict() if config.sizing_policy is not None else None
        ),
        "coinbase_sizing_rules": (
            config.coinbase_sizing_rules.to_dict()
            if config.coinbase_sizing_rules is not None
            else None
        ),
        "size_pct_execution_relevant": size_pct_execution_relevant,
        "size_pct_note": (
            "position-sizing input"
            if size_pct_execution_relevant
            else (
                "execution-irrelevant for always-flat cash; "
                "BacktestConfig requires a positive value"
            )
        ),
        "annualization_factor": config.annualization_factor,
        "execution_costs": asdict(effective_costs),
        "fill_policy": asdict(config.fill_policy),
        "evaluation_start": (
            config.evaluation_start.isoformat() if config.evaluation_start is not None else None
        ),
        "evaluation_end": (
            config.evaluation_end.isoformat() if config.evaluation_end is not None else None
        ),
        "min_bar_coverage": config.min_bar_coverage,
        "price_source": config.price_source,
        "asset_class": config.asset_class,
        "requested_product": config.requested_product,
        "canonical_product": config.canonical_product,
        "require_flat_model_boundary": config.require_flat_model_boundary,
    }


__all__ = [
    "BenchmarkDefinition",
    "BenchmarkKind",
    "BenchmarkRun",
    "BuyAndHoldBenchmarkStrategy",
    "CashBenchmarkStrategy",
    "MomentumLongCashBenchmarkStrategy",
    "SmaLongCashBenchmarkStrategy",
    "VolatilityCalibration",
    "VolatilityTargetConfig",
    "build_benchmark_strategy",
    "calibrate_fixed_volatility_exposure",
    "run_consolidated_benchmark",
]
