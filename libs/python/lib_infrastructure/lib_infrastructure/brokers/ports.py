"""Broker adapter port (abstract interface).

This module defines the abstract interface for broker adapters.
All concrete broker implementations must implement IBrokerAdapter.

The interface is designed to be:
- Broker-agnostic: Same interface for all brokers
- Async-first: All operations are async for non-blocking I/O
- Type-safe: Uses TypedDict types from lib_strategy

Note on execution method types:
- BrokerExecutionMethod: Broker-level routing (spot, perpetual, options_single, options_strategy)
- ExecutionMode: User-level routing (from lib_common.config_validation)

Broker adapters use BrokerExecutionMethod since they deal with broker-specific capabilities.
"""

from abc import ABC, abstractmethod

from lib_strategy.types import (
    AccountBalance,
    BrokerAccountSnapshot,
    BrokerCapabilities,
    BrokerExecutionMethod,
    BrokerFill,
    BrokerOrderIntent,
    BrokerOrderResult,
    BrokerPosition,
)


class BrokerFillRetrievalUnsupportedError(RuntimeError):
    """A broker cannot provide certification-grade exact fill economics."""


class IBrokerAdapter(ABC):
    """Abstract interface for broker adapters.

    All broker adapters must implement this interface to ensure
    consistent behavior across different brokers.

    Lifecycle:
        1. Create adapter instance with environment (paper/live)
        2. Call connect() with credentials
        3. Use place_order(), cancel_order(), etc.
        4. Call disconnect() when done

    Thread Safety:
        Adapters should be thread-safe for concurrent order operations.
        Connection management may require synchronization.

    Error Handling:
        - connect() returns False if authentication fails
        - place_order() returns BrokerOrderResult with success=False on error
        - cancel_order() returns False if order not found or cancel fails
        - All methods may raise exceptions for network/unexpected errors

    Example:
        >>> adapter = CoinbaseAdapter(environment="paper")
        >>> adapter.configure_settlement_currency("USD")
        >>> await adapter.connect(api_key="xxx", api_secret="yyy")
        True
        >>> result = await adapter.place_order({
        ...     "symbol": "BTC-USD",
        ...     "side": "buy",
        ...     "quantity": "0.1",
        ...     "order_type": "market",
        ...     "execution_method": "spot"
        ... })
        >>> result["success"]
        True
        >>> await adapter.disconnect()
    """

    @property
    @abstractmethod
    def broker_code(self) -> str:
        """Unique broker identifier.

        This must match the Broker.code in the database.

        Returns:
            Broker code string (e.g., "coinbase", "deribit", "interactive_brokers")
        """

    @property
    @abstractmethod
    def supported_asset_classes(self) -> list[str]:
        """Asset classes this broker supports.

        Returns:
            List of asset class strings (e.g., ["crypto"], ["equity", "options"])
        """

    @property
    @abstractmethod
    def supported_execution_methods(self) -> list[BrokerExecutionMethod]:
        """Execution methods this broker supports at the broker level.

        Returns:
            List of BrokerExecutionMethod enum values (SPOT, PERPETUAL, OPTIONS_STRATEGY, etc.)
        """

    @property
    @abstractmethod
    def supports_paper_trading(self) -> bool:
        """Whether this broker has native paper trading support.

        Returns:
            True if broker has sandbox/testnet/paper mode
        """

    @property
    @abstractmethod
    def supports_multi_leg_orders(self) -> bool:
        """Whether this broker supports multi-leg option orders.

        If True, place_multi_leg_order() sends a single atomic order.
        If False, legs must be executed sequentially.

        Returns:
            True if broker supports combo/spread orders
        """

    @property
    @abstractmethod
    def capabilities(self) -> BrokerCapabilities:
        """Full broker capabilities for capability detection.

        Returns:
            BrokerCapabilities TypedDict with all supported features
        """

    @abstractmethod
    async def connect(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str | None = None,
        subaccount: str | None = None,
        **kwargs: str,
    ) -> bool:
        """Establish connection to broker.

        Authenticates with the broker API and prepares for trading.
        Must be called before any trading operations.

        Args:
            api_key: API key for authentication
            api_secret: API secret for signing requests
            passphrase: API passphrase (required by some brokers like Coinbase)
            subaccount: Subaccount name (for brokers supporting subaccounts)
            **kwargs: Additional broker-specific parameters

        Returns:
            True if authentication successful, False otherwise

        Raises:
            ConnectionError: If network error occurs
            ValueError: If credentials format is invalid
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection to broker.

        Cleans up any resources (websockets, session pools, etc.).
        Safe to call multiple times.
        """

    @abstractmethod
    async def get_account_balance(self) -> list[AccountBalance]:
        """Get account balances for all currencies.

        Returns:
            List of AccountBalance for each currency in the account

        Raises:
            ConnectionError: If not connected
            RuntimeError: If API call fails
        """

    @abstractmethod
    async def get_account_snapshot(self) -> BrokerAccountSnapshot:
        """Get positions and authoritative account valuation inputs atomically.

        Implementations must set ``equity_source='unavailable'`` when their API
        cannot provide safe equity and the account is not a marked spot account.
        Consumers must fail closed for that source.
        """

    @abstractmethod
    async def place_order(self, order: BrokerOrderIntent) -> BrokerOrderResult:
        """Place a single order.

        Translates the broker-agnostic BrokerOrderIntent to broker-specific
        format and submits the order.

        Args:
            order: Broker-agnostic order specification

        Returns:
            BrokerOrderResult with execution status

        Notes:
            - For market orders, filled_price may differ from expected
            - For limit orders, order may be partially filled
            - client_order_id in order is preserved in result if provided
        """

    @abstractmethod
    async def place_multi_leg_order(self, legs: list[BrokerOrderIntent]) -> BrokerOrderResult:
        """Place a multi-leg order (for options strategies).

        If the broker supports multi-leg orders natively, submits as
        a single atomic order. Otherwise, raises NotImplementedError.

        Args:
            legs: List of order intents for each leg

        Returns:
            BrokerOrderResult for the combined order

        Raises:
            NotImplementedError: If broker doesn't support multi-leg orders
            ValueError: If legs are invalid for a spread
        """

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel an open order.

        Args:
            broker_order_id: The broker's order identifier

        Returns:
            True if cancel successful, False if order not found or already filled
        """

    @abstractmethod
    async def get_order_status(self, broker_order_id: str) -> BrokerOrderResult:
        """Get current status of an order.

        Args:
            broker_order_id: The broker's order identifier

        Returns:
            BrokerOrderResult with current status

        Raises:
            ValueError: If order not found
        """

    @abstractmethod
    async def get_fills(self, broker_order_id: str) -> list[BrokerFill]:
        """Return exact executions generated by one broker order.

        Every returned row must carry the venue's stable trade identifier,
        execution timestamp, price, quantity, fee amount, and fee currency.
        Adapters whose official API cannot provide that complete contract must
        raise :class:`BrokerFillRetrievalUnsupportedError`.
        """

    @abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[BrokerOrderResult]:
        """Get all open orders.

        Args:
            symbol: Optional filter by symbol

        Returns:
            List of BrokerOrderResult for open orders
        """

    @abstractmethod
    async def get_positions(self) -> list[BrokerPosition]:
        """Get current positions.

        Returns:
            List of position dictionaries with symbol, quantity, avg_price, etc.
        """


class IBrokerAdapterFactory(ABC):
    """Abstract factory for creating broker adapters.

    Implementations should handle:
    - Adapter instantiation based on broker code
    - Connection pooling and reuse
    - Credential management via secrets provider
    """

    @abstractmethod
    async def get_adapter(
        self,
        broker_code: str,
        credential_ref: str,
        environment: str = "paper",
    ) -> IBrokerAdapter:
        """Get or create a broker adapter.

        Args:
            broker_code: Broker identifier (e.g., "coinbase")
            credential_ref: Reference to credentials in secrets provider
            environment: "paper" or "live"

        Returns:
            Connected broker adapter instance
        """

    @abstractmethod
    def register_adapter(
        self,
        broker_code: str,
        adapter_class: type,
    ) -> None:
        """Register an adapter class for a broker.

        Args:
            broker_code: Broker identifier
            adapter_class: Class implementing IBrokerAdapter
        """
