"""
Execution engine package.

Orchestrates signal handling, order building, and broker routing for all strategy types.

Components:
- ExecutionEngine: Main orchestrator
- OrderBuilder: Builds order intents from signals
- OptionsOrderBuilder: Options spread construction
- FuturesOrderBuilder: Futures order building
- PositionSizer: Position sizing calculations
- PaperBroker: Paper trading simulation

Example:
    from execution_engine import ExecutionEngine

    engine = ExecutionEngine()
    result = await engine.handle_signal(
        user_id="user123",
        profile={"broker": "paper"},
        user_strategy_config={"execution_mode": "spot"},
        signal=signal,
    )
"""

from .brokers.base import (
    AccountInfo,
    BrokerAdapter,
    BrokerCapabilities,
    BrokerOrderResult,
    OrderStatus,
    Position,
)
from .brokers.paper import PaperBroker
from .config import (
    AccountState,
    BrokerType,
    ExecutionMode,
    FuturesConfig,
    OptionsConfig,
    SizingConfig,
    SizingMethod,
    UserExecutionConfig,
)
from .engine import ExecutionEngine, ExecutionResult
from .futures_builder import FuturesOrderBuilder, FuturesOrderResult
from .models import ExecutionRequest, OptionsIntent, OptionsLeg, OrderIntent
from .options_builder import OptionsOrderBuilder, SpreadResult
from .options_single_builder import OptionContractSpec, OptionsSingleOrderBuilder
from .order_builder import OrderBuilder
from .position_sizer import PositionSizer, SizingResult

__all__ = [
    "AccountInfo",
    "AccountState",
    "BrokerAdapter",
    "BrokerCapabilities",
    "BrokerOrderResult",
    "BrokerType",
    "ExecutionEngine",
    "ExecutionMode",
    "ExecutionRequest",
    "ExecutionResult",
    "FuturesConfig",
    "FuturesOrderBuilder",
    "FuturesOrderResult",
    "OptionContractSpec",
    "OptionsConfig",
    "OptionsIntent",
    "OptionsLeg",
    "OptionsOrderBuilder",
    "OptionsSingleOrderBuilder",
    "OrderBuilder",
    "OrderIntent",
    "OrderStatus",
    "PaperBroker",
    "Position",
    "PositionSizer",
    "SizingConfig",
    "SizingMethod",
    "SizingResult",
    "SpreadResult",
    "UserExecutionConfig",
]
