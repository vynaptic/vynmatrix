"""Vortex Indicator (VI+ / VI-) — backtrader-compatible.

backtrader's ``bt.indicators.Vortex``::

    VM+_t = |high_t - low_{t-1}|
    VM-_t = |low_t - high_{t-1}|
    TR_t  = max(high_t, close_{t-1}) - min(low_t, close_{t-1})
    VI+   = SumN(VM+, period) / SumN(TR, period)
    VI-   = SumN(VM-, period) / SumN(TR, period)

Rolling SUMS (not smoothed averages). First value once ``period`` complete
(VM, TR) samples exist — i.e. on bar ``period + 1`` of the feed.
"""

from __future__ import annotations

import math
from collections import deque
from typing import ClassVar

from .checkpoint import CheckpointedIndicator


class Vortex(CheckpointedIndicator):
    """Vortex VI+/VI- using rolling sums (backtrader-compatible).

    Attributes:
        period: Rolling-sum window.
        vi_plus: Current VI+ (None until warmed up).
        vi_minus: Current VI- (None until warmed up).
        is_ready: True once ``period`` VM/TR samples accumulated.

    Example::

        vx = Vortex(period=14)
        for bar in bars:
            vx.update(high=bar.high, low=bar.low, close=bar.close)
            if vx.is_ready:
                print(vx.vi_plus, vx.vi_minus)
    """

    STATE_CONFIG = ("_period",)
    STATE_FIELDS: ClassVar[dict[str, str]] = {
        "_prev_high": "number?",
        "_prev_low": "number?",
        "_prev_close": "number?",
        "_vm_plus": "numbers",
        "_vm_minus": "numbers",
        "_tr": "numbers",
        "vi_plus": "number?",
        "vi_minus": "number?",
    }

    def __init__(self, period: int) -> None:
        if period < 1:
            msg = f"period must be >= 1, got {period}"
            raise ValueError(msg)
        self._period = period
        self._prev_high: float | None = None
        self._prev_low: float | None = None
        self._prev_close: float | None = None
        self._vm_plus: deque[float] = deque(maxlen=period)
        self._vm_minus: deque[float] = deque(maxlen=period)
        self._tr: deque[float] = deque(maxlen=period)
        self.vi_plus: float | None = None
        self.vi_minus: float | None = None

    @property
    def period(self) -> int:
        return self._period

    @property
    def is_ready(self) -> bool:
        return self.vi_plus is not None

    def update(self, high: float, low: float, close: float) -> float | None:
        """Feed an OHLC bar; returns VI+ (None during warmup)."""
        if self._prev_close is None:
            self._prev_high, self._prev_low, self._prev_close = high, low, close
            return None
        # _prev_high/_prev_low are set together with _prev_close above; narrow
        # them for the type checker (the guard only mentions _prev_close).
        assert self._prev_high is not None
        assert self._prev_low is not None
        self._vm_plus.append(abs(high - self._prev_low))
        self._vm_minus.append(abs(low - self._prev_high))
        self._tr.append(max(high, self._prev_close) - min(low, self._prev_close))
        self._prev_high, self._prev_low, self._prev_close = high, low, close
        if len(self._tr) == self._period:
            tr_sum = math.fsum(self._tr)
            if tr_sum > 0:
                self.vi_plus = math.fsum(self._vm_plus) / tr_sum
                self.vi_minus = math.fsum(self._vm_minus) / tr_sum
            else:
                self.vi_plus = None
                self.vi_minus = None
        return self.vi_plus

    def validate_checkpoint(self) -> None:
        size = len(self._tr)
        ready = size == self.period and math.fsum(self._tr) > 0
        if (
            len(self._vm_plus) != size
            or len(self._vm_minus) != size
            or ready != (self.vi_plus is not None)
            or ready != (self.vi_minus is not None)
            or len({value is None for value in (self._prev_high, self._prev_low, self._prev_close)})
            != 1
            or (size and self._prev_close is None)
        ):
            msg = "Vortex checkpoint readiness or window coverage is invalid"
            raise ValueError(msg)

    def reset(self) -> None:
        self._prev_high = None
        self._prev_low = None
        self._prev_close = None
        self._vm_plus.clear()
        self._vm_minus.clear()
        self._tr.clear()
        self.vi_plus = None
        self.vi_minus = None

    def __repr__(self) -> str:
        if self.is_ready:
            return f"Vortex(period={self._period}, vi+={self.vi_plus:.4f}, vi-={self.vi_minus:.4f})"
        return f"Vortex(period={self._period}, warming up)"
