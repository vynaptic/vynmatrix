"""Base broker adapter with common functionality.

This module provides a base class for broker adapters that implements
common functionality like rate limiting, retry logic, logging, and
fail-closed environment handling.
"""

import asyncio
import time
from abc import abstractmethod
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar, cast

from lib_common.logging import get_logger
from lib_common.retries import retry_async
from lib_infrastructure.brokers.ports import (
    BrokerFillRetrievalUnsupportedError,
    IBrokerAdapter,
)
from lib_strategy.types import (
    AccountBalance,
    BrokerAccountSnapshot,
    BrokerFill,
    BrokerOrderIntent,
    BrokerOrderResult,
    BrokerPosition,
)

logger = get_logger(__name__)
_T = TypeVar("_T")


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""

    requests_per_second: float = 10.0
    requests_per_minute: float = 300.0
    burst_limit: int = 20


@dataclass
class RetryConfig:
    """Configuration for retry logic."""

    max_retries: int = 3
    base_delay: float = 1.0  # seconds
    max_delay: float = 30.0  # seconds
    exponential_base: float = 2.0
    retryable_errors: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError)


class BaseBrokerAdapter(IBrokerAdapter):
    """Base class for broker adapters with common functionality.

    Provides:
    - Rate limiting with token bucket algorithm
    - Retry logic with exponential backoff
    - Request/response logging
    - Fail-closed rejection when a broker has no native paper endpoint
    - Error normalization to BrokerOrderResult

    Subclasses must implement:
    - broker_code, supported_asset_classes, supported_execution_methods
    - supports_paper_trading, supports_multi_leg_orders, capabilities
    - _do_connect(), _do_disconnect()
    - _do_place_order(), _do_cancel_order(), _do_get_order_status()
    - _do_get_account_balance(), _do_get_account_snapshot()
    - _do_get_open_orders(), _do_get_positions()
    """

    def __init__(
        self,
        environment: str = "paper",
        rate_limit_config: RateLimitConfig | None = None,
        retry_config: RetryConfig | None = None,
    ) -> None:
        """Initialize base adapter.

        Args:
            environment: "paper" or "live"
            rate_limit_config: Rate limiting configuration
            retry_config: Retry configuration
        """
        self.environment = environment
        self._rate_limit_config = rate_limit_config or RateLimitConfig()
        self._retry_config = retry_config or RetryConfig()

        # Connection state
        self._connected = False
        self._api_key: str | None = None

        # Rate limiting state (token bucket)
        self._request_times: deque[float] = deque(maxlen=1000)
        self._rate_limit_lock = asyncio.Lock()

    # =========================================================================
    # Abstract methods (broker-specific implementations)
    # =========================================================================

    @abstractmethod
    async def _do_connect(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str | None = None,
        subaccount: str | None = None,
        **kwargs: str,
    ) -> bool:
        """Broker-specific connection logic."""

    @abstractmethod
    async def _do_disconnect(self) -> None:
        """Broker-specific disconnection logic."""

    @abstractmethod
    async def _do_place_order(self, order: BrokerOrderIntent) -> BrokerOrderResult:
        """Broker-specific order placement."""

    @abstractmethod
    async def _do_cancel_order(self, broker_order_id: str) -> bool:
        """Broker-specific order cancellation."""

    @abstractmethod
    async def _do_get_order_status(self, broker_order_id: str) -> BrokerOrderResult:
        """Broker-specific order status retrieval."""

    async def _do_get_fills(self, broker_order_id: str) -> list[BrokerFill]:
        """Fail closed until an adapter implements exact trade retrieval."""
        msg = (
            f"Broker {self.broker_code} cannot provide certification-grade "
            f"fills for order {broker_order_id}"
        )
        raise BrokerFillRetrievalUnsupportedError(msg)

    @abstractmethod
    async def _do_get_account_balance(self) -> list[AccountBalance]:
        """Broker-specific balance retrieval."""

    @abstractmethod
    async def _do_get_account_snapshot(self) -> BrokerAccountSnapshot:
        """Broker-specific atomic account snapshot retrieval."""

    @abstractmethod
    async def _do_get_open_orders(self, symbol: str | None = None) -> list[BrokerOrderResult]:
        """Broker-specific open orders retrieval."""

    @abstractmethod
    async def _do_get_positions(self) -> list[BrokerPosition]:
        """Broker-specific positions retrieval."""

    # =========================================================================
    # IBrokerAdapter implementation with common logic
    # =========================================================================

    async def connect(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str | None = None,
        subaccount: str | None = None,
        **kwargs: str,
    ) -> bool:
        """Connect to broker with logging and error handling."""
        logger.info(
            "Connecting to broker",
            broker=self.broker_code,
            environment=self.environment,
        )

        try:
            if self.environment == "paper" and not self.supports_paper_trading:
                logger.error(
                    "Broker has no native paper endpoint; refusing adapter connection",
                    broker=self.broker_code,
                )
                return False

            # Real connection
            result = await self._do_connect(api_key, api_secret, passphrase, subaccount, **kwargs)

        except Exception:
            logger.exception(
                "Error connecting to broker",
                broker=self.broker_code,
            )
            return False
        else:
            if result:
                self._connected = True
                self._api_key = api_key
                logger.info("Connected to broker", broker=self.broker_code)
            else:
                logger.warning("Failed to connect to broker", broker=self.broker_code)

            return result

    async def disconnect(self) -> None:
        """Disconnect from broker."""
        if not self._connected:
            return

        logger.info("Disconnecting from broker", broker=self.broker_code)

        try:
            await self._do_disconnect()
        except Exception as e:
            logger.warning(
                "Error disconnecting from broker",
                broker=self.broker_code,
                error=str(e),
            )
        finally:
            self._connected = False
            self._api_key = None

    async def get_account_balance(self) -> list[AccountBalance]:
        """Get account balance with rate limiting."""
        self._ensure_connected()
        await self._rate_limit()

        return cast(list[AccountBalance], await self._with_retry(self._do_get_account_balance))

    async def get_account_snapshot(self) -> BrokerAccountSnapshot:
        """Get account valuation inputs with rate limiting and retry."""
        self._ensure_connected()
        await self._rate_limit()

        return cast(
            BrokerAccountSnapshot,
            await self._with_retry(self._do_get_account_snapshot),
        )

    async def place_order(self, order: BrokerOrderIntent) -> BrokerOrderResult:
        """Place order with rate limiting, logging, and retry."""
        self._ensure_connected()
        await self._rate_limit()

        logger.info(
            "Placing order",
            broker=self.broker_code,
            symbol=order.get("symbol"),
            side=order.get("side"),
            quantity=order.get("quantity"),
            order_type=order.get("order_type"),
        )

        try:
            result = await self._with_retry(lambda: self._do_place_order(order))

        except Exception as e:
            logger.exception(
                "Error placing order",
                broker=self.broker_code,
            )
            return self._error_result(str(e))
        else:
            self._log_order_result(order, result)
            return result

    async def place_multi_leg_order(self, _legs: list[BrokerOrderIntent]) -> BrokerOrderResult:
        """Reject adapters without an explicit native multi-leg implementation."""
        if not self.supports_multi_leg_orders:
            msg = f"Broker {self.broker_code} does not support native multi-leg orders"
            raise NotImplementedError(msg)
        msg = (
            f"Broker {self.broker_code} advertises native multi-leg support "
            "but does not implement it"
        )
        raise NotImplementedError(msg)

    async def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel order with rate limiting."""
        self._ensure_connected()
        await self._rate_limit()

        logger.info(
            "Canceling order",
            broker=self.broker_code,
            order_id=broker_order_id,
        )

        return cast(bool, await self._with_retry(lambda: self._do_cancel_order(broker_order_id)))

    async def get_order_status(self, broker_order_id: str) -> BrokerOrderResult:
        """Get order status with rate limiting."""
        self._ensure_connected()
        await self._rate_limit()

        return cast(
            BrokerOrderResult,
            await self._with_retry(lambda: self._do_get_order_status(broker_order_id)),
        )

    async def get_fills(self, broker_order_id: str) -> list[BrokerFill]:
        """Get exact venue fills with rate limiting and retry."""
        self._ensure_connected()
        await self._rate_limit()

        return cast(
            list[BrokerFill],
            await self._with_retry(lambda: self._do_get_fills(broker_order_id)),
        )

    async def get_open_orders(self, symbol: str | None = None) -> list[BrokerOrderResult]:
        """Get open orders with rate limiting."""
        self._ensure_connected()
        await self._rate_limit()

        return cast(
            list[BrokerOrderResult],
            await self._with_retry(lambda: self._do_get_open_orders(symbol)),
        )

    async def get_positions(self) -> list[BrokerPosition]:
        """Get positions with rate limiting."""
        self._ensure_connected()
        await self._rate_limit()

        return cast(list[BrokerPosition], await self._with_retry(self._do_get_positions))

    # =========================================================================
    # Rate limiting
    # =========================================================================

    async def _rate_limit(self) -> None:
        """Apply rate limiting using token bucket algorithm."""
        async with self._rate_limit_lock:
            now = time.time()

            # Clean old request times
            cutoff_second = now - 1.0
            cutoff_minute = now - 60.0

            while self._request_times and self._request_times[0] < cutoff_minute:
                self._request_times.popleft()

            # Count recent requests
            requests_last_second = sum(1 for t in self._request_times if t >= cutoff_second)
            requests_last_minute = len(self._request_times)

            # Check limits
            if requests_last_second >= self._rate_limit_config.requests_per_second:
                wait_time = 1.0 - (now - self._request_times[-1])
                if wait_time > 0:
                    logger.debug("Rate limit (per-second): waiting %.2fs", wait_time)
                    await asyncio.sleep(wait_time)

            if requests_last_minute >= self._rate_limit_config.requests_per_minute:
                oldest_in_minute = next(
                    (t for t in self._request_times if t >= cutoff_minute),
                    now,
                )
                wait_time = 60.0 - (now - oldest_in_minute)
                if wait_time > 0:
                    logger.warning("Rate limit (per-minute): waiting %.2fs", wait_time)
                    await asyncio.sleep(wait_time)

            # Record this request
            self._request_times.append(time.time())

    # =========================================================================
    # Retry logic
    # =========================================================================

    async def _with_retry(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        """Execute an asynchronous broker operation with the shared retry policy."""
        return cast(
            _T,
            await retry_async(
                operation,
                retries=self._retry_config.max_retries,
                base_delay=self._retry_config.base_delay,
                max_delay=self._retry_config.max_delay,
                exponential_base=self._retry_config.exponential_base,
                jitter=0,
                retry_on=self._retry_config.retryable_errors,
                operation_name=f"{self.broker_code} broker operation",
                log=logger,
            ),
        )

    # =========================================================================
    # Helper methods
    # =========================================================================

    def _ensure_connected(self) -> None:
        """Ensure adapter is connected."""
        if not self._connected:
            msg = f"Not connected to {self.broker_code}"
            raise ConnectionError(msg)

    def _error_result(self, message: str, error_code: str | None = None) -> BrokerOrderResult:
        """Create error result."""
        result: BrokerOrderResult = {
            "success": False,
            "status": "rejected",
            "filled_quantity": "0",
            "error_message": message,
        }
        if error_code is not None:
            result["error_code"] = error_code
        return result

    def _log_order_result(self, order: BrokerOrderIntent, result: BrokerOrderResult) -> None:
        """Log order result."""
        if result.get("success"):
            logger.info(
                "Order placed successfully",
                broker=self.broker_code,
                order_id=result.get("broker_order_id"),
                status=result.get("status"),
                filled_quantity=result.get("filled_quantity"),
            )
        else:
            logger.warning(
                "Order failed",
                broker=self.broker_code,
                error=result.get("error_message"),
                symbol=order.get("symbol"),
            )
