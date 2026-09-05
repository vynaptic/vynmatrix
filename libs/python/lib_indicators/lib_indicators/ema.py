"""Exponential Moving Average (EMA) — SMA-seeded, matching LEAN behaviour.

LEAN's ``ExponentialMovingAverage`` uses a Simple Moving Average (SMA) of
the first ``period`` data points as the seed value, then applies the
standard EMA recursion:

    EMA_t = price * k + EMA_{t-1} * (1 - k)

where ``k = 2 / (period + 1)``.

This implementation replicates that behaviour exactly so the pure-core
strategy produces identical values to LEAN's indicator for the same input
sequence.
"""

from __future__ import annotations

import math
from collections import deque


class ExponentialMovingAverage:
    """SMA-seeded Exponential Moving Average matching LEAN semantics.

    Attributes:
        period: Lookback period.
        value: Current EMA value (None until warmed up).
        is_ready: True once at least ``period`` data points have been fed.
        samples_received: Total number of data points fed so far.

    Example::

        ema = ExponentialMovingAverage(period=50)
        for price in prices:
            ema.update(price)
            if ema.is_ready:
                print(ema.value)
    """

    def __init__(self, period: int) -> None:
        if period < 1:
            msg = f"period must be >= 1, got {period}"
            raise ValueError(msg)
        self._period = period
        self._k = 2.0 / (period + 1)
        self._seed_buffer: deque[float] = deque(maxlen=period)
        self._value: float | None = None
        self._samples: int = 0

    @property
    def period(self) -> int:
        return self._period

    @property
    def value(self) -> float | None:
        return self._value

    @property
    def is_ready(self) -> bool:
        return self._value is not None

    @property
    def samples_received(self) -> int:
        return self._samples

    def update(self, price: float) -> float | None:
        """Feed a new price and return the current EMA value.

        Returns None until ``period`` data points have been received
        (during the SMA seeding phase).

        Args:
            price: New price value.

        Returns:
            Current EMA value, or None if not yet warmed up.
        """
        self._samples += 1

        if self._value is None:
            # Still in SMA seeding phase
            self._seed_buffer.append(price)
            if len(self._seed_buffer) == self._period:
                # Seed with SMA of first `period` values
                self._value = math.fsum(self._seed_buffer) / self._period
            return self._value

        # Standard EMA recursion
        self._value = self._value * (1.0 - self._k) + price * self._k
        return self._value

    def reset(self) -> None:
        """Reset the indicator to its initial state."""
        self._seed_buffer.clear()
        self._value = None
        self._samples = 0

    def __repr__(self) -> str:
        status = f"value={self._value:.6f}" if self._value is not None else "warming up"
        return f"EMA(period={self._period}, {status}, samples={self._samples})"
