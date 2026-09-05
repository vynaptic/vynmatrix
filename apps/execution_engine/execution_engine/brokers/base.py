"""
Base broker adapter interface.

All broker implementations must inherit from BrokerAdapter
and implement the required abstract methods.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from execution_engine.config import BrokerType, ExecutionMode
from execution_engine.models import OptionsIntent, OrderIntent
from lib_strategy.types import AccountBalance, PositionQuantityUnit


class OrderStatus(Enum):
    """Order status states."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    WORKING = "working"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class BrokerCapabilities:
    """
    Describes what a broker can do.

    Used for routing decisions and validation.
    """

    broker_type: BrokerType
    supports_spot: bool = True
    supports_margin: bool = False
    supports_futures: bool = False
    supports_perpetual: bool = False
    supports_options: bool = False
    supports_multi_leg: bool = False  # Multi-leg options orders
    supports_exact_fill_retrieval: bool = False

    supported_assets: list[str] = field(default_factory=lambda: ["crypto"])
    supported_symbols: list[str] = field(default_factory=list)  # Empty = all

    max_leverage: float = 1.0
    min_order_size: float = 0.0
    max_order_size: float | None = None

    has_paper_trading: bool = True
    paper_base_url: str | None = None

    def supports_mode(self, mode: ExecutionMode) -> bool:  # noqa: PLR0911
        """Check if broker supports an execution mode."""
        if mode == ExecutionMode.SPOT:
            return self.supports_spot
        if mode == ExecutionMode.MARGIN:
            return self.supports_margin
        if mode in (ExecutionMode.PERPETUAL, ExecutionMode.FUTURES):
            return self.supports_futures or self.supports_perpetual
        if ExecutionMode.is_options(mode):
            return self.supports_options
        if mode == ExecutionMode.PAPER:
            return self.has_paper_trading
        if mode == ExecutionMode.NOTIFY_ONLY:  # noqa: SIM103
            return True
        return False


@dataclass
class BrokerOrderResult:
    """Execution-engine order-status DTO.

    This application shape is intentionally distinct from
    ``lib_strategy.types.BrokerOrderResult``, the decimal-preserving adapter
    wire contract.  ``broker_bridge.from_broker_order_result`` is the sole
    conversion boundary.
    """

    success: bool
    order_id: str | None = None
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: float = 0.0
    filled_price: float | None = None
    commission: Decimal = Decimal("0")
    commission_currency: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    error_message: str | None = None
    error_code: str | None = None
    raw_response: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Keep broker-reported money exact after crossing the adapter boundary."""
        if not isinstance(self.commission, Decimal):
            self.commission = Decimal(str(self.commission))
        if not self.commission.is_finite():
            msg = f"Invalid broker commission: {self.commission!r}"
            raise ValueError(msg)
        if self.commission_currency:
            self.commission_currency = self.commission_currency.strip().upper() or None

    @classmethod
    def rejected(cls, message: str, code: str | None = None) -> "BrokerOrderResult":
        """Create a rejected order result."""
        return cls(
            success=False,
            status=OrderStatus.REJECTED,
            error_message=message,
            error_code=code,
        )

    @classmethod
    def filled(
        cls,
        order_id: str,
        quantity: float,
        price: float,
        commission: Decimal | float = Decimal("0"),
        commission_currency: str | None = None,
    ) -> "BrokerOrderResult":
        """Create a filled order result."""
        return cls(
            success=True,
            order_id=order_id,
            status=OrderStatus.FILLED,
            filled_quantity=quantity,
            filled_price=price,
            commission=Decimal(str(commission)),
            commission_currency=commission_currency,
        )

    @classmethod
    def accepted(cls, order_id: str) -> "BrokerOrderResult":
        """Create an accepted-but-resting order result (working, not yet filled).

        Used for stop/limit orders that a broker holds until the market
        triggers them; they are not realized trades on submission.
        """
        return cls(success=True, order_id=order_id, status=OrderStatus.WORKING)

    @property
    def is_filled(self) -> bool:
        """Check if order is filled."""
        return self.status == OrderStatus.FILLED

    @property
    def has_fill(self) -> bool:
        """Whether the broker reported any economically realized quantity."""
        return self.filled_quantity > 0 and self.average_price > 0

    @property
    def is_terminal(self) -> bool:
        """Whether the order can no longer receive another fill."""
        return self.status in {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }

    @property
    def is_rejected(self) -> bool:
        """Check if order was rejected."""
        return self.status == OrderStatus.REJECTED

    @property
    def average_price(self) -> float:
        """Return the average fill price, or zero when no fill was reported."""
        return self.filled_price or 0.0


@dataclass(frozen=True)
class BrokerFill:
    """One exact broker execution normalized for canonical persistence.

    This application DTO is intentionally distinct from the infrastructure
    ``lib_strategy.types.BrokerFill`` wire shape.  Unlike order status, it has
    no defaults: a fill without complete identity, timestamp, quantity, price,
    fee amount, and fee currency cannot enter the canonical ledger.
    """

    trade_id: str
    order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    commission: Decimal
    commission_currency: str
    timestamp: datetime
    venue: str
    raw_response: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "trade_id",
            "order_id",
            "symbol",
            "commission_currency",
            "venue",
        ):
            normalized = str(getattr(self, field_name) or "").strip()
            if not normalized:
                msg = f"Broker fill requires {field_name}"
                raise ValueError(msg)
            if field_name == "commission_currency":
                normalized = normalized.upper()
            object.__setattr__(self, field_name, normalized)
        normalized_side = str(self.side or "").strip().lower()
        if normalized_side not in {"buy", "sell"}:
            msg = f"Broker fill has invalid side: {self.side!r}"
            raise ValueError(msg)
        object.__setattr__(self, "side", normalized_side)
        for field_name in ("quantity", "price", "commission"):
            try:
                parsed = Decimal(str(getattr(self, field_name)))
            except (InvalidOperation, TypeError, ValueError) as exc:
                msg = f"Broker fill has invalid {field_name}"
                raise ValueError(msg) from exc
            if not parsed.is_finite():
                msg = f"Broker fill has invalid {field_name}"
                raise ValueError(msg)
            if field_name in {"quantity", "price"} and parsed <= 0:
                msg = f"Broker fill requires positive {field_name}"
                raise ValueError(msg)
            object.__setattr__(self, field_name, parsed)
        if self.timestamp.tzinfo is None:
            msg = "Broker fill timestamp must include a timezone"
            raise ValueError(msg)
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(UTC))


@dataclass
class Position:
    """Current position in an instrument."""

    symbol: str
    side: str  # LONG or SHORT
    quantity: float
    entry_price: float
    current_price: float
    unrealized_pnl: float | None
    realized_pnl: float | None = None
    margin_used: float | None = None
    liquidation_price: float | None = None
    opened_at: datetime | None = None
    quantity_unit: PositionQuantityUnit = "asset"
    contract_multiplier: float | None = None
    leverage: float | None = None
    contract_type: str | None = None
    gross_notional: float | None = None
    notional_currency: str | None = None


class PositionValuationError(ValueError):
    """A broker position cannot be valued safely for exposure controls."""


def position_gross_notional(position: Mapping[str, Any]) -> Decimal:
    """Return absolute notional without guessing contract semantics.

    Contract positions must carry the broker adapter's explicit gross
    notional. Only positions explicitly identified as asset quantities may
    fall back to ``quantity * mark``.
    """

    quantity_raw = position.get("quantity")
    if quantity_raw in (None, ""):
        msg = "Position omitted quantity"
        raise PositionValuationError(msg)
    quantity = _finite_position_decimal(quantity_raw, field="quantity")
    if quantity == 0:
        explicit_zero_notional = position.get("gross_notional")
        if (
            explicit_zero_notional not in (None, "")
            and _finite_position_decimal(
                explicit_zero_notional,
                field="gross_notional",
            )
            != 0
        ):
            msg = "Zero-quantity position has a non-zero gross_notional"
            raise PositionValuationError(msg)
        return Decimal("0")

    quantity_unit = str(position.get("quantity_unit") or "").strip().lower()
    if quantity_unit not in {"asset", "contracts", "quote_notional"}:
        msg = "Non-zero position omitted a canonical quantity_unit"
        raise PositionValuationError(msg)

    explicit_notional = position.get("gross_notional")
    if explicit_notional not in (None, ""):
        notional = _finite_position_decimal(explicit_notional, field="gross_notional")
        if notional <= 0:
            msg = "Non-zero position has a non-positive gross_notional"
            raise PositionValuationError(msg)
        return notional

    if quantity_unit != "asset":
        msg = (
            "Non-asset position omitted gross_notional; contract exposure "
            "cannot be inferred from quantity and price"
        )
        raise PositionValuationError(msg)

    price_raw = position.get("current_price")
    if price_raw in (None, ""):
        price_raw = position.get("entry_price")
    if price_raw in (None, ""):
        msg = "Asset position omitted both current and entry price"
        raise PositionValuationError(msg)
    price = _finite_position_decimal(price_raw, field="position price")
    if price <= 0:
        msg = "Non-zero asset position has a non-positive price"
        raise PositionValuationError(msg)
    return abs(quantity) * price


def _finite_position_decimal(value: Any, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        msg = f"Position has an invalid {field}: {value!r}"
        raise PositionValuationError(msg) from exc
    if not parsed.is_finite():
        msg = f"Position has a non-finite {field}: {value!r}"
        raise PositionValuationError(msg)
    return parsed


@dataclass
class AccountInfo:
    """Account information from broker."""

    broker: BrokerType
    account_id: str
    equity: float
    available_balance: float
    margin_used: float = 0.0
    unrealized_pnl: float | None = 0.0
    realized_pnl: float | None = None
    positions: list[Position] = field(default_factory=list)
    currency: str = "USD"
    fetched_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    balances: list[AccountBalance] = field(default_factory=list)


class BrokerAdapter(ABC):
    """
    Abstract base class for broker adapters.

    All broker implementations must implement these methods.
    The execution engine uses this interface to interact with brokers.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        passphrase: str | None = None,
        sandbox: bool = False,
    ) -> None:
        """
        Initialize broker adapter.

        Args:
            api_key: API key for authentication
            api_secret: API secret for signing requests
            passphrase: Additional passphrase (some brokers require this)
            sandbox: Use sandbox/testnet environment
        """
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._sandbox = sandbox
        self._connected = False

    @property
    @abstractmethod
    def capabilities(self) -> BrokerCapabilities:
        """Return broker capabilities."""

    @abstractmethod
    async def connect(self) -> bool:
        """
        Connect to the broker.

        Returns:
            True if connection successful
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the broker."""

    @abstractmethod
    async def get_account_info(self) -> AccountInfo:
        """
        Get current account information.

        Returns:
            AccountInfo with balance and positions
        """

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """
        Get all open positions.

        Returns:
            List of current positions
        """

    @abstractmethod
    async def submit_order(self, intent: OrderIntent) -> BrokerOrderResult:
        """
        Submit an order to the broker.

        Args:
            intent: Order intent with all details

        Returns:
            BrokerOrderResult with order status
        """

    @abstractmethod
    async def submit_options_order(self, intent: OptionsIntent) -> BrokerOrderResult:
        """
        Submit an options order (potentially multi-leg).

        Args:
            intent: Options order intent with legs

        Returns:
            BrokerOrderResult with order status
        """

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an existing order.

        Args:
            order_id: Broker order ID

        Returns:
            True if cancellation successful
        """

    @abstractmethod
    async def get_order_status(self, order_id: str) -> BrokerOrderResult:
        """
        Get status of an order.

        Args:
            order_id: Broker order ID

        Returns:
            BrokerOrderResult with current status
        """

    async def get_fills(self, order_id: str) -> list[BrokerFill]:
        """Return exact broker executions generated by one order.

        Brokers that cannot satisfy the complete contract remain usable only
        outside live certification and fail explicitly if queried.
        """
        msg = f"{type(self).__name__} cannot provide exact fills for order {order_id}"
        raise NotImplementedError(msg)

    async def close_position(self, symbol: str) -> BrokerOrderResult:
        """
        Close an entire position.

        Default implementation fetches position and submits opposite order.

        Args:
            symbol: Symbol to close

        Returns:
            BrokerOrderResult for the closing order
        """
        positions = await self.get_positions()

        for pos in positions:
            if pos.symbol == symbol:
                # Create closing order
                intent = OrderIntent(
                    broker_code=self.capabilities.broker_type.value,
                    symbol=symbol,
                    side="SELL" if pos.side == "LONG" else "BUY",
                    quantity=pos.quantity,
                    order_type="market",
                    time_in_force="ioc",
                    metadata={"purpose": "close_position"},
                )
                return await self.submit_order(intent)

        return BrokerOrderResult.rejected(f"No position found for {symbol}")

    async def get_quote(self, symbol: str) -> dict[str, float] | None:  # noqa: ARG002
        """
        Get current quote for a symbol.

        Args:
            symbol: Symbol to quote

        Returns:
            Dict with bid, ask, last, volume (or None if not available)
        """
        # Default implementation returns None
        # Override in subclasses that support quotes
        return None

    def validate_order(self, intent: OrderIntent) -> str | None:
        """
        Validate an order before submission.

        Args:
            intent: Order intent to validate

        Returns:
            Error message if invalid, None if valid
        """
        caps = self.capabilities

        # Check quantity
        if intent.quantity <= 0:
            return "Quantity must be positive"

        if intent.quantity < caps.min_order_size:
            return f"Quantity below minimum: {caps.min_order_size}"

        if caps.max_order_size and intent.quantity > caps.max_order_size:
            return f"Quantity above maximum: {caps.max_order_size}"

        # Check symbol if restricted
        if caps.supported_symbols:  # noqa: SIM102
            if intent.symbol not in caps.supported_symbols:
                return f"Symbol not supported: {intent.symbol}"

        return None

    @property
    def is_connected(self) -> bool:
        """Check if connected to broker."""
        return self._connected

    @property
    def is_sandbox(self) -> bool:
        """Check if using sandbox environment."""
        return self._sandbox
