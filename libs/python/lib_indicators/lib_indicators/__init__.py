"""Technical indicators library for the vynmatrix platform.

This module provides streaming technical indicators optimized for
real-time trading strategy development. All indicators use incremental
calculation for efficiency.

Available Indicators:
    - AverageTrueRange: Wilder ATR (backtrader-parity)
    - ExponentialMovingAverage: Streaming exponential mean
    - PriceMomentumOscillator: Double-smoothed momentum oscillator
    - SimpleMovingAverage: Rolling arithmetic mean
    - SwingHighLow / SwingPoint: Swing high/low detection
    - Vortex: Positive/negative trend movement

Retention rationale (owner decision 2026-07-29): ``AverageTrueRange``,
``SimpleMovingAverage``, and ``Vortex`` currently have no strategy consumer
but are deliberately retained with backtrader-parity coverage
(``tests/test_vortex_indicator_parity.py``) as validated building blocks.
``ExpandingPercentileRank``/``RollingZScore``/``TrendConfirmGate`` back the
dev_cli equity validation framework and the active SP500 rotation change.
"""

__version__ = "0.1.0"

from .atr import AverageTrueRange
from .ema import ExponentialMovingAverage
from .pmo import PriceMomentumOscillator
from .regime_gates import ExpandingPercentileRank, RollingZScore, TrendConfirmGate
from .sma import SimpleMovingAverage
from .swing_high_low import SwingHighLow, SwingPoint
from .vortex import Vortex

__all__ = [
    "AverageTrueRange",
    "ExpandingPercentileRank",
    "ExponentialMovingAverage",
    "PriceMomentumOscillator",
    "RollingZScore",
    "SimpleMovingAverage",
    "SwingHighLow",
    "SwingPoint",
    "TrendConfirmGate",
    "Vortex",
]
