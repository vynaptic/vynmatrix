"""Broker adapter infrastructure.

This module provides the broker adapter framework for executing trades
across multiple brokers (Coinbase, Deribit, Interactive Brokers, etc.).

Components:
- ports: Abstract broker interface (IBrokerAdapter)
- base: Base adapter with common functionality
- secrets: Secrets provider interface and implementations
- factory: Broker adapter factory with connection pooling
- capabilities: Broker capability matrix for routing decisions
- adapters/: Concrete broker implementations
"""

from lib_infrastructure.brokers.base import (
    BaseBrokerAdapter,
    RateLimitConfig,
    RetryConfig,
)
from lib_infrastructure.brokers.capabilities import (
    BROKER_CAPABILITIES,
    BrokerCapabilityMatrix,
    get_broker_capabilities,
)
from lib_infrastructure.brokers.factory import (
    BrokerFactory,
    register_default_adapters,
)
from lib_infrastructure.brokers.ports import (
    BrokerFillRetrievalUnsupportedError,
    IBrokerAdapter,
    IBrokerAdapterFactory,
)
from lib_infrastructure.brokers.secrets import (
    BrokerCredentials,
    CompositeSecretsProvider,
    DbSecretsProvider,
    EnvSecretsProvider,
    ISecretsProvider,
    create_secrets_provider,
)

__all__ = [
    "BROKER_CAPABILITIES",
    "BaseBrokerAdapter",
    "BrokerCapabilityMatrix",
    "BrokerCredentials",
    "BrokerFactory",
    "BrokerFillRetrievalUnsupportedError",
    "CompositeSecretsProvider",
    "DbSecretsProvider",
    "EnvSecretsProvider",
    "IBrokerAdapter",
    "IBrokerAdapterFactory",
    "ISecretsProvider",
    "RateLimitConfig",
    "RetryConfig",
    "create_secrets_provider",
    "get_broker_capabilities",
    "register_default_adapters",
]
