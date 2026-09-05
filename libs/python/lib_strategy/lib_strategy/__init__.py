"""
Trading strategy framework - DOMAIN LAYER ONLY.

This module provides pure business logic with ZERO external dependencies:
- Domain execution entities
- Option spread calculations
- Type definitions
- Abstract interfaces (ports)
- Unified signal infrastructure (Signal, SignalEmitter, PureSignalStrategy)

NO database, NO frameworks, PURE Python.

For infrastructure implementations (database, etc.), see lib_infrastructure.
For use cases and orchestration, see lib_application.

Signal Infrastructure:
    The canonical Signal is defined in lib_strategy.signals.signal.
    Use `from lib_strategy.signals import Signal, SignalAction` for new code.
    All strategies subclass
    ``lib_strategy.signals.pure_strategy.PureSignalStrategy``.
"""

# Domain entities
from lib_strategy.domain import ExecutionLog

# Ports (abstract interfaces)
from lib_strategy.ports import IExecutionRepository
from lib_strategy.signals.emitter import (
    BacktestSignalEmitter,
    HttpSignalEmitter,
    LogSignalEmitter,
    SignalEmitter,
)
from lib_strategy.signals.pure_strategy import (
    MarketState,
    PureSignalStrategy,
    StrategyState,
)

# Unified signal infrastructure
from lib_strategy.signals.signal import Signal, SignalAction

# Option spreads (pure calculations)
from lib_strategy.spreads import build_spread

# Type definitions
from lib_strategy.types import (
    AccountBalance,
    BrokerCapabilities,
    BrokerOrderIntent,
    BrokerOrderResult,
    OptionLeg,
    OptionSide,
    OptionSpread,
    OptionType,
    SpreadType,
)

__all__ = [
    "AccountBalance",
    "BacktestSignalEmitter",
    "BrokerCapabilities",
    "BrokerOrderIntent",
    "BrokerOrderResult",
    "ExecutionLog",
    "HttpSignalEmitter",
    "IExecutionRepository",
    "LogSignalEmitter",
    "MarketState",
    "OptionLeg",
    "OptionSide",
    "OptionSpread",
    "OptionType",
    "PureSignalStrategy",
    "Signal",
    "SignalAction",
    "SignalEmitter",
    "SpreadType",
    "StrategyState",
    "build_spread",
]
