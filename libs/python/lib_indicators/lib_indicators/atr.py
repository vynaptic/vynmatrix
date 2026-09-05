"""Average True Range (ATR) — Wilder smoothing, matching backtrader semantics.

backtrader's ``bt.indicators.ATR`` is a ``SmoothedMovingAverage`` (Wilder
SMMA) of the True Range, where the SMMA is seeded with the SMA of the first
``period`` true-range samples and then follows::

    SMMA_t = SMMA_{t-1} * (1 - 1/period) + TR_t * (1/period)

True Range requires the previous close::

    TR = max(high - low, |high - prev_close|, |low - prev_close|)

The first TR is therefore produced on the second bar, exactly as backtrader
computes it. This file documents and replicates that behaviour so migrated
strategies produce bit-identical values to their backtrader originals.
"""

from __future__ import annotations

import math
from collections import deque


class AverageTrueRange:
    """Wilder-smoothed Average True Range (backtrader-compatible).

    Attributes:
        period: Smoothing period.
        value: Current ATR value (None until warmed up).
        is_ready: True once the seed SMA of ``period`` TR samples is formed.

    Example::

        atr = AverageTrueRange(period=14)
        for bar in bars:
            atr.update(high=bar.high, low=bar.low, close=bar.close)
            if atr.is_ready:
                print(atr.value)
    """

    def __init__(self, period: int) -> None:
        if period < 1:
            msg = f"period must be >= 1, got {period}"
            raise ValueError(msg)
        self._period = period
        self._prev_close: float | None = None
        self._seed: deque[float] = deque(maxlen=period)
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

    def update(self, high: float, low: float, close: float) -> float | None:
        """Feed a new OHLC bar and return the current ATR (None during warmup)."""
        if self._prev_close is None:
            self._prev_close = close
            return None

        tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close

        if self._value is None:
            self._seed.append(tr)
            if len(self._seed) == self._period:
                self._value = math.fsum(self._seed) / self._period
            return self._value

        alpha = 1.0 / self._period
        self._value = self._value * (1.0 - alpha) + tr * alpha
        return self._value

    def reset(self) -> None:
        """Reset the indicator to its initial state."""
        self._prev_close = None
        self._seed.clear()
        self._value = None

    def __repr__(self) -> str:
        status = f"value={self._value:.6f}" if self._value is not None else "warming up"
        return f"ATR(period={self._period}, {status})"
