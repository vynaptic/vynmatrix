"""Evidence-complete diagnostics for synchronized US-equity portfolio validation.

The module composes the shared metric, bootstrap, Sharpe-deflation,
contribution, and exact-calendar CSCV/PBO implementations.  It does not acquire
data, infer missing lineage, select candidates, or make a promotion decision.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import StrEnum
from itertools import pairwise
from typing import TYPE_CHECKING

from dev_cli.validation._diagnostic_common import (
    invalid,
    require_finite,
    require_nonempty,
    require_utc,
)
from dev_cli.validation.backtest.equity_portfolio_types import EQUITY_TURNOVER_POLICY_ID
from dev_cli.validation.backtest.metrics import BacktestMetrics, compute_metrics
from dev_cli.validation.backtest.statistics import (
    BootstrapConfidenceInterval,
    SharpeDiagnostics,
    block_bootstrap_confidence_interval,
    sharpe_diagnostics,
)
from dev_cli.validation.contribution_diagnostics import (
    AttributedContribution,
    ContributionRobustnessSummary,
    TradeContribution,
    summarize_contribution_robustness,
)
from dev_cli.validation.cscv import (
    CalendarYearCSCVPBOResult,
    CandidateDailyReturnSeries,
    build_exact_calendar_cscv_pbo,
)
from lib_common.hashing import canonical_json_hash

if TYPE_CHECKING:
    from dev_cli.validation.backtest.equity_portfolio import BacktestResult

_MIN_CURVE_POINTS = 2
_PERCENT_SCALE = 100.0
_SECONDS_PER_DAY = 86_400.0


class AccountingView(StrEnum):
    """Accounting view represented by one equity curve."""

    GROSS = "gross"
    NET = "net"


class BenchmarkRole(StrEnum):
    """Mandatory comparison roles."""

    SPY_TOTAL_RETURN = "spy_total_return"
    SP500_EQUAL_WEIGHT = "sp500_equal_weight"


class BenchmarkSourceKind(StrEnum):
    """Exact source or explicitly non-official equal-weight approximation."""

    SPY_ADJUSTED_SECURITY = "spy_adjusted_security"
    OFFICIAL_SP500_EQUAL_WEIGHT_TOTAL_RETURN_INDEX = (
        "official_sp500_equal_weight_total_return_index"
    )
    TRADABLE_EQUAL_WEIGHT_ETF_APPROXIMATION = "tradable_equal_weight_etf_approximation"
    RECONSTRUCTED_EQUAL_WEIGHT_PORTFOLIO_APPROXIMATION = (
        "reconstructed_equal_weight_portfolio_approximation"
    )


@dataclass(frozen=True, slots=True)
class PerformanceSeriesEvidence:
    """One immutable, fully accounted equity series and its ledger inputs."""

    series_id: str
    label: str
    accounting: AccountingView
    equity_curve: tuple[tuple[datetime, float], ...]
    initial_capital: float
    trade_pnls: tuple[float, ...] = ()
    gross_trade_pnls: tuple[float, ...] | None = None
    turnover_notional: float = 0.0
    exposure_seconds: float = 0.0
    holding_periods_seconds: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        require_nonempty(self.series_id, field_name="series_id")
        require_nonempty(self.label, field_name="label")
        if not isinstance(self.accounting, AccountingView):
            invalid("accounting must be an AccountingView")
        if len(self.equity_curve) < _MIN_CURVE_POINTS:
            invalid("performance series requires at least two equity observations")
        if any(current[0] <= previous[0] for previous, current in pairwise(self.equity_curve)):
            invalid("performance-series timestamps must be strictly increasing")
        for _timestamp, value in self.equity_curve:
            require_finite(value, field_name="equity_curve value")
            if value <= 0.0:
                invalid("equity_curve values must be positive")
        require_finite(self.initial_capital, field_name="initial_capital")
        if self.initial_capital <= 0.0:
            invalid("initial_capital must be positive")

    def metrics(self, *, annualization_factor: float) -> BacktestMetrics:
        """Delegate summary calculation to the shared metrics owner."""

        return compute_metrics(
            list(self.equity_curve),
            list(self.trade_pnls),
            initial_capital=self.initial_capital,
            annualization_factor=annualization_factor,
            gross_trade_pnls=self.gross_trade_pnls,
            turnover_notional=self.turnover_notional,
            exposure_seconds=self.exposure_seconds,
            holding_periods_seconds=self.holding_periods_seconds,
        )


@dataclass(frozen=True, slots=True)
class StrategyPerformanceEvidence:
    """Gross and net accounting for the same strategy run."""

    gross: PerformanceSeriesEvidence
    net: PerformanceSeriesEvidence

    def __post_init__(self) -> None:
        if self.gross.accounting is not AccountingView.GROSS:
            invalid("strategy gross series must use gross accounting")
        if self.net.accounting is not AccountingView.NET:
            invalid("strategy net series must use net accounting")
        _require_aligned_curves(self.gross, self.net)


@dataclass(frozen=True, slots=True)
class BenchmarkEvidence:
    """Gross/net benchmark curves with construction, costs, and lineage."""

    benchmark_id: str
    label: str
    role: BenchmarkRole
    source_kind: BenchmarkSourceKind
    gross: PerformanceSeriesEvidence
    net: PerformanceSeriesEvidence
    construction: str
    cost_policy: str
    adjustment_policy: str
    source_observation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.benchmark_id, field_name="benchmark_id")
        require_nonempty(self.label, field_name="label")
        require_nonempty(self.construction, field_name="construction")
        require_nonempty(self.cost_policy, field_name="cost_policy")
        require_nonempty(self.adjustment_policy, field_name="adjustment_policy")
        if not isinstance(self.role, BenchmarkRole):
            invalid("benchmark role must be a BenchmarkRole")
        if not isinstance(self.source_kind, BenchmarkSourceKind):
            invalid("benchmark source_kind must be a BenchmarkSourceKind")
        if self.gross.accounting is not AccountingView.GROSS:
            invalid("benchmark gross series must use gross accounting")
        if self.net.accounting is not AccountingView.NET:
            invalid("benchmark net series must use net accounting")
        _require_aligned_curves(self.gross, self.net)
        if not self.source_observation_ids:
            invalid("benchmark source_observation_ids must not be empty")
        if any(not value.strip() for value in self.source_observation_ids):
            invalid("benchmark source observation ids must not be blank")
        if len(set(self.source_observation_ids)) != len(self.source_observation_ids):
            invalid("benchmark source observation ids must be unique")
        if (
            self.role is BenchmarkRole.SPY_TOTAL_RETURN
            and self.source_kind is not BenchmarkSourceKind.SPY_ADJUSTED_SECURITY
        ):
            invalid("SPY total-return role requires adjusted SPY security evidence")
        if (
            self.role is BenchmarkRole.SP500_EQUAL_WEIGHT
            and self.source_kind is BenchmarkSourceKind.SPY_ADJUSTED_SECURITY
        ):
            invalid("equal-weight role cannot use SPY adjusted-security evidence")

    @property
    def is_approximation(self) -> bool:
        return self.source_kind in {
            BenchmarkSourceKind.TRADABLE_EQUAL_WEIGHT_ETF_APPROXIMATION,
            BenchmarkSourceKind.RECONSTRUCTED_EQUAL_WEIGHT_PORTFOLIO_APPROXIMATION,
        }

    @property
    def display_label(self) -> str:
        suffix = " (approximation)" if self.is_approximation else ""
        return f"{self.label}{suffix}"

    def to_dict(self) -> dict[str, object]:
        return {
            "adjustment_policy": self.adjustment_policy,
            "benchmark_id": self.benchmark_id,
            "construction": self.construction,
            "cost_policy": self.cost_policy,
            "display_label": self.display_label,
            "is_approximation": self.is_approximation,
            "label": self.label,
            "role": self.role.value,
            "source_kind": self.source_kind.value,
            "source_observation_ids": list(self.source_observation_ids),
        }


@dataclass(frozen=True, slots=True)
class RollingWindow:
    """One preregistered observation-count rolling-return window."""

    window_id: str
    observations: int

    def __post_init__(self) -> None:
        require_nonempty(self.window_id, field_name="window_id")
        if (
            isinstance(self.observations, bool)
            or not isinstance(self.observations, int)
            or self.observations <= 0
        ):
            invalid("rolling-window observations must be a positive integer")


@dataclass(frozen=True, slots=True)
class PeriodReturn:
    """One calendar-period return."""

    series_id: str
    frequency: str
    period: str
    return_pct: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RollingReturn:
    """One exact observation-count rolling return."""

    series_id: str
    window_id: str
    observations: int
    start: datetime
    end: datetime
    return_pct: float

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["start"] = self.start.isoformat()
        payload["end"] = self.end.isoformat()
        return payload


@dataclass(frozen=True, slots=True)
class SeriesDiagnostics:
    """Shared metrics plus calendar and rolling return tables."""

    series_id: str
    label: str
    accounting: AccountingView
    metrics: BacktestMetrics
    monthly_returns: tuple[PeriodReturn, ...]
    annual_returns: tuple[PeriodReturn, ...]
    rolling_returns: tuple[RollingReturn, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "accounting": self.accounting.value,
            "annual_returns": [item.to_dict() for item in self.annual_returns],
            "label": self.label,
            "metrics": asdict(self.metrics),
            "monthly_returns": [item.to_dict() for item in self.monthly_returns],
            "rolling_returns": [item.to_dict() for item in self.rolling_returns],
            "series_id": self.series_id,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkComparison:
    """Aligned strategy/benchmark systematic-risk diagnostics."""

    accounting: AccountingView
    benchmark_id: str
    benchmark_label: str
    benchmark_is_approximation: bool
    aligned_daily_observations: int
    downside_months: int
    downside_capture_pct: float | None
    beta: float
    alpha_annualized_pct: float
    downside_capture_method: str = field(
        default="geometric_monthly_return_ratio_on_negative_benchmark_months",
        init=False,
    )
    beta_alpha_method: str = field(
        default="daily_simple_returns_ols_zero_risk_free_rate",
        init=False,
    )

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["accounting"] = self.accounting.value
        return payload


@dataclass(frozen=True, slots=True)
class BootstrapProtocol:
    """Seeded dependent-bootstrap assumptions registered before the run."""

    block_size: int
    samples: int
    confidence: float
    seed: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.block_size, bool)
            or not isinstance(self.block_size, int)
            or self.block_size <= 0
        ):
            invalid("bootstrap block_size must be a positive integer")
        if isinstance(self.samples, bool) or not isinstance(self.samples, int) or self.samples <= 0:
            invalid("bootstrap samples must be a positive integer")
        if not 0.0 < self.confidence < 1.0:
            invalid("bootstrap confidence must be in (0, 1)")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            invalid("bootstrap seed must be an integer")


@dataclass(frozen=True, slots=True)
class ReturnUncertainty:
    """Dependent-bootstrap intervals for net return and net SPY excess."""

    annualized_net_return_pct: BootstrapConfidenceInterval
    annualized_net_excess_vs_spy_pct: BootstrapConfidenceInterval
    statistic: str = field(default="annualized_arithmetic_daily_mean_pct", init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "annualized_net_excess_vs_spy_pct": asdict(self.annualized_net_excess_vs_spy_pct),
            "annualized_net_return_pct": asdict(self.annualized_net_return_pct),
            "statistic": self.statistic,
        }


@dataclass(frozen=True, slots=True)
class SharpeTrialEvidence:
    """Frozen cross-trial assumptions required by the shared DSR owner."""

    effective_trials: float
    trial_sharpe_std: float
    trial_sharpe_std_source: str
    benchmark_sharpe: float
    selection_contaminated: bool
    max_autocorrelation_lag: int | None = None

    def __post_init__(self) -> None:
        require_nonempty(
            self.trial_sharpe_std_source,
            field_name="trial_sharpe_std_source",
        )


@dataclass(frozen=True, slots=True)
class CSCVProtocol:
    """Exact six-year, preregistered candidate family for shared CSCV/PBO."""

    candidate_series: tuple[CandidateDailyReturnSeries, ...]
    registered_candidate_order: tuple[str, ...]
    calendar_years: tuple[int, ...]
    registered_calendar_dates: tuple[date, ...]
    annualization_factor: float


@dataclass(frozen=True, slots=True)
class FactorPnlContribution:
    """One factor's additive contribution to an observation's net P&L."""

    factor: str
    net_contribution: float

    def __post_init__(self) -> None:
        require_nonempty(self.factor, field_name="factor")
        require_finite(self.net_contribution, field_name="net_contribution")


@dataclass(frozen=True, slots=True)
class PortfolioPnlAttribution:
    """One lineaged, additive portfolio P&L observation."""

    contribution_id: str
    asset: str
    timestamp: datetime
    net_contribution: float
    sector: str
    regime: str
    factors: tuple[FactorPnlContribution, ...]
    source_observation_id: str

    def __post_init__(self) -> None:
        require_nonempty(self.contribution_id, field_name="contribution_id")
        require_nonempty(self.asset, field_name="asset")
        require_utc(self.timestamp, field_name="timestamp")
        require_finite(self.net_contribution, field_name="net_contribution")
        require_nonempty(self.sector, field_name="sector")
        require_nonempty(self.regime, field_name="regime")
        require_nonempty(
            self.source_observation_id,
            field_name="source_observation_id",
        )
        if not self.factors:
            invalid("P&L attribution requires factor contributions")
        names = tuple(item.factor for item in self.factors)
        if len(set(names)) != len(names):
            invalid("factor contribution names must be unique per observation")


@dataclass(frozen=True, slots=True)
class AttributionEvidence:
    """Complete additive attribution ledger and reconciliation policy."""

    observations: tuple[PortfolioPnlAttribution, ...]
    closed_trade_contributions: tuple[TradeContribution, ...]
    methodology_id: str
    reconciliation_tolerance: float

    def __post_init__(self) -> None:
        require_nonempty(self.methodology_id, field_name="methodology_id")
        require_finite(
            self.reconciliation_tolerance,
            field_name="reconciliation_tolerance",
        )
        if self.reconciliation_tolerance < 0.0:
            invalid("reconciliation_tolerance must be non-negative")
        if not self.observations:
            invalid("attribution observations must not be empty")
        if not self.closed_trade_contributions:
            invalid("closed_trade_contributions must not be empty")
        identities = tuple(item.contribution_id for item in self.observations)
        if len(set(identities)) != len(identities):
            invalid("attribution contribution ids must be unique")
        for observation in self.observations:
            factor_total = sum(item.net_contribution for item in observation.factors)
            if not math.isclose(
                factor_total,
                observation.net_contribution,
                rel_tol=0.0,
                abs_tol=self.reconciliation_tolerance,
            ):
                invalid(f"factor contributions do not reconcile for {observation.contribution_id}")


@dataclass(frozen=True, slots=True)
class AttributionBucket:
    """One additive attribution bucket."""

    dimension: str
    bucket: str
    net_contribution: float
    share_of_total: float | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AttributionSummary:
    """Factor, sector, regime, and calendar attribution plus shared removals."""

    methodology_id: str
    total_net_contribution: float
    buckets: tuple[AttributionBucket, ...]
    contribution_robustness: ContributionRobustnessSummary
    lineage_digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "buckets": [item.to_dict() for item in self.buckets],
            "contribution_robustness": self.contribution_robustness.to_dict(),
            "lineage_digest": self.lineage_digest,
            "methodology_id": self.methodology_id,
            "total_net_contribution": self.total_net_contribution,
        }


@dataclass(frozen=True, slots=True)
class LiquidityCapacityObservation:
    """Point-in-time dollar-ADV evidence for one model order."""

    order_id: str
    symbol: str
    decision_at: datetime
    available_at: datetime
    absolute_weight_change: float
    point_in_time_dollar_adv: float
    execution_sessions: int
    adv_window_sessions: int
    source_observation_id: str

    def __post_init__(self) -> None:
        require_nonempty(self.order_id, field_name="order_id")
        require_nonempty(self.symbol, field_name="symbol")
        require_utc(self.decision_at, field_name="decision_at")
        require_utc(self.available_at, field_name="available_at")
        if self.available_at > self.decision_at:
            invalid("capacity liquidity evidence was unavailable at decision time")
        require_finite(
            self.absolute_weight_change,
            field_name="absolute_weight_change",
        )
        if not 0.0 < self.absolute_weight_change <= 1.0:
            invalid("absolute_weight_change must be in (0, 1]")
        require_finite(
            self.point_in_time_dollar_adv,
            field_name="point_in_time_dollar_adv",
        )
        if self.point_in_time_dollar_adv <= 0.0:
            invalid("point_in_time_dollar_adv must be positive")
        for field_name, value in (
            ("execution_sessions", self.execution_sessions),
            ("adv_window_sessions", self.adv_window_sessions),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                invalid(f"{field_name} must be a positive integer")
        require_nonempty(
            self.source_observation_id,
            field_name="source_observation_id",
        )


@dataclass(frozen=True, slots=True)
class CapacityProtocol:
    """Preregistered AUM levels and participation ceiling."""

    aum_levels: tuple[float, ...]
    maximum_participation: float
    observations: tuple[LiquidityCapacityObservation, ...]

    def __post_init__(self) -> None:
        if not self.aum_levels:
            invalid("capacity aum_levels must not be empty")
        for value in self.aum_levels:
            require_finite(value, field_name="aum_level")
            if value <= 0.0:
                invalid("capacity AUM levels must be positive")
        if tuple(sorted(set(self.aum_levels))) != self.aum_levels:
            invalid("capacity AUM levels must be strictly increasing and unique")
        require_finite(
            self.maximum_participation,
            field_name="maximum_participation",
        )
        if not 0.0 < self.maximum_participation <= 1.0:
            invalid("maximum_participation must be in (0, 1]")
        if not self.observations:
            invalid("capacity observations must not be empty")
        order_ids = tuple(item.order_id for item in self.observations)
        if len(set(order_ids)) != len(order_ids):
            invalid("capacity order ids must be unique")


@dataclass(frozen=True, slots=True)
class CapacityPoint:
    """One point on the preregistered AUM capacity curve."""

    aum: float
    requested_notional: float
    maximum_observed_participation: float
    p95_observed_participation: float
    orders_above_limit: int
    feasible: bool
    bottleneck_order_id: str
    bottleneck_symbol: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CapacitySummary:
    """PIT-liquidity capacity curve without inferred or current ADV."""

    maximum_participation: float
    maximum_supported_aum: float
    observations: int
    curve: tuple[CapacityPoint, ...]
    lineage_digest: str
    method: str = field(
        default="absolute_target_weight_change_x_aum_over_pit_dollar_adv_x_sessions",
        init=False,
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "curve": [item.to_dict() for item in self.curve],
            "lineage_digest": self.lineage_digest,
            "maximum_participation": self.maximum_participation,
            "maximum_supported_aum": self.maximum_supported_aum,
            "method": self.method,
            "observations": self.observations,
        }


@dataclass(frozen=True, slots=True)
class EquityDiagnosticsEvidence:
    """All immutable external evidence required for a complete diagnostic."""

    annualization_factor: float
    spy: BenchmarkEvidence
    equal_weight: BenchmarkEvidence
    rolling_windows: tuple[RollingWindow, ...]
    bootstrap: BootstrapProtocol
    sharpe_trials: SharpeTrialEvidence
    cscv: CSCVProtocol
    attribution: AttributionEvidence
    capacity: CapacityProtocol

    def __post_init__(self) -> None:
        if self.spy.role is not BenchmarkRole.SPY_TOTAL_RETURN:
            invalid("spy evidence must use the SPY total-return role")
        if self.equal_weight.role is not BenchmarkRole.SP500_EQUAL_WEIGHT:
            invalid("equal-weight evidence must use the equal-weight role")
        if not self.rolling_windows:
            invalid("rolling_windows must not be empty")
        window_ids = tuple(item.window_id for item in self.rolling_windows)
        if len(set(window_ids)) != len(window_ids):
            invalid("rolling-window ids must be unique")
        require_finite(
            self.annualization_factor,
            field_name="annualization_factor",
        )
        if self.annualization_factor <= 0.0:
            invalid("annualization_factor must be positive")


@dataclass(frozen=True, slots=True)
class EquityDiagnosticsResult:
    """Complete reusable diagnostics for one synchronized portfolio run."""

    series: tuple[SeriesDiagnostics, ...]
    benchmarks: tuple[BenchmarkEvidence, ...]
    comparisons: tuple[BenchmarkComparison, ...]
    return_uncertainty: ReturnUncertainty
    sharpe_diagnostics: SharpeDiagnostics
    cscv_pbo: CalendarYearCSCVPBOResult
    attribution: AttributionSummary
    capacity: CapacitySummary
    evidence_role: str = field(
        default="retrospective_validation_diagnostic",
        init=False,
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "attribution": self.attribution.to_dict(),
            "benchmarks": [item.to_dict() for item in self.benchmarks],
            "capacity": self.capacity.to_dict(),
            "comparisons": [item.to_dict() for item in self.comparisons],
            "cscv_pbo": self.cscv_pbo.to_dict(),
            "evidence_role": self.evidence_role,
            "return_uncertainty": self.return_uncertainty.to_dict(),
            "series": [item.to_dict() for item in self.series],
            "sharpe_diagnostics": asdict(self.sharpe_diagnostics),
        }


def _require_aligned_curves(
    left: PerformanceSeriesEvidence,
    right: PerformanceSeriesEvidence,
) -> None:
    left_dates = tuple(timestamp.date() for timestamp, _value in left.equity_curve)
    right_dates = tuple(timestamp.date() for timestamp, _value in right.equity_curve)
    if left_dates != right_dates:
        invalid(f"series {left.series_id} and {right.series_id} are not session-aligned")


def _simple_returns(
    series: PerformanceSeriesEvidence,
) -> tuple[float, ...]:
    return tuple(
        current[1] / previous[1] - 1.0 for previous, current in pairwise(series.equity_curve)
    )


def _period_returns(
    series: PerformanceSeriesEvidence,
    *,
    frequency: str,
) -> tuple[PeriodReturn, ...]:
    if frequency not in {"monthly", "annual"}:
        invalid("period-return frequency must be monthly or annual")
    bases: dict[str, float] = {}
    ending: dict[str, float] = {}
    previous_value = series.initial_capital
    for timestamp, value in series.equity_curve:
        period = timestamp.strftime("%Y-%m") if frequency == "monthly" else str(timestamp.year)
        bases.setdefault(period, previous_value)
        ending[period] = value
        previous_value = value
    return tuple(
        PeriodReturn(
            series_id=series.series_id,
            frequency=frequency,
            period=period,
            return_pct=(ending[period] / base - 1.0) * _PERCENT_SCALE,
        )
        for period, base in bases.items()
    )


def _rolling_returns(
    series: PerformanceSeriesEvidence,
    windows: Sequence[RollingWindow],
) -> tuple[RollingReturn, ...]:
    rows: list[RollingReturn] = []
    for window in windows:
        if window.observations >= len(series.equity_curve):
            invalid(f"series {series.series_id} has no complete {window.window_id} rolling window")
        for index in range(window.observations, len(series.equity_curve)):
            start = series.equity_curve[index - window.observations]
            end = series.equity_curve[index]
            rows.append(
                RollingReturn(
                    series_id=series.series_id,
                    window_id=window.window_id,
                    observations=window.observations,
                    start=start[0],
                    end=end[0],
                    return_pct=(end[1] / start[1] - 1.0) * _PERCENT_SCALE,
                )
            )
    return tuple(rows)


def _series_diagnostics(
    series: PerformanceSeriesEvidence,
    *,
    annualization_factor: float,
    windows: Sequence[RollingWindow],
) -> SeriesDiagnostics:
    return SeriesDiagnostics(
        series_id=series.series_id,
        label=series.label,
        accounting=series.accounting,
        metrics=series.metrics(annualization_factor=annualization_factor),
        monthly_returns=_period_returns(series, frequency="monthly"),
        annual_returns=_period_returns(series, frequency="annual"),
        rolling_returns=_rolling_returns(series, windows),
    )


def _geometric_average(values: Sequence[float]) -> float:
    if not values:
        invalid("geometric average requires observations")
    product = 1.0
    for value in values:
        if value <= -1.0:
            invalid("simple returns must be greater than -100 percent")
        product *= 1.0 + value
    return float(product ** (1.0 / len(values)) - 1.0)


def _benchmark_comparison(
    strategy: PerformanceSeriesEvidence,
    benchmark: BenchmarkEvidence,
    benchmark_series: PerformanceSeriesEvidence,
    *,
    annualization_factor: float,
) -> BenchmarkComparison:
    _require_aligned_curves(strategy, benchmark_series)
    strategy_returns = _simple_returns(strategy)
    benchmark_returns = _simple_returns(benchmark_series)
    strategy_mean = sum(strategy_returns) / len(strategy_returns)
    benchmark_mean = sum(benchmark_returns) / len(benchmark_returns)
    covariance = sum(
        (strategy_return - strategy_mean) * (benchmark_return - benchmark_mean)
        for strategy_return, benchmark_return in zip(
            strategy_returns,
            benchmark_returns,
            strict=True,
        )
    ) / len(strategy_returns)
    benchmark_variance = sum(
        (benchmark_return - benchmark_mean) ** 2 for benchmark_return in benchmark_returns
    ) / len(benchmark_returns)
    beta = covariance / benchmark_variance if benchmark_variance > 0.0 else 0.0
    alpha = (strategy_mean - beta * benchmark_mean) * annualization_factor * _PERCENT_SCALE

    strategy_monthly = {
        item.period: item.return_pct / _PERCENT_SCALE
        for item in _period_returns(strategy, frequency="monthly")
    }
    benchmark_monthly = {
        item.period: item.return_pct / _PERCENT_SCALE
        for item in _period_returns(benchmark_series, frequency="monthly")
    }
    if tuple(strategy_monthly) != tuple(benchmark_monthly):
        invalid("strategy and benchmark monthly return periods are not aligned")
    downside_periods = tuple(period for period, value in benchmark_monthly.items() if value < 0.0)
    downside_capture: float | None = None
    if downside_periods:
        benchmark_downside = _geometric_average(
            [benchmark_monthly[period] for period in downside_periods]
        )
        strategy_downside = _geometric_average(
            [strategy_monthly[period] for period in downside_periods]
        )
        if benchmark_downside != 0.0:
            downside_capture = strategy_downside / benchmark_downside * _PERCENT_SCALE
    return BenchmarkComparison(
        accounting=strategy.accounting,
        benchmark_id=benchmark.benchmark_id,
        benchmark_label=benchmark.display_label,
        benchmark_is_approximation=benchmark.is_approximation,
        aligned_daily_observations=len(strategy_returns),
        downside_months=len(downside_periods),
        downside_capture_pct=downside_capture,
        beta=beta,
        alpha_annualized_pct=alpha,
    )


def _annualized_mean_pct(
    values: Sequence[float],
    annualization_factor: float,
) -> float:
    return sum(values) / len(values) * annualization_factor * _PERCENT_SCALE if values else 0.0


def _return_uncertainty(
    strategy_net: PerformanceSeriesEvidence,
    spy_net: PerformanceSeriesEvidence,
    protocol: BootstrapProtocol,
    *,
    annualization_factor: float,
) -> ReturnUncertainty:
    _require_aligned_curves(strategy_net, spy_net)
    net_returns = _simple_returns(strategy_net)
    spy_returns = _simple_returns(spy_net)
    excess_returns = tuple(
        strategy_return - spy_return
        for strategy_return, spy_return in zip(
            net_returns,
            spy_returns,
            strict=True,
        )
    )
    statistic = lambda values: _annualized_mean_pct(  # noqa: E731
        values,
        annualization_factor,
    )
    return ReturnUncertainty(
        annualized_net_return_pct=block_bootstrap_confidence_interval(
            net_returns,
            statistic,
            block_size=protocol.block_size,
            samples=protocol.samples,
            confidence=protocol.confidence,
            seed=protocol.seed,
        ),
        annualized_net_excess_vs_spy_pct=block_bootstrap_confidence_interval(
            excess_returns,
            statistic,
            block_size=protocol.block_size,
            samples=protocol.samples,
            confidence=protocol.confidence,
            seed=protocol.seed,
        ),
    )


def _attribution_summary(
    evidence: AttributionEvidence,
    *,
    expected_net_pnl: float,
) -> AttributionSummary:
    observations = tuple(
        sorted(
            evidence.observations,
            key=lambda item: (item.timestamp, item.contribution_id),
        )
    )
    total = sum(item.net_contribution for item in observations)
    if not math.isclose(
        total,
        expected_net_pnl,
        rel_tol=0.0,
        abs_tol=evidence.reconciliation_tolerance,
    ):
        invalid("attribution ledger does not reconcile to net portfolio P&L")

    grouped: dict[tuple[str, str], float] = defaultdict(float)
    for observation in observations:
        grouped[("sector", observation.sector)] += observation.net_contribution
        grouped[("regime", observation.regime)] += observation.net_contribution
        grouped[("calendar_month", observation.timestamp.strftime("%Y-%m"))] += (
            observation.net_contribution
        )
        grouped[("calendar_year", str(observation.timestamp.year))] += observation.net_contribution
        for factor in observation.factors:
            grouped[("factor", factor.factor)] += factor.net_contribution

    for dimension in (
        "factor",
        "sector",
        "regime",
        "calendar_month",
        "calendar_year",
    ):
        dimension_total = sum(
            contribution
            for (candidate_dimension, _bucket), contribution in grouped.items()
            if candidate_dimension == dimension
        )
        if not math.isclose(
            dimension_total,
            total,
            rel_tol=0.0,
            abs_tol=evidence.reconciliation_tolerance,
        ):
            invalid(f"{dimension} attribution does not reconcile to total P&L")

    attributed = tuple(
        AttributedContribution(
            contribution_id=item.contribution_id,
            asset=item.asset,
            timestamp=item.timestamp,
            net_contribution=item.net_contribution,
        )
        for item in observations
    )
    robustness = summarize_contribution_robustness(
        evidence.closed_trade_contributions,
        attributed_contributions=attributed,
    )
    buckets = tuple(
        AttributionBucket(
            dimension=dimension,
            bucket=bucket,
            net_contribution=value,
            share_of_total=value / total if total != 0.0 else None,
        )
        for (dimension, bucket), value in sorted(grouped.items())
    )
    lineage_digest = canonical_json_hash(
        {
            "methodology_id": evidence.methodology_id,
            "observations": [
                {
                    "contribution_id": item.contribution_id,
                    "source_observation_id": item.source_observation_id,
                }
                for item in observations
            ],
        }
    )
    return AttributionSummary(
        methodology_id=evidence.methodology_id,
        total_net_contribution=total,
        buckets=buckets,
        contribution_robustness=robustness,
        lineage_digest=lineage_digest,
    )


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _capacity_summary(protocol: CapacityProtocol) -> CapacitySummary:
    ordered = tuple(
        sorted(
            protocol.observations,
            key=lambda item: (item.decision_at, item.order_id),
        )
    )
    supported_aum = min(
        protocol.maximum_participation
        * item.point_in_time_dollar_adv
        * item.execution_sessions
        / item.absolute_weight_change
        for item in ordered
    )
    curve: list[CapacityPoint] = []
    for aum in protocol.aum_levels:
        participations = tuple(
            aum
            * item.absolute_weight_change
            / (item.point_in_time_dollar_adv * item.execution_sessions)
            for item in ordered
        )
        bottleneck_index = max(
            range(len(ordered)),
            key=lambda index: (participations[index], -index),
        )
        bottleneck = ordered[bottleneck_index]
        breaches = sum(value > protocol.maximum_participation for value in participations)
        curve.append(
            CapacityPoint(
                aum=aum,
                requested_notional=sum(aum * item.absolute_weight_change for item in ordered),
                maximum_observed_participation=participations[bottleneck_index],
                p95_observed_participation=_quantile(participations, 0.95),
                orders_above_limit=breaches,
                feasible=breaches == 0,
                bottleneck_order_id=bottleneck.order_id,
                bottleneck_symbol=bottleneck.symbol,
            )
        )
    lineage_digest = canonical_json_hash(
        {
            "maximum_participation": protocol.maximum_participation,
            "observations": [
                {
                    "adv_window_sessions": item.adv_window_sessions,
                    "available_at": item.available_at.isoformat(),
                    "decision_at": item.decision_at.isoformat(),
                    "order_id": item.order_id,
                    "source_observation_id": item.source_observation_id,
                }
                for item in ordered
            ],
        }
    )
    return CapacitySummary(
        maximum_participation=protocol.maximum_participation,
        maximum_supported_aum=supported_aum,
        observations=len(ordered),
        curve=tuple(curve),
        lineage_digest=lineage_digest,
    )


def build_equity_diagnostics(
    strategy: StrategyPerformanceEvidence,
    evidence: EquityDiagnosticsEvidence,
) -> EquityDiagnosticsResult:
    """Build the complete diagnostic set from immutable, pre-existing evidence."""

    all_series = (
        strategy.gross,
        strategy.net,
        evidence.spy.gross,
        evidence.spy.net,
        evidence.equal_weight.gross,
        evidence.equal_weight.net,
    )
    series_ids = tuple(item.series_id for item in all_series)
    if len(set(series_ids)) != len(series_ids):
        invalid("strategy and benchmark series ids must be unique")
    for benchmark_series in all_series[2:]:
        _require_aligned_curves(strategy.net, benchmark_series)

    net_pnl = strategy.net.equity_curve[-1][1] - strategy.net.initial_capital
    comparisons = tuple(
        _benchmark_comparison(
            strategy_series,
            benchmark,
            benchmark.gross
            if strategy_series.accounting is AccountingView.GROSS
            else benchmark.net,
            annualization_factor=evidence.annualization_factor,
        )
        for benchmark in (evidence.spy, evidence.equal_weight)
        for strategy_series in (strategy.gross, strategy.net)
    )
    return EquityDiagnosticsResult(
        series=tuple(
            _series_diagnostics(
                item,
                annualization_factor=evidence.annualization_factor,
                windows=evidence.rolling_windows,
            )
            for item in all_series
        ),
        benchmarks=(evidence.spy, evidence.equal_weight),
        comparisons=comparisons,
        return_uncertainty=_return_uncertainty(
            strategy.net,
            evidence.spy.net,
            evidence.bootstrap,
            annualization_factor=evidence.annualization_factor,
        ),
        sharpe_diagnostics=sharpe_diagnostics(
            _simple_returns(strategy.net),
            effective_trials=evidence.sharpe_trials.effective_trials,
            trial_sharpe_std=evidence.sharpe_trials.trial_sharpe_std,
            trial_sharpe_std_source=(evidence.sharpe_trials.trial_sharpe_std_source),
            annualization_factor=evidence.annualization_factor,
            benchmark_sharpe=evidence.sharpe_trials.benchmark_sharpe,
            selection_contaminated=(evidence.sharpe_trials.selection_contaminated),
            max_autocorrelation_lag=(evidence.sharpe_trials.max_autocorrelation_lag),
        ),
        cscv_pbo=build_exact_calendar_cscv_pbo(
            evidence.cscv.candidate_series,
            registered_candidate_order=(evidence.cscv.registered_candidate_order),
            calendar_years=evidence.cscv.calendar_years,
            annualization_factor=evidence.cscv.annualization_factor,
            registered_calendar_dates=(evidence.cscv.registered_calendar_dates),
        ),
        attribution=_attribution_summary(
            evidence.attribution,
            expected_net_pnl=net_pnl,
        ),
        capacity=_capacity_summary(evidence.capacity),
    )


def strategy_performance_evidence(
    result: BacktestResult,
    *,
    initial_capital: float,
) -> StrategyPerformanceEvidence:
    """Adapt the exact portfolio ledgers to the shared-metric input contract."""

    closed = tuple(trade for trade in result.trades if trade.exit_fill is not None)
    if len(closed) != len(result.trades):
        invalid("strategy trade ledger may contain only completed executions")
    if result.policies.get("turnover") != EQUITY_TURNOVER_POLICY_ID:
        invalid("strategy result does not declare the frozen two-way turnover policy")
    if len(result.daily_exposures) != len(result.equity_curve) or any(
        exposure.timestamp != curve_observation[0]
        for exposure, curve_observation in zip(
            result.daily_exposures,
            result.equity_curve,
            strict=True,
        )
    ):
        invalid("strategy daily exposure ledger does not align with its equity curve")
    gross_pnls: list[float] = []
    holding_periods: list[float] = []
    for trade in closed:
        if (
            trade.entry_reference is None
            or trade.exit_reference is None
            or trade.exit_session is None
        ):
            invalid("diagnostics require gross reference prices and exit sessions")
        gross_pnls.append((trade.exit_reference - trade.entry_reference) * trade.shares)
        holding_periods.append(trade.holding_sessions * _SECONDS_PER_DAY)

    turnover_notional = sum(
        abs(float(row["execution_price"]) * float(row["quantity"]))
        for row in result.cost_ledger
        if row.get("turnover_eligible", True)
    )
    exposure_seconds = sum(
        (current.timestamp - previous.timestamp).total_seconds()
        for previous, current in pairwise(result.daily_exposures)
        if previous.gross_invested_notional > 0.0
    )

    gross_tuple = tuple(gross_pnls)
    net_pnls = tuple(trade.pnl for trade in closed)
    holding_tuple = tuple(holding_periods)
    gross = PerformanceSeriesEvidence(
        series_id="strategy_gross",
        label="Strategy gross",
        accounting=AccountingView.GROSS,
        equity_curve=tuple(result.gross_equity_curve),
        initial_capital=initial_capital,
        trade_pnls=gross_tuple,
        gross_trade_pnls=gross_tuple,
        turnover_notional=turnover_notional,
        exposure_seconds=exposure_seconds,
        holding_periods_seconds=holding_tuple,
    )
    net = PerformanceSeriesEvidence(
        series_id="strategy_net",
        label="Strategy net",
        accounting=AccountingView.NET,
        equity_curve=tuple(result.equity_curve),
        initial_capital=initial_capital,
        trade_pnls=net_pnls,
        gross_trade_pnls=gross_tuple,
        turnover_notional=turnover_notional,
        exposure_seconds=exposure_seconds,
        holding_periods_seconds=holding_tuple,
    )
    return StrategyPerformanceEvidence(gross=gross, net=net)


def unavailable_diagnostics_payload(reason: str) -> dict[str, object]:
    """Return the sole allowed representation of absent diagnostic evidence."""

    require_nonempty(reason, field_name="reason")
    return {
        "available": False,
        "comparison_eligible": False,
        "headline_eligible": False,
        "headline_eligibility_evaluated": True,
        "reason": reason,
    }


def complete_diagnostics_payload(
    result: EquityDiagnosticsResult,
) -> dict[str, object]:
    """Return a report envelope only after every required diagnostic succeeds."""

    return {
        "available": True,
        "comparison_eligible": True,
        "headline_eligible": None,
        "headline_eligibility_evaluated": False,
        "result": result.to_dict(),
    }


__all__ = [
    "AccountingView",
    "AttributionEvidence",
    "BenchmarkEvidence",
    "BenchmarkRole",
    "BenchmarkSourceKind",
    "BootstrapProtocol",
    "CSCVProtocol",
    "CapacityProtocol",
    "EquityDiagnosticsEvidence",
    "EquityDiagnosticsResult",
    "FactorPnlContribution",
    "LiquidityCapacityObservation",
    "PerformanceSeriesEvidence",
    "PortfolioPnlAttribution",
    "RollingWindow",
    "SharpeTrialEvidence",
    "StrategyPerformanceEvidence",
    "build_equity_diagnostics",
    "complete_diagnostics_payload",
    "strategy_performance_evidence",
    "unavailable_diagnostics_payload",
]
