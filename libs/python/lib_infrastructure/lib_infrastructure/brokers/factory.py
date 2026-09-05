"""Broker adapter factory with connection pooling.

This module provides a factory for creating and managing broker adapters
with connection pooling, credential management, and automatic reconnection.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from typing import cast

from lib_common.logging import get_logger
from lib_infrastructure.brokers.capabilities import BROKER_SPECS
from lib_infrastructure.brokers.ports import IBrokerAdapter, IBrokerAdapterFactory
from lib_infrastructure.brokers.secrets import ISecretsProvider

logger = get_logger(__name__)


@dataclass
class CachedAdapter:
    """Cached adapter with metadata."""

    adapter: IBrokerAdapter
    credential_ref: str
    environment: str
    connected_at: datetime
    last_used: datetime = field(default_factory=lambda: datetime.now(UTC))


class BrokerFactory(IBrokerAdapterFactory):
    """Factory for broker adapters with connection pooling.

    Features:
    - Lazy adapter instantiation
    - Connection pooling and reuse
    - Automatic credential loading from secrets provider
    - Connection health checking
    - Graceful cleanup on shutdown

    Usage:
        >>> factory = BrokerFactory(secrets_provider)
        >>> factory.register_adapter("coinbase", CoinbaseAdapter)
        >>> adapter = await factory.get_adapter("coinbase", "coinbase-prod", "live")
        >>> adapter.configure_settlement_currency("USD")
        >>> result = await adapter.place_order(order)

    Thread Safety:
        The factory is thread-safe. Multiple coroutines can request
        adapters concurrently.
    """

    def __init__(
        self,
        secrets_provider: ISecretsProvider,
        max_idle_seconds: int = 300,
    ) -> None:
        """Initialize broker factory.

        Args:
            secrets_provider: Provider for broker credentials
            max_idle_seconds: Max time to keep idle connections (default 5 min)
        """
        self._secrets_provider = secrets_provider
        self._max_idle_seconds = max_idle_seconds

        # Registered adapter classes
        self._adapter_classes: dict[str, type[IBrokerAdapter]] = {}

        # Active adapter connections
        self._connections: dict[str, CachedAdapter] = {}
        self._connection_lock = asyncio.Lock()

    def register_adapter(
        self,
        broker_code: str,
        adapter_class: type[IBrokerAdapter],
    ) -> None:
        """Register an adapter class for a broker.

        Args:
            broker_code: Broker identifier (e.g., "coinbase")
            adapter_class: Class implementing IBrokerAdapter
        """
        logger.info("Registering adapter for broker: %s", broker_code)
        self._adapter_classes[broker_code] = adapter_class

    def register(self, broker_code: str) -> Callable[[type[IBrokerAdapter]], type[IBrokerAdapter]]:
        """Decorator to register adapter classes.

        Example:
            >>> @factory.register("coinbase")
            ... class CoinbaseAdapter(BaseBrokerAdapter):
            ...     pass
        """

        def decorator(adapter_class: type[IBrokerAdapter]) -> type[IBrokerAdapter]:
            self.register_adapter(broker_code, adapter_class)
            return adapter_class

        return decorator

    async def get_adapter(
        self,
        broker_code: str,
        credential_ref: str,
        environment: str = "paper",
    ) -> IBrokerAdapter:
        """Get or create a broker adapter.

        Returns a cached adapter if available and healthy, otherwise
        creates a new one.

        Args:
            broker_code: Broker identifier
            credential_ref: Reference to credentials in secrets provider
            environment: "paper" or "live"

        Returns:
            Connected broker adapter

        Raises:
            ValueError: If broker_code not registered
            ConnectionError: If connection fails
        """
        cache_key = self._make_cache_key(broker_code, credential_ref, environment)

        async with self._connection_lock:
            # Check for existing healthy connection
            if cache_key in self._connections:
                cached = self._connections[cache_key]
                cached.last_used = datetime.now(UTC)
                logger.debug("Reusing cached adapter: %s", cache_key)
                return cached.adapter

            # Create new adapter
            adapter = await self._create_adapter(broker_code, credential_ref, environment)

            # Cache it
            self._connections[cache_key] = CachedAdapter(
                adapter=adapter,
                credential_ref=credential_ref,
                environment=environment,
                connected_at=datetime.now(UTC),
            )

            return adapter

    async def _create_adapter(
        self,
        broker_code: str,
        credential_ref: str,
        environment: str,
    ) -> IBrokerAdapter:
        """Create and connect a new adapter."""
        # Get adapter class
        adapter_class = self._adapter_classes.get(broker_code)
        if not adapter_class:
            registered = list(self._adapter_classes.keys())
            msg = f"Unknown broker: {broker_code}. Registered brokers: {registered}"
            raise ValueError(msg)

        # Load credentials
        credentials = await self._secrets_provider.get_broker_credentials(credential_ref)
        if not credentials:
            msg = f"Failed to load credentials for: {credential_ref}"
            raise ValueError(msg)

        # Create and connect adapter
        logger.info(
            "Creating new adapter",
            broker=broker_code,
            environment=environment,
        )

        # The port deliberately does not constrain __init__; every concrete
        # adapter takes ``environment`` by convention (BaseBrokerAdapter).
        adapter_ctor = cast("Callable[..., IBrokerAdapter]", adapter_class)
        adapter: IBrokerAdapter = adapter_ctor(environment=environment)
        configure_loader = getattr(adapter, "configure_credential_loader", None)
        if callable(configure_loader):
            # OAuth adapters such as Saxo reload one atomically persisted
            # credential document before access-token expiry. They do not
            # perform a rotating refresh-token exchange inside the trading
            # process because a crash could otherwise lose the replacement.
            configure_loader(lambda: self._secrets_provider.get_broker_credentials(credential_ref))
        success = await adapter.connect(
            api_key=credentials.api_key,
            api_secret=credentials.api_secret,
            passphrase=credentials.passphrase,
            subaccount=credentials.subaccount,
            **(credentials.additional or {}),
        )

        if not success:
            msg = f"Failed to connect to {broker_code}"
            raise ConnectionError(msg)

        return adapter

    async def disconnect_adapter(
        self,
        broker_code: str,
        credential_ref: str,
        environment: str = "paper",
    ) -> None:
        """Disconnect and remove a specific adapter.

        Args:
            broker_code: Broker identifier
            credential_ref: Credential reference
            environment: Environment
        """
        cache_key = self._make_cache_key(broker_code, credential_ref, environment)

        async with self._connection_lock:
            if cache_key in self._connections:
                cached = self._connections.pop(cache_key)
                try:
                    await cached.adapter.disconnect()
                    logger.info("Disconnected adapter: %s", cache_key)
                except Exception as e:
                    logger.warning("Error disconnecting adapter %s: %s", cache_key, e)

    async def disconnect_all(self) -> None:
        """Disconnect all cached adapters.

        Call this on application shutdown.
        """
        logger.info("Disconnecting all broker adapters")

        async with self._connection_lock:
            for cache_key, cached in list(self._connections.items()):
                try:
                    await cached.adapter.disconnect()
                    logger.debug("Disconnected: %s", cache_key)
                except Exception as e:
                    logger.warning("Error disconnecting %s: %s", cache_key, e)

            self._connections.clear()

    async def cleanup_idle(self) -> int:
        """Clean up idle connections.

        Disconnects adapters that haven't been used for max_idle_seconds.

        Returns:
            Number of connections cleaned up
        """
        now = datetime.now(UTC)
        cleaned = 0

        async with self._connection_lock:
            to_remove = []

            for cache_key, cached in self._connections.items():
                idle_seconds = (now - cached.last_used).total_seconds()
                if idle_seconds > self._max_idle_seconds:
                    to_remove.append(cache_key)

            for cache_key in to_remove:
                cached = self._connections.pop(cache_key)
                try:
                    await cached.adapter.disconnect()
                    cleaned += 1
                    logger.debug("Cleaned up idle adapter: %s", cache_key)
                except Exception as e:
                    logger.warning("Error cleaning up %s: %s", cache_key, e)

        if cleaned > 0:
            logger.info("Cleaned up %s idle adapter(s)", cleaned)

        return cleaned

    def get_registered_brokers(self) -> list[str]:
        """Get list of registered broker codes."""
        return list(self._adapter_classes.keys())

    def get_active_connections(self) -> dict[str, dict]:
        """Get info about active connections.

        Returns:
            Dict mapping cache keys to connection info
        """
        return {
            key: {
                "broker": cached.adapter.broker_code,
                "environment": cached.environment,
                "connected_at": cached.connected_at.isoformat(),
                "last_used": cached.last_used.isoformat(),
            }
            for key, cached in self._connections.items()
        }

    @staticmethod
    def _make_cache_key(
        broker_code: str,
        credential_ref: str,
        environment: str,
    ) -> str:
        """Create cache key for adapter."""
        return f"{broker_code}:{credential_ref}:{environment}"


def register_default_adapters(factory: BrokerFactory) -> None:
    """Register all default broker adapters from the broker spec registry.

    Iterates the single ``BROKER_SPECS`` source of truth, importing each
    adapter lazily and tolerating an absent optional dependency (the adapter
    module's third-party SDK not being installed) so a partial install still
    boots. Adding a broker is a one-line ``BROKER_SPECS`` entry, not a new
    hand-written try/except block here.

    Args:
        factory: BrokerFactory instance
    """
    for spec in BROKER_SPECS:
        try:
            module = import_module(spec.adapter_module)
        except ImportError:
            logger.debug("%s adapter not available", spec.code)
            continue
        factory.register_adapter(spec.factory_code, getattr(module, spec.adapter_class))

    logger.info("Registered broker adapters: %s", factory.get_registered_brokers())
