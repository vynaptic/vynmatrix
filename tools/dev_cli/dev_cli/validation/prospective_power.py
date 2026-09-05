"""Observed-cadence diagnostics and prospective evidence-clock power design."""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime

from dev_cli.validation._diagnostic_common import (
    CALENDAR_DAYS_PER_YEAR,
    MAX_ABS_OBSERVED_LAG1,
    MIN_LAG1_CORRELATION_OBSERVATIONS,
    invalid,
    require_finite,
    require_nonempty,
    require_utc,
)
from dev_cli.validation.backtest.power import (
    DurationSearchResult,
    PowerStudyConfig,
    minimum_duration_for_power,
)
from dev_cli.validation.backtest.statistics import (
    circular_block_bootstrap_indices,
    effective_sample_size,
)


@dataclass(frozen=True, slots=True)
class ClosedTradeOutcome:
    """One completed entry/exit outcome used only for prospective design."""

    trade_id: str
    asset: str
    entry_ts: datetime
    exit_ts: datetime
    net_return: float

    def __post_init__(self) -> None:
        require_nonempty(self.trade_id, field_name="trade_id")
        require_nonempty(self.asset, field_name="asset")
        require_utc(self.entry_ts, field_name="entry_ts")
        require_utc(self.exit_ts, field_name="exit_ts")
        if self.exit_ts <= self.entry_ts:
            invalid("closed trade exit_ts must be later than entry_ts")
        require_finite(self.net_return, field_name="net_return")

    def to_dict(self) -> dict[str, object]:
        return {
            "trade_id": self.trade_id,
            "asset": self.asset,
            "entry_ts": self.entry_ts.isoformat(),
            "exit_ts": self.exit_ts.isoformat(),
            "net_return": self.net_return,
        }


@dataclass(frozen=True, slots=True)
class HoldingOverlapDiagnostics:
    """Half-open holding-interval overlap for pooled completed trades."""

    observed_trades: int
    overlapping_pairs: int
    trades_overlapping_any_other: int
    overlapping_trade_fraction: float
    maximum_concurrent_trades: int
    summed_holding_days: float
    union_holding_days: float
    overlap_time_fraction: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ObservedProspectiveDesignInputs:
    """Conservative cadence and dependence inputs for a future evidence clock."""

    observation_start: datetime
    observation_end_exclusive: datetime
    observation_years: float
    filled_entries: int
    completed_exits: int
    matured_outcomes: int
    right_censored_entries: int
    filled_entry_cadence_per_year: float
    matured_outcome_cadence_per_year: float
    cadence_lower_confidence: float
    conservative_matured_outcome_cadence_per_year: float
    raw_lag1_correlation: float
    capped_lag1_correlation: float
    effective_trade_count: float
    serial_independence_ratio: float
    holding_time_independence_ratio: float
    observed_independence_ratio: float
    independence_bootstrap_samples: int
    independence_bootstrap_seed: int
    independence_bootstrap_registered_block_size: int
    independence_bootstrap_effective_block_size: int
    independence_lower_quantile: float
    serial_independence_lower_bound: float
    holding_time_independence_lower_bound: float
    conservative_independence_ratio: float
    holding_overlap: HoldingOverlapDiagnostics
    evidence_role: str = field(default="prospective_design_input", init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "observation_start": self.observation_start.isoformat(),
            "observation_end_exclusive": self.observation_end_exclusive.isoformat(),
            "observation_years": self.observation_years,
            "filled_entries": self.filled_entries,
            "completed_exits": self.completed_exits,
            "matured_outcomes": self.matured_outcomes,
            "right_censored_entries": self.right_censored_entries,
            "filled_entry_cadence_per_year": self.filled_entry_cadence_per_year,
            "matured_outcome_cadence_per_year": self.matured_outcome_cadence_per_year,
            "cadence_lower_confidence": self.cadence_lower_confidence,
            "conservative_matured_outcome_cadence_per_year": (
                self.conservative_matured_outcome_cadence_per_year
            ),
            "raw_lag1_correlation": self.raw_lag1_correlation,
            "capped_lag1_correlation": self.capped_lag1_correlation,
            "effective_trade_count": self.effective_trade_count,
            "serial_independence_ratio": self.serial_independence_ratio,
            "holding_time_independence_ratio": self.holding_time_independence_ratio,
            "observed_independence_ratio": self.observed_independence_ratio,
            "independence_bootstrap_samples": self.independence_bootstrap_samples,
            "independence_bootstrap_seed": self.independence_bootstrap_seed,
            "independence_bootstrap_registered_block_size": (
                self.independence_bootstrap_registered_block_size
            ),
            "independence_bootstrap_effective_block_size": (
                self.independence_bootstrap_effective_block_size
            ),
            "independence_lower_quantile": self.independence_lower_quantile,
            "serial_independence_lower_bound": self.serial_independence_lower_bound,
            "holding_time_independence_lower_bound": self.holding_time_independence_lower_bound,
            "conservative_independence_ratio": self.conservative_independence_ratio,
            "holding_overlap": self.holding_overlap.to_dict(),
            "evidence_role": self.evidence_role,
        }


def _poisson_count_rate_lower_bound(
    *, count: int, exposure_years: float, confidence: float
) -> float:
    """Exact one-sided lower confidence bound for a Poisson event rate."""

    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        invalid("count must be a non-negative integer")
    if not math.isfinite(exposure_years) or exposure_years <= 0.0:
        invalid("exposure_years must be finite and positive")
    if not 0.0 < confidence < 1.0:
        invalid("confidence must be in (0, 1)")
    if count == 0:
        return 0.0

    target_survival = 1.0 - confidence

    def survival(expected: float) -> float:
        if expected == 0.0:
            return 0.0
        log_terms = [
            -expected + index * math.log(expected) - math.lgamma(index + 1.0)
            for index in range(count)
        ]
        maximum = max(log_terms)
        cdf = math.exp(maximum) * sum(math.exp(item - maximum) for item in log_terms)
        return max(0.0, min(1.0, 1.0 - cdf))

    lower = 0.0
    upper = max(1.0, float(count))
    while survival(upper) < target_survival:
        upper *= 2.0
    for _ in range(100):
        midpoint = (lower + upper) / 2.0
        if survival(midpoint) < target_survival:
            lower = midpoint
        else:
            upper = midpoint
    return upper / exposure_years


def _lower_nearest_rank(values: Sequence[float], *, quantile: float) -> float:
    if not values:
        invalid("lower quantile requires at least one value")
    if not 0.0 < quantile < 1.0:
        invalid("quantile must be in (0, 1)")
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return float(ordered[index])


def _numeric_holding_independence(
    durations_seconds: Sequence[float], forward_gaps_seconds: Sequence[float]
) -> float:
    if len(durations_seconds) != len(forward_gaps_seconds) or not durations_seconds:
        invalid("holding bootstrap inputs must be non-empty and aligned")
    intervals: list[tuple[float, float]] = []
    cursor = 0.0
    for duration, gap in zip(durations_seconds, forward_gaps_seconds, strict=True):
        if duration <= 0.0 or gap < 0.0:
            invalid("holding bootstrap durations/gaps are invalid")
        intervals.append((cursor, cursor + duration))
        cursor += gap
    summed = sum(end - start for start, end in intervals)
    intervals.sort()
    union = 0.0
    union_start, union_end = intervals[0]
    for start, end in intervals[1:]:
        if start < union_end:
            union_end = max(union_end, end)
        else:
            union += union_end - union_start
            union_start, union_end = start, end
    union += union_end - union_start
    count = len(intervals)
    return max(1.0 / count, min(1.0, union / summed))


def _bootstrap_independence_lower_bounds(
    trades: Sequence[ClosedTradeOutcome],
    *,
    observation_start: datetime,
    observation_end_exclusive: datetime,
    effective_sample_max_lag: int | None,
    block_size: int,
    samples: int,
    seed: int,
    lower_quantile: float,
) -> tuple[float, float, int]:
    if samples <= 0:
        invalid("independence bootstrap samples must be positive")
    if isinstance(seed, bool) or not isinstance(seed, int):
        invalid("independence bootstrap seed must be an integer")
    if block_size <= 0:
        invalid("independence bootstrap block size must be positive")
    effective_block_size = min(block_size, len(trades))

    exit_order = tuple(
        sorted(trades, key=lambda item: (item.exit_ts, item.entry_ts, item.trade_id))
    )
    entry_order = tuple(
        sorted(trades, key=lambda item: (item.entry_ts, item.exit_ts, item.trade_id))
    )
    durations = tuple((item.exit_ts - item.entry_ts).total_seconds() for item in entry_order)
    forward_gaps: list[float] = []
    for index, item in enumerate(entry_order):
        if index + 1 < len(entry_order):
            gap = (entry_order[index + 1].entry_ts - item.entry_ts).total_seconds()
        else:
            gap = (
                observation_end_exclusive
                - item.entry_ts
                + entry_order[0].entry_ts
                - observation_start
            ).total_seconds()
        forward_gaps.append(gap)

    serial_estimates: list[float] = []
    holding_estimates: list[float] = []
    serial_indices = circular_block_bootstrap_indices(
        population_size=len(exit_order),
        block_size=effective_block_size,
        samples=samples,
        seed=seed,
    )
    holding_indices = circular_block_bootstrap_indices(
        population_size=len(entry_order),
        block_size=effective_block_size,
        samples=samples,
        seed=seed + 1,
    )
    for return_sample, holding_sample in zip(serial_indices, holding_indices, strict=True):
        sampled_returns = tuple(exit_order[index].net_return for index in return_sample)
        serial_count = effective_sample_size(sampled_returns, max_lag=effective_sample_max_lag)
        serial_estimates.append(
            max(1.0 / len(sampled_returns), min(1.0, serial_count / len(sampled_returns)))
        )
        holding_estimates.append(
            _numeric_holding_independence(
                tuple(durations[index] for index in holding_sample),
                tuple(forward_gaps[index] for index in holding_sample),
            )
        )
    return (
        _lower_nearest_rank(serial_estimates, quantile=lower_quantile),
        _lower_nearest_rank(holding_estimates, quantile=lower_quantile),
        effective_block_size,
    )


def _lag1_correlation(values: Sequence[float]) -> float:
    if len(values) < MIN_LAG1_CORRELATION_OBSERVATIONS:
        return 0.0
    left = values[:-1]
    right = values[1:]
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    covariance = sum(
        (x_value - left_mean) * (y_value - right_mean)
        for x_value, y_value in zip(left, right, strict=True)
    )
    left_sum_squares = sum((value - left_mean) ** 2 for value in left)
    right_sum_squares = sum((value - right_mean) ** 2 for value in right)
    denominator = (left_sum_squares * right_sum_squares) ** 0.5
    return float(covariance / denominator) if denominator > 0.0 else 0.0


def _holding_overlap(trades: Sequence[ClosedTradeOutcome]) -> HoldingOverlapDiagnostics:
    overlapping_pairs = 0
    overlapping_ids: set[str] = set()
    for left, right in itertools.combinations(trades, 2):
        if left.entry_ts < right.exit_ts and right.entry_ts < left.exit_ts:
            overlapping_pairs += 1
            overlapping_ids.update((left.trade_id, right.trade_id))

    events: list[tuple[datetime, int]] = []
    intervals = sorted((item.entry_ts, item.exit_ts) for item in trades)
    for start, end in intervals:
        events.extend(((start, 1), (end, -1)))
    events.sort(key=lambda item: (item[0], item[1]))
    concurrent = 0
    maximum_concurrent = 0
    for _timestamp, delta in events:
        concurrent += delta
        maximum_concurrent = max(maximum_concurrent, concurrent)

    summed_seconds = sum((end - start).total_seconds() for start, end in intervals)
    union_seconds = 0.0
    if intervals:
        union_start, union_end = intervals[0]
        for start, end in intervals[1:]:
            if start < union_end:
                union_end = max(union_end, end)
            else:
                union_seconds += (union_end - union_start).total_seconds()
                union_start, union_end = start, end
        union_seconds += (union_end - union_start).total_seconds()
    overlap_fraction = (
        max(0.0, min(1.0, 1.0 - union_seconds / summed_seconds)) if summed_seconds > 0.0 else 0.0
    )
    return HoldingOverlapDiagnostics(
        observed_trades=len(trades),
        overlapping_pairs=overlapping_pairs,
        trades_overlapping_any_other=len(overlapping_ids),
        overlapping_trade_fraction=(len(overlapping_ids) / len(trades) if trades else 0.0),
        maximum_concurrent_trades=maximum_concurrent,
        summed_holding_days=summed_seconds / 86_400.0,
        union_holding_days=union_seconds / 86_400.0,
        overlap_time_fraction=overlap_fraction,
    )


def observe_prospective_design_inputs(
    outcomes: Sequence[ClosedTradeOutcome],
    *,
    observation_start: datetime,
    observation_end_exclusive: datetime,
    effective_sample_max_lag: int | None = None,
    right_censored_entries: int = 0,
    cadence_lower_confidence: float = 0.95,
    independence_bootstrap_samples: int = 10_000,
    independence_bootstrap_seed: int = 0,
    independence_bootstrap_block_size: int = 2,
    independence_lower_quantile: float = 0.05,
) -> ObservedProspectiveDesignInputs:
    """Estimate conservative power inputs from pooled chronological outcomes."""

    require_utc(observation_start, field_name="observation_start")
    require_utc(observation_end_exclusive, field_name="observation_end_exclusive")
    if effective_sample_max_lag is not None and (
        isinstance(effective_sample_max_lag, bool)
        or not isinstance(effective_sample_max_lag, int)
        or effective_sample_max_lag < 1
    ):
        invalid("effective_sample_max_lag must be a positive integer or None")
    if observation_end_exclusive <= observation_start:
        invalid("observation_end_exclusive must be later than observation_start")
    if (
        isinstance(right_censored_entries, bool)
        or not isinstance(right_censored_entries, int)
        or right_censored_entries < 0
    ):
        invalid("right_censored_entries must be a non-negative integer")
    trades = tuple(outcomes)
    if not trades:
        invalid("at least one closed trade outcome is required")
    identifiers = tuple(item.trade_id for item in trades)
    if len(set(identifiers)) != len(identifiers):
        invalid("closed trade outcome ids must be unique")
    for trade in trades:
        if trade.entry_ts < observation_start or trade.exit_ts >= observation_end_exclusive:
            invalid("closed trades must fall wholly inside the observation interval")
    ordered = tuple(sorted(trades, key=lambda item: (item.exit_ts, item.entry_ts, item.trade_id)))
    returns = tuple(item.net_return for item in ordered)
    raw_lag1 = _lag1_correlation(returns)
    capped_lag1 = max(-MAX_ABS_OBSERVED_LAG1, min(MAX_ABS_OBSERVED_LAG1, raw_lag1))
    effective_count = effective_sample_size(returns, max_lag=effective_sample_max_lag)
    serial_ratio = max(1.0 / len(returns), min(1.0, effective_count / len(returns)))
    overlap = _holding_overlap(ordered)
    holding_ratio = (
        max(
            1.0 / len(returns),
            min(1.0, overlap.union_holding_days / overlap.summed_holding_days),
        )
        if overlap.summed_holding_days > 0.0
        else 1.0
    )
    independence_ratio = min(serial_ratio, holding_ratio)
    years = (observation_end_exclusive - observation_start).total_seconds() / (
        86_400.0 * CALENDAR_DAYS_PER_YEAR
    )
    serial_lower, holding_lower, effective_block_size = _bootstrap_independence_lower_bounds(
        ordered,
        observation_start=observation_start,
        observation_end_exclusive=observation_end_exclusive,
        effective_sample_max_lag=effective_sample_max_lag,
        block_size=independence_bootstrap_block_size,
        samples=independence_bootstrap_samples,
        seed=independence_bootstrap_seed,
        lower_quantile=independence_lower_quantile,
    )
    conservative_independence = min(independence_ratio, serial_lower, holding_lower)
    matured_cadence = len(trades) / years
    return ObservedProspectiveDesignInputs(
        observation_start=observation_start,
        observation_end_exclusive=observation_end_exclusive,
        observation_years=years,
        filled_entries=len(trades) + right_censored_entries,
        completed_exits=len(trades),
        matured_outcomes=len(trades),
        right_censored_entries=right_censored_entries,
        filled_entry_cadence_per_year=(len(trades) + right_censored_entries) / years,
        matured_outcome_cadence_per_year=matured_cadence,
        cadence_lower_confidence=cadence_lower_confidence,
        conservative_matured_outcome_cadence_per_year=(
            _poisson_count_rate_lower_bound(
                count=len(trades),
                exposure_years=years,
                confidence=cadence_lower_confidence,
            )
        ),
        raw_lag1_correlation=raw_lag1,
        capped_lag1_correlation=capped_lag1,
        effective_trade_count=effective_count,
        serial_independence_ratio=serial_ratio,
        holding_time_independence_ratio=holding_ratio,
        observed_independence_ratio=independence_ratio,
        independence_bootstrap_samples=independence_bootstrap_samples,
        independence_bootstrap_seed=independence_bootstrap_seed,
        independence_bootstrap_registered_block_size=independence_bootstrap_block_size,
        independence_bootstrap_effective_block_size=effective_block_size,
        independence_lower_quantile=independence_lower_quantile,
        serial_independence_lower_bound=serial_lower,
        holding_time_independence_lower_bound=holding_lower,
        conservative_independence_ratio=conservative_independence,
        holding_overlap=overlap,
    )


@dataclass(frozen=True, slots=True)
class ProspectivePowerDesignResult:
    """Observed-input wrapper over the registered Monte Carlo duration search."""

    observed_inputs: ObservedProspectiveDesignInputs
    minimum_standardized_effect: float
    search: DurationSearchResult
    evidence_role: str = field(default="prospective_design", init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "observed_inputs": self.observed_inputs.to_dict(),
            "minimum_standardized_effect": self.minimum_standardized_effect,
            "minimum_duration_years": self.search.minimum_duration_years,
            "evaluated": [asdict(item) for item in self.search.evaluated],
            "evidence_role": self.evidence_role,
        }


def search_prospective_duration_from_observed_trades(
    observed_inputs: ObservedProspectiveDesignInputs,
    *,
    minimum_standardized_effect: float,
    effective_trials: int,
    familywise_alpha: float,
    familywise_error_allocation: float,
    target_power: float,
    simulations: int,
    seed: int,
    monte_carlo_confidence: float,
    duration_step_years: float,
    maximum_duration_years: float,
    required_consecutive_passes: int,
) -> ProspectivePowerDesignResult:
    """Run the existing duration search with only observed cadence/dependence."""

    config = PowerStudyConfig(
        cadence_per_year=observed_inputs.conservative_matured_outcome_cadence_per_year,
        duration_years=duration_step_years,
        minimum_standardized_effect=minimum_standardized_effect,
        effective_trials=effective_trials,
        familywise_alpha=familywise_alpha,
        familywise_error_allocation=familywise_error_allocation,
        target_power=target_power,
        independence_ratio=observed_inputs.conservative_independence_ratio,
        simulations=simulations,
        seed=seed,
        monte_carlo_confidence=monte_carlo_confidence,
        evidence_role="prospective_design",
    )
    search = minimum_duration_for_power(
        config,
        duration_step_years=duration_step_years,
        maximum_duration_years=maximum_duration_years,
        required_consecutive_passes=required_consecutive_passes,
    )
    return ProspectivePowerDesignResult(observed_inputs, minimum_standardized_effect, search)
