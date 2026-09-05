"""Concrete broker adapter implementations.

Each adapter implements IBrokerAdapter for a specific broker.

Available adapters:
- CoinbaseAdapter: Coinbase Advanced Trade API (crypto spot)
- DeribitAdapter: Deribit API (crypto options/perpetuals)
- IBKRAdapter: Interactive Brokers Client Portal API (multi-asset)
- DeltaExchangeAdapter: Delta Exchange API (crypto options, India)
- SaxoAdapter: Saxo OpenAPI (global multi-asset)
- ZerodhaAdapter: Zerodha Kite API (Indian equities)

Usage:
    from lib_infrastructure.brokers.adapters.coinbase import CoinbaseAdapter
    adapter = CoinbaseAdapter(environment="paper")
    adapter.configure_settlement_currency("USD")
    await adapter.connect(api_key="xxx", api_secret="yyy", passphrase="zzz")
"""

# Adapters are imported lazily to avoid dependency issues
# Use: from lib_infrastructure.brokers.adapters.coinbase import CoinbaseAdapter

__all__ = [
    "CoinbaseAdapter",
    "DeltaExchangeAdapter",
    "DeribitAdapter",
    "IBKRAdapter",
    "SaxoAdapter",
    "ZerodhaAdapter",
]
