"""Type definitions for trading strategies.

This module contains the option and broker wire shapes used by production
strategy and infrastructure boundaries.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, NotRequired, TypedDict

# Options-related types
OptionType = Literal["CALL", "PUT"]

SpreadType = Literal[
    "bull_call",
    "bear_put",
    "bull_put",
    "bear_call",
    "iron_condor",
    "straddle",
    "strangle",
]
OptionSide = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class OptionContractIdentity:
    """Shared identity and sizing fields for one concrete option contract."""

    underlying_symbol: str
    expiry: str
    strike: float
    option_type: str
    underlying_asset_class: str | None
    lot_size: int
    contract_multiplier: float
    underlying_price: float | None


class OptionLeg(TypedDict):
    """Single leg of an options spread."""

    option_type: OptionType
    strike: float
    expiry: str  # ISO format: YYYY-MM-DD
    side: OptionSide
    quantity: int
    premium: float


class OptionSpread(TypedDict, total=False):
    """General option spread structure (supports multi-leg strategies)."""

    spread_type: SpreadType
    underlying: str
    underlying_price: float
    legs: list[OptionLeg]
    leg1: OptionLeg
    leg2: OptionLeg
    leg3: OptionLeg
    leg4: OptionLeg
    net_debit: float  # Debit (positive) or credit (negative)
    max_profit: float | None
    max_loss: float
    breakeven: float
    probability_of_profit: float
    days_to_expiry: int
    notes: str
    contract_multiplier: float
    implied_volatility: float | None
    delta: float | None
    vega: float | None
    theta: float | None
    gamma: float | None


# =============================================================================
# BROKER EXECUTION TYPES
# =============================================================================


class BrokerExecutionMethod(Enum):
    """Execution method for broker order routing.

    Determines how a signal is executed at the broker level:
    - SPOT: Direct buy/sell of underlying asset
    - MARGIN: Leveraged spot trading
    - PERPETUAL: Perpetual futures contracts
    - FUTURES: Dated futures contracts
    - OPTIONS_SINGLE: Single option leg (call/put)
    - OPTIONS_STRATEGY: Multi-leg options strategy (spreads)
    """

    SPOT = "spot"
    MARGIN = "margin"
    PERPETUAL = "perpetual"
    FUTURES = "futures"
    OPTIONS_SINGLE = "options_single"
    OPTIONS_STRATEGY = "options_strategy"


class BrokerOrderIntent(TypedDict, total=False):
    """Broker-agnostic order request.

    This structure represents an order before it's translated to
    broker-specific format. All monetary values are strings to preserve
    decimal precision.

    Required fields:
        symbol: Trading symbol (e.g., "BTCUSD", "AAPL")
        side: "buy" or "sell"
        quantity: Order quantity as string (decimal precision)
        order_type: "market", "limit", "stop", "stop_limit"
        execution_method: How to execute (spot, perpetual, options, etc.)

    Optional fields for limit/stop orders:
        limit_price: Limit price for limit/stop_limit orders
        stop_price: Stop trigger price for stop/stop_limit orders
        time_in_force: Order duration (default: GTC)

    Optional tracking:
        client_order_id: Client-generated order ID for reconciliation

    Broker catalogue fields:
        symbol: Exact broker symbol when a catalogue mapping exists
        broker_instrument_id: Opaque venue identifier (for example Saxo UIC)
        broker_instrument_type: Venue discriminator (for example Saxo AssetType)
        lot_size: Exact catalogue quantity increment enforced before venue submission
        to_open_close: Option netting intent when the venue requires it

    Options-specific fields (for OPTIONS_SINGLE/OPTIONS_STRATEGY):
        option_type: "CALL" or "PUT"
        strike: Strike price as string
        expiry: Expiration date (ISO format: YYYY-MM-DD)
        legs: List of order intents for multi-leg strategies
    """

    # Required fields
    symbol: str
    side: str  # "buy" or "sell"
    quantity: str  # Decimal as string for precision
    order_type: str  # "market", "limit", "stop", "stop_limit"
    execution_method: str  # ExecutionMode value

    # Price fields (required for limit/stop orders)
    limit_price: NotRequired[str]
    stop_price: NotRequired[str]

    # Order management
    time_in_force: NotRequired[str]  # TimeInForce value, default "gtc"
    client_order_id: NotRequired[str]

    # Broker catalogue identity (requirements are adapter-specific)
    broker_instrument_id: NotRequired[str]
    broker_instrument_type: NotRequired[str]
    lot_size: NotRequired[str]
    to_open_close: NotRequired[Literal["to_open", "to_close"]]

    # Options-specific
    option_type: NotRequired[str]  # "CALL" or "PUT"
    strike: NotRequired[str]
    expiry: NotRequired[str]  # ISO format: YYYY-MM-DD
    exchange: NotRequired[str]
    legs: NotRequired[list["BrokerOrderIntent"]]  # For multi-leg orders


class BrokerOrderResult(TypedDict, total=False):
    """Result of broker order execution.

    Returned by broker adapters after order placement.
    All monetary values are strings to preserve decimal precision.

    Core fields:
        success: Whether the order was accepted
        status: Order status (new, filled, partially_filled, rejected, etc.)
        filled_quantity: Quantity filled so far

    Identification:
        broker_order_id: Broker's order identifier
        client_order_id: Client-provided order ID (if supplied)

    Execution details:
        filled_price: Average fill price (if any fills)
        commission: Commission charged
        commission_currency: Currency in which commission was charged
        timestamp: Execution timestamp (ISO format)

    Error handling:
        error_message: Error description if order failed
        error_code: Broker-specific error code

    Raw response:
        raw_response: Original broker response for debugging
    """

    # Core status
    success: bool
    status: str  # new, working, partially_filled, filled, canceled, rejected
    filled_quantity: str

    # Order identification
    broker_order_id: NotRequired[str]
    client_order_id: NotRequired[str]

    # Execution details
    filled_price: NotRequired[str]
    commission: NotRequired[str]
    commission_currency: NotRequired[str]
    timestamp: NotRequired[str]

    # Error information
    error_message: NotRequired[str]
    error_code: NotRequired[str]

    # For debugging
    raw_response: NotRequired[dict[str, Any]]


class BrokerFill(TypedDict):
    """One exact broker-reported execution crossing the infrastructure port.

    This wire contract is deliberately separate from the execution engine's
    domain fill DTO.  Adapter implementations must populate every economic and
    identity field from a broker fill/trade resource; cumulative order status
    is not a valid substitute.
    """

    trade_id: str
    broker_order_id: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: str
    price: str
    fee_amount: str
    fee_currency: str
    timestamp: str
    venue: str
    raw_response: NotRequired[dict[str, Any]]


class AccountBalance(TypedDict):
    """Account balance for a single currency.

    All monetary values are strings to preserve decimal precision.
    """

    currency: str  # Currency code (e.g., "USD", "EUR", "BTC")
    total: str  # Total balance
    available: str  # Available for trading
    locked: str  # Locked in open orders


class BrokerMoney(TypedDict):
    """One broker-reported monetary amount with an explicit currency."""

    value: str
    currency: str


PositionQuantityUnit = Literal["asset", "contracts", "quote_notional"]


class BrokerPosition(TypedDict):
    """Canonical position payload crossing the infrastructure port.

    ``quantity`` always remains the broker quantity required to close the
    position. Non-asset units therefore carry an adapter-computed absolute
    ``gross_notional`` rather than changing quantity into underlying units.
    """

    symbol: str
    side: Literal["long", "short"]
    quantity: str
    quantity_unit: PositionQuantityUnit
    notional_type: Literal[
        "spot",
        "linear",
        "vanilla",
        "broker_mark_value",
        "broker_notional",
    ]
    is_quanto: bool
    entry_price: NotRequired[str]
    current_price: NotRequired[str]
    mark_price: NotRequired[str]
    unrealized_pnl: NotRequired[str]
    realized_pnl: NotRequired[str]
    margin_used: NotRequired[str]
    liquidation_price: NotRequired[str]
    currency: NotRequired[str]
    asset_currency: NotRequired[str]
    quote_currency: NotRequired[str]
    settlement_currency: NotRequired[str]
    pnl_currency: NotRequired[str]
    notional_currency: NotRequired[str]
    gross_notional: NotRequired[str]
    contract_multiplier: NotRequired[str]
    contract_type: NotRequired[str]
    asset_class: NotRequired[str]
    exchange: NotRequired[str]
    product: NotRequired[str]


class BrokerAccountSnapshot(TypedDict):
    """Atomic broker account input used by execution risk checks.

    ``equity_source`` is deliberately explicit. ``broker`` means the monetary
    aggregates came from an authoritative broker account-summary field;
    ``spot_holdings`` permits reconstruction from cash plus marked spot assets;
    ``unavailable`` forces execution to fail closed.
    """

    balances: list[AccountBalance]
    positions: list[BrokerPosition]
    equity: list[BrokerMoney]
    available_cash: list[BrokerMoney]
    margin_used: list[BrokerMoney]
    equity_source: Literal["broker", "spot_holdings", "unavailable"]


class BrokerCapabilities(TypedDict, total=False):
    """Broker capabilities for capability detection.

    This infrastructure wire shape is intentionally distinct from the
    execution engine's application ``BrokerCapabilities`` dataclass.  The
    bridge converts execution-method strings into explicit routing booleans.
    """

    asset_classes: list[str]  # ["crypto", "equity", "futures", etc.]
    execution_methods: list[str]  # ExecutionMode values
    multi_leg_orders: NotRequired[bool]  # Supports multi-leg options orders
    paper_trading: NotRequired[dict[str, Any]]  # Paper trading config
    regions: NotRequired[list[str]]  # Supported regions
    max_order_size: NotRequired[str]  # Maximum order size
    min_order_size: NotRequired[str]  # Minimum order size
    supported_order_types: NotRequired[list[str]]  # OrderType values
    exact_fill_retrieval: NotRequired[bool]
