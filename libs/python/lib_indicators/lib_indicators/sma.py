"""Simple Moving Average (SMA).

Plain arithmetic mean over a fixed window. Matches backtrader's
``bt.indicators.SimpleMovingAverage`` semantics exactly: the first value is
produced once ``period`` samples have been received and always averages the
most recent ``period`` samples (current sample included).
"""

from __future__ import annotations

import math
from collections import deque


class SimpleMovingAverage:
    """Rolling arithmetic mean.

    Attributes:
        period: Lookback period.
        value: Current SMA value (None until warmed up).
        is_ready: True once ``period`` samples have been fed.

    Example::

        sma = SimpleMovingAverage(period=20)
        for price in prices:
            sma.update(price)
            if sma.is_ready:
                print(sma.value)
    """

    def __init__(self, period: int) -> None:
        if period < 1:
            msg = f"period must be >= 1, got {period}"
            raise ValueError(msg)
        self._period = period
        self._window: deque[float] = deque(maxlen=period)
        self._value: float | None = None

    @property
    def period(self) -> int:
        return self._period

    @property
    def value(self) -> float | None:
        return self._value

    @property
    def is_ready(self) -> bool:
        return self._value is not None

    def update(self, sample: float) -> float | None:
        """Feed a new sample and return the current SMA (None during warmup)."""
        self._window.append(sample)
        if len(self._window) == self._period:
            # math.fsum mirrors backtrader's Average (bit-exact parity).
            self._value = math.fsum(self._window) / self._period
        return self._value

    def reset(self) -> None:
        """Reset the indicator to its initial state."""
        self._window.clear()
        self._value = None

    def __repr__(self) -> str:
        status = f"value={self._value:.6f}" if self._value is not None else "warming up"
        return f"SMA(period={self._period}, {status})"
