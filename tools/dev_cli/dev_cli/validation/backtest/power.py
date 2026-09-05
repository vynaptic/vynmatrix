"""Cadence- and dependence-aware Monte Carlo study-design utilities.

The calculator estimates whether a prospective observation window can detect a
pre-registered minimum standardized effect.  It does not estimate a strategy's
edge and never converts retrospective observations into promotion evidence.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace
from statistics import NormalDist
from typing import Literal

EvidenceRole = Literal["retrospective_diagnostic", "prospective_design"]

_POISSON_EXACT_LIMIT = 30.0
_MIN_TEST_OBSERVATIONS = 2
_LOWER_PREDICTION_QUANTILE = 0.025
_UPPER_PREDICTION_QUANTILE = 0.975
_DEFAULT_MONTE_CARLO_CONFIDENCE = 0.95
_DEFAULT_CONSECUTIVE_PASSES = 2


@dataclass(frozen=True)
class PowerStudyConfig:
    """Explicit assumptions for one Monte Carlo power calculation."""

    cadence_per_year: float
    duration_years: float
    minimum_standardized_effect: float
    effective_trials: int
    familywise_alpha: float = 0.05
    familywise_error_allocation: float = 0.50
    target_power: float = 0.80
    independence_ratio: float = 1.0
    simulations: int = 10_000
    seed: int = 0
    monte_carlo_confidence: float = _DEFAULT_MONTE_CARLO_CONFIDENCE
    evidence_role: EvidenceRole = "prospective_design"

    def __post_init__(self) -> None:
        finite_positive = {
            "cadence_per_year": self.cadence_per_year,
            "duration_years": self.duration_years,
            "minimum_standardized_effect": self.minimum_standardized_effect,
        }
        for name, value in finite_positive.items():
            if not math.isfinite(value) or value <= 0.0:
                msg = f"{name} must be finite and positive"
                raise ValueError(msg)
        if self.effective_trials < 1:
            msg = "effective_trials must be at least 1"
            raise ValueError(msg)
        if not 0.0 < self.familywise_alpha < 1.0:
            msg = "familywise_alpha must be in (0, 1)"
            raise ValueError(msg)
        if not 0.0 < self.familywise_error_allocation < 1.0:
            msg = "familywise_error_allocation must be in (0, 1)"
            raise ValueError(msg)
        if not 0.0 < self.target_power < 1.0:
            msg = "target_power must be in (0, 1)"
            raise ValueError(msg)
        if not 0.0 < self.independence_ratio <= 1.0:
            msg = "independence_ratio must be in (0, 1]"
            raise ValueError(msg)
        if self.simulations <= 0:
            msg = "simulations must be positive"
            raise ValueError(msg)
        if not 0.0 < self.monte_carlo_confidence < 1.0:
            msg = "monte_carlo_confidence must be in (0, 1)"
            raise ValueError(msg)
        if self.evidence_role not in {"retrospective_diagnostic", "prospective_design"}:
            msg = "unsupported evidence_role"
            raise ValueError(msg)


@dataclass(frozen=True)
class PowerStudyResult:
    """Seed-reproducible power and cadence predictions."""

    config: PowerStudyConfig
    expected_decisions: float
    decision_count_lower: int
    decision_count_upper: int
    expected_effective_decisions: float
    critical_z: float
    estimated_power: float
    power_confidence_lower: float
    power_confidence_upper: float
    estimated_familywise_type_i_error: float
    familywise_type_i_error_confidence_lower: float
    familywise_type_i_error_confidence_upper: float
    type_i_error_within_budget: bool
    adequately_powered: bool


@dataclass(frozen=True)
class DurationSearchResult:
    """First registered duration reaching target power, if one exists."""

    minimum_duration_years: float | None
    evaluated: tuple[PowerStudyResult, ...]
    evidence_role: EvidenceRole = "prospective_design"


def _poisson(rng: random.Random, expected: float) -> int:
    """Sample Poisson counts without a third-party numerical dependency."""

    if expected < _POISSON_EXACT_LIMIT:
        threshold = math.exp(-expected)
        product = 1.0
        count = 0
        while product > threshold:
            count += 1
            product *= rng.random()
        return count - 1
    # Normal approximation is stable for stage-design horizons with high
    # expected cadence and avoids an O(lambda) loop.
    return max(0, round(rng.gauss(expected, expected**0.5)))


def _effective_count(count: int, config: PowerStudyConfig) -> float:
    return max(1.0, min(float(count), count * config.independence_ratio))


def _sample_test_statistic(
    rng: random.Random,
    *,
    count: int,
    effect: float,
    config: PowerStudyConfig,
) -> float:
    if count < _MIN_TEST_OBSERVATIONS:
        return float("-inf")
    # Dependence has already been reduced to one registered effective-count
    # ratio by the caller. Simulating another autocorrelation process here
    # would charge the same evidence twice.
    values = [effect + rng.gauss(0.0, 1.0) for _ in range(count)]
    average = sum(values) / count
    variance = sum((value - average) ** 2 for value in values) / count
    if variance <= 0.0:
        return float("-inf")
    return float(average / variance**0.5 * _effective_count(count, config) ** 0.5)


def _wilson_interval(*, successes: int, trials: int, confidence: float) -> tuple[float, float]:
    """Return a two-sided Wilson interval for a Monte Carlo rejection rate."""

    if trials <= 0 or not 0 <= successes <= trials:
        msg = "Wilson interval requires 0 <= successes <= positive trials"
        raise ValueError(msg)
    z_value = NormalDist().inv_cdf(0.5 + confidence / 2.0)
    estimate = successes / trials
    z_squared = z_value**2
    denominator = 1.0 + z_squared / trials
    centre = (estimate + z_squared / (2.0 * trials)) / denominator
    half_width = (
        z_value
        * (estimate * (1.0 - estimate) / trials + z_squared / (4.0 * trials**2)) ** 0.5
        / denominator
    )
    return max(0.0, centre - half_width), min(1.0, centre + half_width)


def run_power_study(config: PowerStudyConfig) -> PowerStudyResult:
    """Estimate one-sided detection power and null familywise error."""

    rng = random.Random(config.seed)
    expected_count = config.cadence_per_year * config.duration_years
    # Reserve part of the governance error budget for finite-simulation and
    # test-model uncertainty. Testing exactly at the full nominal alpha makes
    # ``upper CI <= alpha`` arithmetically unreachable at any finite sample.
    per_trial_alpha = (
        config.familywise_alpha * config.familywise_error_allocation / config.effective_trials
    )
    critical_z = NormalDist().inv_cdf(1.0 - per_trial_alpha)
    counts: list[int] = []
    effective_counts: list[float] = []
    alternative_rejections = 0
    null_rejections = 0
    for _ in range(config.simulations):
        count = _poisson(rng, expected_count)
        counts.append(count)
        effective_counts.append(_effective_count(count, config) if count > 0 else 0.0)
        if (
            _sample_test_statistic(
                rng,
                count=count,
                effect=config.minimum_standardized_effect,
                config=config,
            )
            >= critical_z
        ):
            alternative_rejections += 1
        # Simulating every registered null trial captures the familywise error
        # of the same maximum-z decision rule used by the power calculation.
        if any(
            _sample_test_statistic(rng, count=count, effect=0.0, config=config) >= critical_z
            for _ in range(config.effective_trials)
        ):
            null_rejections += 1

    counts.sort()
    lower_index = math.floor((len(counts) - 1) * _LOWER_PREDICTION_QUANTILE)
    upper_index = math.ceil((len(counts) - 1) * _UPPER_PREDICTION_QUANTILE)
    power = alternative_rejections / config.simulations
    type_i_error = null_rejections / config.simulations
    power_lower, power_upper = _wilson_interval(
        successes=alternative_rejections,
        trials=config.simulations,
        confidence=config.monte_carlo_confidence,
    )
    type_i_lower, type_i_upper = _wilson_interval(
        successes=null_rejections,
        trials=config.simulations,
        confidence=config.monte_carlo_confidence,
    )
    # Stage gates bind on conservative Monte Carlo uncertainty bounds, never on
    # a noisy point estimate. This prevents a lucky seed close to either target
    # from authorising an underpowered prospective observation period.
    type_i_within_budget = type_i_upper <= config.familywise_alpha
    return PowerStudyResult(
        config=config,
        expected_decisions=expected_count,
        decision_count_lower=counts[lower_index],
        decision_count_upper=counts[upper_index],
        expected_effective_decisions=sum(effective_counts) / len(effective_counts),
        critical_z=critical_z,
        estimated_power=power,
        power_confidence_lower=power_lower,
        power_confidence_upper=power_upper,
        estimated_familywise_type_i_error=type_i_error,
        familywise_type_i_error_confidence_lower=type_i_lower,
        familywise_type_i_error_confidence_upper=type_i_upper,
        type_i_error_within_budget=type_i_within_budget,
        adequately_powered=power_lower >= config.target_power and type_i_within_budget,
    )


def minimum_duration_for_power(
    config: PowerStudyConfig,
    *,
    duration_step_years: float,
    maximum_duration_years: float,
    required_consecutive_passes: int = _DEFAULT_CONSECUTIVE_PASSES,
) -> DurationSearchResult:
    """Find the first duration in a stable run of conservatively powered points.

    Every duration uses the same registered seed (common random numbers) and a
    candidate is accepted only after consecutive duration points pass on the
    Monte Carlo confidence bounds. The returned duration is the first point in
    that confirmed run, not the later confirmation point.
    """

    if duration_step_years <= 0.0 or not math.isfinite(duration_step_years):
        msg = "duration_step_years must be finite and positive"
        raise ValueError(msg)
    if maximum_duration_years < duration_step_years:
        msg = "maximum_duration_years must be at least duration_step_years"
        raise ValueError(msg)
    if required_consecutive_passes < 1:
        msg = "required_consecutive_passes must be positive"
        raise ValueError(msg)
    results: list[PowerStudyResult] = []
    consecutive_passes = 0
    steps = math.floor(maximum_duration_years / duration_step_years)
    for step in range(1, steps + 1):
        duration = step * duration_step_years
        # Reuse the same registered random stream across durations. Combined
        # with consecutive confirmation this is less seed-sensitive than
        # searching for the first crossing from independent streams.
        candidate = replace(config, duration_years=duration, seed=config.seed)
        result = run_power_study(candidate)
        results.append(result)
        if result.adequately_powered:
            consecutive_passes += 1
            if consecutive_passes >= required_consecutive_passes:
                first_confirmed_index = len(results) - required_consecutive_passes
                minimum_duration = results[first_confirmed_index].config.duration_years
                return DurationSearchResult(
                    minimum_duration,
                    tuple(results),
                    config.evidence_role,
                )
        else:
            consecutive_passes = 0
    return DurationSearchResult(None, tuple(results), config.evidence_role)
