"""Bounded rolling statistics and causal crossover state for native signals."""

from __future__ import annotations

import math
from collections import deque
from typing import ClassVar

from .checkpoint import CheckpointedIndicator


def validate_period(period: int) -> int:
    """Require an actual positive integer lookback."""
    if isinstance(period, bool) or not isinstance(period, int) or period < 1:
        msg = f"period must be a positive integer, got {period!r}"
        raise ValueError(msg)
    return period


def finite_sample(value: float) -> float:
    """Reject invalid samples before changing any streaming state."""
    if not math.isfinite(value):
        msg = "indicator sample must be finite"
        raise ValueError(msg)
    return value


class RollingStatistics(CheckpointedIndicator):
    """Population mean/deviation over the current, bounded sample window.

    Unlike RollingZScore (which deliberately scores against a prior window),
    this contract includes the current observation, as Backtrader's bands do.
    Centered summation avoids cancellation for large price levels.
    """

    STATE_CONFIG = ("period",)
    STATE_FIELDS: ClassVar[dict[str, str]] = {
        "values": "numbers",
        "mean": "number?",
        "std": "number?",
    }

    def __init__(self, period: int) -> None:
        self.period = validate_period(period)
        self.values: deque[float] = deque(maxlen=period)
        self.mean: float | None = None
        self.std: float | None = None

    def update(self, value: float) -> tuple[float, float] | None:
        self.values.append(finite_sample(value))
        if len(self.values) < self.period:
            return None
        self.mean = math.fsum(self.values) / self.period
        variance = math.fsum((sample - self.mean) ** 2 for sample in self.values) / self.period
        self.std = math.sqrt(variance)
        return self.mean, self.std

    def validate_checkpoint(self) -> None:
        ready = len(self.values) == self.period
        if ready != (self.mean is not None) or ready != (self.std is not None):
            msg = "Rolling checkpoint readiness differs from its window"
            raise ValueError(msg)
        if ready:
            mean = math.fsum(self.values) / self.period
            std = math.sqrt(math.fsum((sample - mean) ** 2 for sample in self.values) / self.period)
            if self.mean != mean or self.std != std:
                msg = "Rolling checkpoint statistics differ from its window"
                raise ValueError(msg)

    @property
    def percent_rank(self) -> float | None:
        """Fraction strictly below the current sample, including it in the divisor."""
        if len(self.values) < self.period:
            return None
        current = self.values[-1]
        return sum(sample < current for sample in self.values) / self.period


class WilderAverage(CheckpointedIndicator):
    """SMA-seeded Wilder smoothing with alpha=1/period."""

    STATE_CONFIG = ("period",)
    STATE_FIELDS: ClassVar[dict[str, str]] = {
        "_seed": "numbers",
        "value": "number?",
        "_samples": "integer",
    }

    def __init__(self, period: int) -> None:
        self.period = validate_period(period)
        self._seed: list[float] = []
        self._samples = 0
        self.value: float | None = None

    def update(self, value: float) -> float | None:
        finite_sample(value)
        self._samples += 1
        if self.value is None:
            self._seed.append(value)
            if len(self._seed) == self.period:
                self.value = math.fsum(self._seed) / self.period
                self._seed.clear()
        else:
            alpha = 1.0 / self.period
            self.value = self.value * (1.0 - alpha) + value * alpha
        return self.value

    def validate_checkpoint(self) -> None:
        ready = self._samples >= self.period
        if ready != (self.value is not None) or len(self._seed) != (0 if ready else self._samples):
            msg = "Wilder checkpoint readiness differs from its sample count"
            raise ValueError(msg)


class CrossOver(CheckpointedIndicator):
    """Report a sign change through zero; equal observations retain the last sign."""

    STATE_CONFIG = ()
    STATE_FIELDS: ClassVar[dict[str, str]] = {"_last_nonzero": "number?"}

    def __init__(self) -> None:
        self._last_nonzero: float | None = None

    def update(self, difference: float) -> int:
        finite_sample(difference)
        previous = self._last_nonzero
        result = 0
        if difference != 0:
            if previous is not None and previous * difference < 0:
                result = 1 if difference > 0 else -1
            self._last_nonzero = difference
        return result
