"""Causal RSI, directional movement, and MACD using the shared smoothing stack."""

from __future__ import annotations

from typing import ClassVar

from .atr import AverageTrueRange
from .checkpoint import CheckpointedIndicator
from .ema import ExponentialMovingAverage
from .streaming_statistics import WilderAverage, finite_sample, validate_period

_PERCENT = 100.0
_NEUTRAL_RSI = 50.0


class RelativeStrengthIndex(CheckpointedIndicator):
    """Wilder RSI; flat windows are neutral and loss-free rises return 100.

    These defined limits intentionally replace the original unsafe division
    on monotone/flat data. The first output needs period+1 closes.
    """

    STATE_CONFIG = ("period",)
    STATE_FIELDS: ClassVar[dict[str, str]] = {
        "_gains": "indicator",
        "_losses": "indicator",
        "_previous": "number?",
        "value": "number?",
    }

    def __init__(self, period: int) -> None:
        self.period = validate_period(period)
        self._gains = WilderAverage(period)
        self._losses = WilderAverage(period)
        self._previous: float | None = None
        self.value: float | None = None

    def update(self, close: float) -> float | None:
        finite_sample(close)
        previous, self._previous = self._previous, close
        if previous is None:
            return None
        change = close - previous
        gain = self._gains.update(max(change, 0.0))
        loss = self._losses.update(max(-change, 0.0))
        if gain is None or loss is None:
            return None
        if loss == 0:
            self.value = _PERCENT if gain > 0 else _NEUTRAL_RSI
        else:
            self.value = _PERCENT - _PERCENT / (1.0 + gain / loss)
        return self.value

    def validate_checkpoint(self) -> None:
        gain, loss = self._gains.value, self._losses.value
        if (
            self._gains._samples != self._losses._samples
            or (self._gains._samples and self._previous is None)
            or any(value < 0 for value in self._gains._seed + self._losses._seed)
            or (gain is not None and gain < 0)
            or (loss is not None and loss < 0)
            or (gain is None) != (loss is None)
        ):
            msg = "RSI checkpoint has inconsistent gain/loss streams"
            raise ValueError(msg)
        expected = None
        if gain is not None and loss is not None:
            expected = (
                (_PERCENT if gain > 0 else _NEUTRAL_RSI)
                if loss == 0
                else _PERCENT - _PERCENT / (1.0 + gain / loss)
            )
        if self.value != expected:
            msg = "RSI checkpoint output disagrees with its recursive state"
            raise ValueError(msg)


class DirectionalMovement(CheckpointedIndicator):
    """Wilder DI+/DI-/ADX; first ADX needs 2*period OHLC bars.

    A zero true range or zero directional movement yields zero strength,
    rather than a division exception or an invented trend.
    """

    STATE_CONFIG = ("period",)
    STATE_FIELDS: ClassVar[dict[str, str]] = {
        "_atr": "indicator",
        "_plus": "indicator",
        "_minus": "indicator",
        "_dx": "indicator",
        "_previous": "pair?",
        "plus_di": "number?",
        "minus_di": "number?",
        "value": "number?",
    }

    def __init__(self, period: int) -> None:
        self.period = validate_period(period)
        self._atr = AverageTrueRange(period)
        self._plus = WilderAverage(period)
        self._minus = WilderAverage(period)
        self._dx = WilderAverage(period)
        self._previous: tuple[float, float] | None = None
        self.plus_di: float | None = None
        self.minus_di: float | None = None
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        for sample in (high, low, close):
            finite_sample(sample)
        previous, self._previous = self._previous, (high, low)
        atr = self._atr.update(high, low, close)
        if previous is None:
            return None
        up, down = high - previous[0], previous[1] - low
        plus = self._plus.update(up if up > down and up > 0 else 0.0)
        minus = self._minus.update(down if down > up and down > 0 else 0.0)
        if atr is None or plus is None or minus is None:
            return None
        self.plus_di = _PERCENT * plus / atr if atr > 0 else 0.0
        self.minus_di = _PERCENT * minus / atr if atr > 0 else 0.0
        total = self.plus_di + self.minus_di
        dx = _PERCENT * abs(self.plus_di - self.minus_di) / total if total > 0 else 0.0
        self.value = self._dx.update(dx)
        return self.value

    def validate_checkpoint(self) -> None:
        samples = self._plus._samples
        if (
            samples != self._minus._samples
            or self._dx._samples != max(0, samples - self.period + 1)
            or len(self._atr._seed) != min(samples, self.period)
            or (self._previous is None) != (self._atr._prev_close is None)
            or (samples and self._previous is None)
            or any(value < 0 for value in self._plus._seed + self._minus._seed + self._dx._seed)
        ):
            msg = "Directional checkpoint has inconsistent recursive streams"
            raise ValueError(msg)
        plus, minus, atr = self._plus.value, self._minus.value, self._atr.value
        expected_plus = expected_minus = None
        if plus is not None and minus is not None and atr is not None:
            if plus < 0 or minus < 0:
                msg = "Directional checkpoint has negative movement"
                raise ValueError(msg)
            expected_plus = _PERCENT * plus / atr if atr > 0 else 0.0
            expected_minus = _PERCENT * minus / atr if atr > 0 else 0.0
        if (
            self.plus_di != expected_plus
            or self.minus_di != expected_minus
            or self.value != self._dx.value
            or (self.value is not None and not 0 <= self.value <= _PERCENT)
        ):
            msg = "Directional checkpoint output disagrees with its recursive state"
            raise ValueError(msg)


class MACD(CheckpointedIndicator):
    """SMA-seeded fast/slow EMAs followed by an SMA-seeded signal EMA."""

    STATE_CONFIG = ()
    STATE_FIELDS: ClassVar[dict[str, str]] = {
        "_fast": "indicator",
        "_slow": "indicator",
        "_signal": "indicator",
        "value": "triple?",
    }

    def __init__(self, fast: int, slow: int, signal: int) -> None:
        for period in (fast, slow, signal):
            validate_period(period)
        if fast >= slow:
            msg = "MACD fast period must be smaller than slow period"
            raise ValueError(msg)
        self._fast = ExponentialMovingAverage(fast)
        self._slow = ExponentialMovingAverage(slow)
        self._signal = ExponentialMovingAverage(signal)
        self.value: tuple[float, float, float] | None = None

    def update(self, close: float) -> tuple[float, float, float] | None:
        finite_sample(close)
        fast = self._fast.update(close)
        slow = self._slow.update(close)
        if fast is None or slow is None:
            return None
        macd = fast - slow
        signal = self._signal.update(macd)
        if signal is not None:
            self.value = macd, signal, macd - signal
        return self.value

    def validate_checkpoint(self) -> None:
        samples = self._slow.samples_received
        if samples != self._fast.samples_received or self._signal.samples_received != max(
            0, samples - self._slow.period + 1
        ):
            msg = "MACD checkpoint has inconsistent recursive streams"
            raise ValueError(msg)
        fast, slow, signal = self._fast.value, self._slow.value, self._signal.value
        expected = None
        if fast is not None and slow is not None and signal is not None:
            expected = fast - slow, signal, fast - slow - signal
        if self.value != expected:
            msg = "MACD checkpoint output disagrees with its recursive state"
            raise ValueError(msg)
