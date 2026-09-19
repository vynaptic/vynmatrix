"""Deterministic Hurst and slow stochastic calculations matching source formulas."""

from __future__ import annotations

import math
from collections import deque
from typing import ClassVar

from .checkpoint import CheckpointedIndicator
from .sma import SimpleMovingAverage
from .streaming_statistics import finite_sample, validate_period

_MIN_HURST_PERIOD = 8


class HurstExponent(CheckpointedIndicator):
    """Backtrader estimator: twice the log-log slope of sqrt(std(lag differences))."""

    STATE_CONFIG = ("period",)
    STATE_FIELDS: ClassVar[dict[str, str]] = {"_prices": "numbers", "value": "number?"}

    def __init__(self, period: int) -> None:
        self.period = validate_period(period)
        if period < _MIN_HURST_PERIOD:
            msg = "Hurst period must provide at least two lags (period >= 8)"
            raise ValueError(msg)
        self._prices: deque[float] = deque(maxlen=period)
        self.value: float | None = None

    def update(self, close: float) -> float | None:
        self._prices.append(finite_sample(close))
        self.value = None
        if len(self._prices) < self.period:
            return None
        prices = list(self._prices)
        xs, ys = [], []
        for lag in range(2, self.period // 2):
            differences = [prices[index] - prices[index - lag] for index in range(lag, self.period)]
            mean = math.fsum(differences) / len(differences)
            variance = math.fsum((value - mean) ** 2 for value in differences) / len(differences)
            if variance <= 0:
                return None
            xs.append(math.log10(lag))
            # sqrt(population standard deviation) = variance**0.25.
            ys.append(0.25 * math.log10(variance))
        x_mean, y_mean = math.fsum(xs) / len(xs), math.fsum(ys) / len(ys)
        denominator = math.fsum((value - x_mean) ** 2 for value in xs)
        self.value = (
            2
            * math.fsum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True))
            / denominator
        )
        return self.value


class Stochastic(CheckpointedIndicator):
    """Slow %K=SMA(fast %K, dfast), %D=SMA(slow %K, dslow)."""

    STATE_CONFIG = ("period",)
    STATE_FIELDS: ClassVar[dict[str, str]] = {
        "_ranges": "pairs",
        "_fast": "indicator",
        "_slow": "indicator",
        "value": "pair?",
    }

    def __init__(self, period: int, dfast: int, dslow: int = 3) -> None:
        self.period = validate_period(period)
        self._ranges: deque[tuple[float, float]] = deque(maxlen=period)
        self._fast = SimpleMovingAverage(validate_period(dfast))
        self._slow = SimpleMovingAverage(validate_period(dslow))
        self.value: tuple[float, float] | None = None

    def update(self, high: float, low: float, close: float) -> tuple[float, float] | None:
        for sample in (high, low, close):
            finite_sample(sample)
        self._ranges.append((high, low))
        self.value = None
        if len(self._ranges) < self.period:
            return None
        highest, lowest = (
            max(pair[0] for pair in self._ranges),
            min(pair[1] for pair in self._ranges),
        )
        if highest <= lowest:
            self._fast.reset()
            self._slow.reset()
            return None
        slow_k = self._fast.update(100 * (close - lowest) / (highest - lowest))
        if slow_k is None:
            return None
        slow_d = self._slow.update(slow_k)
        if slow_d is not None:
            self.value = slow_k, slow_d
        return self.value

    def validate_checkpoint(self) -> None:
        if any(high < low for high, low in self._ranges):
            msg = "Stochastic checkpoint has inverted high/low ranges"
            raise ValueError(msg)
        fast, slow = self._fast.value, self._slow.value
        expected = (fast, slow) if fast is not None and slow is not None else None
        if self.value != expected or (slow is not None and fast is None):
            msg = "Stochastic checkpoint output disagrees with its smoothing streams"
            raise ValueError(msg)
