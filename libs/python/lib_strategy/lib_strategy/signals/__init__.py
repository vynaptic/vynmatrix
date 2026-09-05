"""
Unified signal infrastructure for indicator strategies.

This module provides:
- Signal: Canonical signal dataclass used by indicator strategies
- SignalAction: Enum for signal actions (LONG, SHORT, CLOSE, HOLD)
- SignalEmitter: Abstract interface for emitting signals to different backends
- Concrete emitters: BacktestEmitter, LogEmitter, HttpEmitter
- PureSignalStrategy: Base class for framework-agnostic strategies
- MarketState, StrategyState: Data classes for strategy state management
"""

from lib_strategy.signals.adapters.scoring import ScoringSignalView, build_scoring_view
from lib_strategy.signals.emitter import (
    BacktestSignalEmitter,
    HttpSignalEmitter,
    LogSignalEmitter,
    SignalEmitter,
)
from lib_strategy.signals.normalization import (
    is_known_signal_action,
    normalize_scoring_action,
    normalize_signal_action,
)
from lib_strategy.signals.pure_strategy import (
    MarketState,
    PureSignalStrategy,
    StrategyState,
)
from lib_strategy.signals.signal import Signal, SignalAction
from lib_strategy.signals.utils import (
    compute_stable_signal_id,
    ensure_utc,
)

__all__ = [
    "BacktestSignalEmitter",
    "HttpSignalEmitter",
    "LogSignalEmitter",
    "MarketState",
    "PureSignalStrategy",
    "ScoringSignalView",
    "Signal",
    "SignalAction",
    "SignalEmitter",
    "StrategyState",
    "build_scoring_view",
    "compute_stable_signal_id",
    "ensure_utc",
    "is_known_signal_action",
    "normalize_scoring_action",
    "normalize_signal_action",
]
