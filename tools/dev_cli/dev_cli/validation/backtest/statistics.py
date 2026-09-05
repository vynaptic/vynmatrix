"""Selection-aware statistical diagnostics for backtest return series.

These routines are deterministic and dependency-free.  They expose assumptions
that are commonly hidden by statistical packages (block length, random seed,
effective sample size, and effective trial count).  Their outputs are labelled
retrospective diagnostics and never constitute promotion evidence by
themselves.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from statistics import NormalDist

_DEFAULT_ANNUALIZATION_FACTOR = 365.0
_EULER_MASCHERONI = 0.5772156649015329
_MIN_VARIANCE_OBSERVATIONS = 2
_MIN_ACF_OBSERVATIONS = 3


@dataclass(frozen=True)
class BootstrapConfidenceInterval:
    """A seeded circular-block-bootstrap interval."""

    observed: float
    lower: float
    upper: float
    confidence: float
    block_size: int
    samples: int
    seed: int
    evidence_role: str = field(default="retrospective_diagnostic", init=False)


@dataclass(frozen=True)
class SharpeDiagnostics:
    """Probabilistic and trial-deflated Sharpe diagnostics."""

    observed_sharpe: float
    benchmark_sharpe: float
    probabilistic_sharpe_probability: float
    deflated_benchmark_sharpe: float
    deflated_sharpe_probability: float
    observations: int
    effective_observations: float
    effective_trials: float
    trial_sharpe_std: float
    trial_sharpe_std_source: str
    skewness: float
    kurtosis: float
    selection_contaminated: bool
    evidence_role: str = field(default="retrospective_diagnostic", init=False)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: Sequence[float]) -> float:
    if len(values) < _MIN_VARIANCE_OBSERVATIONS:
        return 0.0
    average = _mean(values)
    return float((sum((value - average) ** 2 for value in values) / len(values)) ** 0.5)


def _validate_finite(values: Sequence[float]) -> None:
    if not values:
        msg = "values must not be empty"
        raise ValueError(msg)
    if any(not math.isfinite(value) for value in values):
        msg = "values must be finite"
        raise ValueError(msg)


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def circular_block_bootstrap_indices(
    *,
    population_size: int,
    block_size: int,
    samples: int,
    seed: int,
) -> Iterator[tuple[int, ...]]:
    """Yield deterministic circular-block bootstrap index samples.

    Exposing the sampled indices lets callers bootstrap aligned multi-field
    observations (for example an event return and its ATR bucket) without
    flattening them into separate, inconsistently sampled series.
    """

    if isinstance(population_size, bool) or not isinstance(population_size, int):
        msg = "population_size must be an integer"
        raise TypeError(msg)
    if isinstance(block_size, bool) or not isinstance(block_size, int):
        msg = "block_size must be an integer"
        raise TypeError(msg)
    if isinstance(samples, bool) or not isinstance(samples, int):
        msg = "samples must be an integer"
        raise TypeError(msg)
    if isinstance(seed, bool) or not isinstance(seed, int):
        msg = "seed must be an integer"
        raise TypeError(msg)
    if population_size <= 0:
        msg = "population_size must be positive"
        raise ValueError(msg)
    if block_size <= 0 or block_size > population_size:
        msg = "block_size must be in [1, population_size]"
        raise ValueError(msg)
    if samples <= 0:
        msg = "samples must be positive"
        raise ValueError(msg)

    rng = random.Random(seed)
    for _ in range(samples):
        sample: list[int] = []
        while len(sample) < population_size:
            start = rng.randrange(population_size)
            remaining = min(block_size, population_size - len(sample))
            sample.extend((start + offset) % population_size for offset in range(remaining))
        yield tuple(sample)


def bootstrap_interval_from_estimates(
    *,
    observed: float,
    estimates: Sequence[float],
    confidence: float,
    block_size: int,
    seed: int,
) -> BootstrapConfidenceInterval:
    """Build a percentile interval from already aligned bootstrap estimates."""

    if not math.isfinite(observed):
        msg = "observed must be finite"
        raise ValueError(msg)
    _validate_finite(estimates)
    if not 0.0 < confidence < 1.0:
        msg = "confidence must be in (0, 1)"
        raise ValueError(msg)
    if block_size <= 0:
        msg = "block_size must be positive"
        raise ValueError(msg)
    if isinstance(seed, bool) or not isinstance(seed, int):
        msg = "seed must be an integer"
        raise TypeError(msg)

    tail = (1.0 - confidence) / 2.0
    return BootstrapConfidenceInterval(
        observed=observed,
        lower=_quantile(estimates, tail),
        upper=_quantile(estimates, 1.0 - tail),
        confidence=confidence,
        block_size=block_size,
        samples=len(estimates),
        seed=seed,
    )


def block_bootstrap_confidence_interval(
    values: Sequence[float],
    statistic: Callable[[Sequence[float]], float],
    *,
    block_size: int,
    samples: int,
    confidence: float,
    seed: int,
) -> BootstrapConfidenceInterval:
    """Estimate a percentile interval with seeded circular blocks.

    A circular bootstrap preserves every possible block start and avoids
    systematically undersampling observations near either boundary.  The block
    size is an input assumption and must be registered with the trial.
    """

    _validate_finite(values)
    if block_size <= 0 or block_size > len(values):
        msg = "block_size must be in [1, len(values)]"
        raise ValueError(msg)
    if samples <= 0:
        msg = "samples must be positive"
        raise ValueError(msg)
    if not 0.0 < confidence < 1.0:
        msg = "confidence must be in (0, 1)"
        raise ValueError(msg)

    estimates: list[float] = []
    for indices in circular_block_bootstrap_indices(
        population_size=len(values),
        block_size=block_size,
        samples=samples,
        seed=seed,
    ):
        sample = [values[index] for index in indices]
        estimate = float(statistic(sample))
        if not math.isfinite(estimate):
            msg = "statistic must return a finite value"
            raise ValueError(msg)
        estimates.append(estimate)

    return bootstrap_interval_from_estimates(
        observed=float(statistic(values)),
        estimates=estimates,
        confidence=confidence,
        block_size=block_size,
        seed=seed,
    )


def effective_sample_size(values: Sequence[float], *, max_lag: int | None = None) -> float:
    """Estimate effective observations using the initial-positive ACF sequence."""

    _validate_finite(values)
    count = len(values)
    if count < _MIN_ACF_OBSERVATIONS:
        return float(count)
    average = _mean(values)
    denominator = sum((value - average) ** 2 for value in values)
    if denominator == 0.0:
        return float(count)
    lag_limit = min(max_lag if max_lag is not None else int(count**0.5), count - 1)
    if lag_limit <= 0:
        return float(count)

    positive_sum = 0.0
    for lag in range(1, lag_limit + 1):
        numerator = sum(
            (values[index] - average) * (values[index - lag] - average)
            for index in range(lag, count)
        )
        autocorrelation = numerator / denominator
        if autocorrelation <= 0.0:
            break
        positive_sum += autocorrelation
    estimate = count / (1.0 + 2.0 * positive_sum)
    return min(float(count), max(1.0, estimate))


def _skewness(values: Sequence[float]) -> float:
    deviation = _std(values)
    if deviation == 0.0:
        return 0.0
    average = _mean(values)
    return _mean([((value - average) / deviation) ** 3 for value in values])


def _kurtosis(values: Sequence[float]) -> float:
    deviation = _std(values)
    if deviation == 0.0:
        return 3.0
    average = _mean(values)
    return _mean([((value - average) / deviation) ** 4 for value in values])


def probabilistic_sharpe_probability(
    *,
    observed_sharpe: float,
    benchmark_sharpe: float,
    effective_observations: float,
    skewness: float,
    kurtosis: float,
    annualization_factor: float,
) -> float:
    """Probability observed Sharpe exceeds a benchmark under PSR assumptions."""

    if effective_observations <= 1.0:
        return 0.5
    if annualization_factor <= 0.0 or not math.isfinite(annualization_factor):
        msg = "annualization_factor must be finite and positive"
        raise ValueError(msg)
    scale = annualization_factor**0.5
    observed_per_period = observed_sharpe / scale
    benchmark_per_period = benchmark_sharpe / scale
    variance_adjustment = (
        1.0 - skewness * observed_per_period + ((kurtosis - 1.0) / 4.0) * observed_per_period**2
    )
    if variance_adjustment <= 0.0:
        return 0.5
    z_score = (
        (observed_per_period - benchmark_per_period)
        * (effective_observations - 1.0) ** 0.5
        / variance_adjustment**0.5
    )
    return NormalDist().cdf(z_score)


def expected_maximum_sharpe(
    *,
    effective_trials: float,
    trial_sharpe_std: float,
) -> float:
    """Expected best Sharpe under independent zero-edge trials.

    ``effective_trials`` deliberately accepts a non-integer so correlated trial
    families can register an independently justified effective count.
    """

    if effective_trials < 1.0 or not math.isfinite(effective_trials):
        msg = "effective_trials must be finite and at least 1"
        raise ValueError(msg)
    if trial_sharpe_std < 0.0 or not math.isfinite(trial_sharpe_std):
        msg = "trial_sharpe_std must be finite and non-negative"
        raise ValueError(msg)
    if effective_trials == 1.0 or trial_sharpe_std == 0.0:
        return 0.0
    normal = NormalDist()
    first = normal.inv_cdf(1.0 - 1.0 / effective_trials)
    second = normal.inv_cdf(1.0 - 1.0 / (effective_trials * math.e))
    estimate = trial_sharpe_std * ((1.0 - _EULER_MASCHERONI) * first + _EULER_MASCHERONI * second)
    return max(0.0, estimate)


def sharpe_diagnostics(
    returns: Sequence[float],
    *,
    effective_trials: float,
    trial_sharpe_std: float,
    trial_sharpe_std_source: str,
    annualization_factor: float = _DEFAULT_ANNUALIZATION_FACTOR,
    benchmark_sharpe: float = 0.0,
    selection_contaminated: bool = True,
    max_autocorrelation_lag: int | None = None,
) -> SharpeDiagnostics:
    """Calculate PSR and selection-deflated Sharpe with explicit assumptions.

    ``trial_sharpe_std`` is deliberately mandatory. The function cannot infer
    cross-trial dispersion from the selected strategy's own return count: doing
    so would let a caller claim deflation while omitting the registered search
    family. The source identifier must point to the immutable selection ledger
    or to a separately pre-registered external estimate.
    """

    _validate_finite(returns)
    if annualization_factor <= 0.0 or not math.isfinite(annualization_factor):
        msg = "annualization_factor must be finite and positive"
        raise ValueError(msg)
    if not math.isfinite(benchmark_sharpe):
        msg = "benchmark_sharpe must be finite"
        raise ValueError(msg)
    if not math.isfinite(effective_trials) or effective_trials < 1.0:
        msg = "effective_trials must be finite and at least 1"
        raise ValueError(msg)
    if not math.isfinite(trial_sharpe_std) or trial_sharpe_std < 0.0:
        msg = "trial_sharpe_std must be finite and non-negative"
        raise ValueError(msg)
    if effective_trials > 1.0 and trial_sharpe_std == 0.0:
        msg = "multiple-trial Sharpe deflation requires positive cross-trial dispersion"
        raise ValueError(msg)
    if not trial_sharpe_std_source.strip():
        msg = "trial_sharpe_std_source must identify the registered evidence"
        raise ValueError(msg)
    deviation = _std(returns)
    observed = _mean(returns) / deviation * annualization_factor**0.5 if deviation > 0.0 else 0.0
    effective_observations = effective_sample_size(
        returns,
        max_lag=max_autocorrelation_lag,
    )
    registered_trial_std = float(trial_sharpe_std)
    deflated_benchmark = expected_maximum_sharpe(
        effective_trials=effective_trials,
        trial_sharpe_std=registered_trial_std,
    )
    skewness = _skewness(returns)
    kurtosis = _kurtosis(returns)
    return SharpeDiagnostics(
        observed_sharpe=observed,
        benchmark_sharpe=benchmark_sharpe,
        probabilistic_sharpe_probability=probabilistic_sharpe_probability(
            observed_sharpe=observed,
            benchmark_sharpe=benchmark_sharpe,
            effective_observations=effective_observations,
            skewness=skewness,
            kurtosis=kurtosis,
            annualization_factor=annualization_factor,
        ),
        deflated_benchmark_sharpe=deflated_benchmark,
        deflated_sharpe_probability=probabilistic_sharpe_probability(
            observed_sharpe=observed,
            benchmark_sharpe=max(benchmark_sharpe, deflated_benchmark),
            effective_observations=effective_observations,
            skewness=skewness,
            kurtosis=kurtosis,
            annualization_factor=annualization_factor,
        ),
        observations=len(returns),
        effective_observations=effective_observations,
        effective_trials=effective_trials,
        trial_sharpe_std=registered_trial_std,
        trial_sharpe_std_source=trial_sharpe_std_source,
        skewness=skewness,
        kurtosis=kurtosis,
        selection_contaminated=selection_contaminated,
    )
