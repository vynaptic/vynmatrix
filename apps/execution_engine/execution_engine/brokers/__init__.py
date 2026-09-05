"""
Broker adapters for the execution engine.

Provides a unified interface for executing orders across different brokers:
- Paper trading (simulation)
- Coinbase (crypto spot)
- Deribit (crypto options/futures)
- Interactive Brokers (multi-asset)
- Delta Exchange (crypto derivatives)
- Zerodha (Indian markets)
"""

from execution_engine.brokers.base import (
    BrokerAdapter,
    BrokerCapabilities,
    BrokerOrderResult,
    OrderStatus,
)

__all__ = [
    "BrokerAdapter",
    "BrokerCapabilities",
    "BrokerOrderResult",
    "OrderStatus",
]
