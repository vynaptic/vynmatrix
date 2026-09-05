"""Streaming, scale-invariant regime-gate primitives for equity strategies.

Shared by strategy cores and the equity backtest data feeder
(tools/dev_cli validation harness) — the rules live here once, never
re-implemented:

- ``TrendConfirmGate``: symmetric N-day-confirmation hysteresis around a
  boolean condition (index close vs its SMA200).
- ``RollingZScore``: z-score of the current sample against the *prior*
  ``window`` samples (the sample never dilutes its own baseline).
- ``ExpandingPercentileRank``: percentile rank of the current sample against
  all *prior* samples — the §4.1 VIX kill-switch statistic, computed by the
  data feeder over the full 2010→present history.
"""

from __future__ import annotations

import math
from bisect import bisect_right, insort
from collections import deque

__all__ = [
    "ExpandingPercentileRank",
    "RollingZScore",
    "TrendConfirmGate",
]

_MIN_STD = 1e-12
_MIN_Z_WINDOW = 2


class TrendConfirmGate:
    """Boolean gate that flips only after ``confirm_days`` consecutive opposites.

    Starts OFF (fail closed): the gate must earn its ON state with
    ``confirm_days`` consecutive True observations, which resolves during
    warmup long before any signal can be emitted.
    """

    def __init__(self, confirm_days: int) -> None:
        if confirm_days < 1:
            msg = f"confirm_days must be >= 1, got {confirm_days}"
            raise ValueError(msg)
        self._confirm_days = confirm_days
        self._state = False
        self._streak = 0

    @property
    def is_on(self) -> bool:
        return self._state

    def update(self, above: bool) -> bool:
        """Feed one observation (e.g. index close > SMA200); returns the gate state."""
        if above == self._state:
            self._streak = 0
        else:
            self._streak += 1
            if self._streak >= self._confirm_days:
                self._state = above
                self._streak = 0
        return self._state


class RollingZScore:
    """Z-score of each sample against the preceding ``window`` samples.

    Returns ``None`` until the prior window is full, and ``None`` when the
    prior window is degenerate (std below a floor) — a flat baseline cannot
    classify a dip, so the caller must treat ``None`` as not-a-signal.
    """

    def __init__(self, window: int) -> None:
        if window < _MIN_Z_WINDOW:
            msg = f"window must be >= {_MIN_Z_WINDOW}, got {window}"
            raise ValueError(msg)
        self._window = window
        self._values: deque[float] = deque(maxlen=window)

    @property
    def is_ready(self) -> bool:
        return len(self._values) == self._window

    def update(self, value: float) -> float | None:
        z: float | None = None
        if self.is_ready:
            mean = sum(self._values) / self._window
            variance = sum((v - mean) ** 2 for v in self._values) / self._window
            std = math.sqrt(variance)
            if std > _MIN_STD:
                z = (value - mean) / std
        self._values.append(value)
        return z


class ExpandingPercentileRank:
    """Percentile rank of each sample against all prior samples (expanding).

    Returns ``None`` until ``min_samples`` prior observations exist; the
    caller decides the fail-closed behaviour for ``None``.
    """

    def __init__(self, min_samples: int) -> None:
        if min_samples < 1:
            msg = f"min_samples must be >= 1, got {min_samples}"
            raise ValueError(msg)
        self._min_samples = min_samples
        self._sorted: list[float] = []

    def update(self, value: float) -> float | None:
        rank: float | None = None
        if len(self._sorted) >= self._min_samples:
            rank = bisect_right(self._sorted, value) / len(self._sorted)
        insort(self._sorted, value)
        return rank
