from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from lib_strategy.signals.signal import Signal

HISTORICAL_REPLAY_FILL_POLICY = "historical-next-bar-open-v1"
ORDINARY_REBALANCE_EXIT_METADATA_KEY = "portfolio_rebalance_ordinary_exit"
_CLOSE_QUANTITY_TOLERANCE = Decimal("0.00000001")
_TARGET_QUANTITY_TOLERANCE = Decimal("0.00000001")
_MIN_CURRENCY_CODE_LENGTH = 2
_MAX_CURRENCY_CODE_LENGTH = 10
_SHA256_HEX_LENGTH = 64


def resolve_historical_replay_fill_context(
    metadata: dict[str, Any] | None,
) -> tuple[datetime, dict[str, Any]] | None:
    """Validate the exact source-bar identity carried by a replay order."""
    values = dict(metadata or {})
    if values.get("historical_replay") is not True:
        return None
    source_price_id = values.get("source_price_id")
    source_revision = values.get("source_content_revision")
    source_price_ts = values.get("source_price_ts")
    trigger_policy = values.get("trigger_policy_version")
    if (
        not isinstance(source_price_id, int)
        or isinstance(source_price_id, bool)
        or source_price_id <= 0
        or not isinstance(source_revision, int)
        or isinstance(source_revision, bool)
        or source_revision <= 0
        or trigger_policy != HISTORICAL_REPLAY_FILL_POLICY
    ):
        msg = "Historical paper fill has incomplete source-bar provenance"
        raise ValueError(msg)
    try:
        timestamp = datetime.fromisoformat(str(source_price_ts))
    except ValueError as exc:
        msg = "Historical paper fill has an invalid source timestamp"
        raise ValueError(msg) from exc
    if timestamp.tzinfo is None:
        msg = "Historical paper fill source timestamp must be timezone-aware"
        raise ValueError(msg)
    provenance = {
        "source_price_id": source_price_id,
        "source_content_revision": source_revision,
        "trigger_policy_version": trigger_policy,
    }
    return timestamp.astimezone(UTC), provenance


def client_order_id_for(metadata: dict[str, Any]) -> str:
    """Derive the stable broker identity for an entry or protective leg."""
    explicit = metadata.get("client_order_id") or metadata.get("signal_id")
    if explicit:
        return str(explicit)
    parent = metadata.get("parent_signal_id")
    purpose = metadata.get("purpose")
    if parent and purpose:
        return f"{parent}:{purpose}"
    return ""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _optional_currency(value: str | None) -> str | None:
    if value is None:
        return None
    currency = str(value).strip().upper()
    if (
        not currency.isalnum()
        or not _MIN_CURRENCY_CODE_LENGTH <= len(currency) <= _MAX_CURRENCY_CODE_LENGTH
    ):
        msg = "Target position override settlement_currency is invalid"
        raise ValueError(msg)
    return currency


@dataclass(frozen=True)
class OrderCurrencyContext:
    """Observed conversion used to size and account for one order.

    ``account_to_settlement_rate`` is expressed as settlement-currency units
    per one account-currency unit.  The requested timestamp is retained
    separately from the source observation so historical replays remain
    point-in-time auditable.
    """

    account_currency: str
    settlement_currency: str
    account_to_settlement_rate: Decimal
    requested_at: datetime
    observed_at: datetime
    source: str

    def __post_init__(self) -> None:
        account_currency = str(self.account_currency or "").strip().upper()
        settlement_currency = str(self.settlement_currency or "").strip().upper()
        for field_name, currency in (
            ("account_currency", account_currency),
            ("settlement_currency", settlement_currency),
        ):
            if not currency or len(currency) > 10 or not currency.isalnum():  # noqa: PLR2004
                msg = f"Order currency context has invalid {field_name}"
                raise ValueError(msg)
        try:
            rate = Decimal(str(self.account_to_settlement_rate))
        except (InvalidOperation, TypeError, ValueError) as exc:
            msg = "Order currency context requires a finite positive FX rate"
            raise ValueError(msg) from exc
        if not rate.is_finite() or rate <= 0:
            msg = "Order currency context requires a finite positive FX rate"
            raise ValueError(msg)
        if account_currency == settlement_currency and rate != 1:
            msg = "Same-currency order context requires an identity FX rate"
            raise ValueError(msg)
        source = str(self.source or "").strip()
        if not source:
            msg = "Order currency context requires an observed FX source"
            raise ValueError(msg)
        requested_at = _utc(self.requested_at)
        observed_at = _utc(self.observed_at)
        if observed_at > requested_at:
            msg = "Order currency context cannot use a future FX observation"
            raise ValueError(msg)
        object.__setattr__(self, "account_currency", account_currency)
        object.__setattr__(self, "settlement_currency", settlement_currency)
        object.__setattr__(self, "account_to_settlement_rate", rate)
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "source", source)

    def account_to_settlement(self, amount: Decimal | float | int | str) -> Decimal:
        """Convert an account-currency amount into settlement currency."""
        return Decimal(str(amount)) * self.account_to_settlement_rate

    def settlement_to_account(self, amount: Decimal | float | int | str) -> Decimal:
        """Convert a settlement-currency amount into account currency."""
        return Decimal(str(amount)) / self.account_to_settlement_rate


@dataclass
class ExecutionRequest:
    """
    Request passed to the order builder with user/profile context.
    """

    user_id: str
    profile: dict[str, Any]  # cached user profile + broker preference
    user_strategy_config: dict[str, Any]
    signal: Signal
    score_context: dict[str, float] | None = None  # asset_score, sector_score, market_score
    currency_context: OrderCurrencyContext | None = None
    # Execution-internal, fenced portfolio reconciliation instructions. They
    # are never accepted from the public single-signal command contract.
    close_quantity_override: "CloseQuantityOverride | None" = None
    target_position_override: "TargetPositionQuantityOverride | None" = None


@dataclass(frozen=True)
class CloseQuantityOverride:
    """Strategy-attributed close quantity clamped to a broker observation."""

    account_plan_id: str
    plan_leg_id: str
    symbol: str
    quantity: Decimal
    strategy_quantity: Decimal
    broker_quantity: Decimal
    broker_observed_at: datetime

    def __post_init__(self) -> None:
        account_plan_id = str(self.account_plan_id or "").strip()
        plan_leg_id = str(self.plan_leg_id or "").strip()
        symbol = str(self.symbol or "").strip()
        self._validate_digest(account_plan_id, field_name="account_plan_id")
        self._validate_digest(plan_leg_id, field_name="plan_leg_id")
        if not symbol:
            msg = "Close quantity override requires a symbol"
            raise ValueError(msg)
        quantity = self._decimal(self.quantity, field_name="quantity")
        strategy_quantity = self._decimal(
            self.strategy_quantity,
            field_name="strategy_quantity",
        )
        broker_quantity = self._decimal(
            self.broker_quantity,
            field_name="broker_quantity",
        )
        if quantity <= 0:
            msg = "Close quantity override must be positive"
            raise ValueError(msg)
        if strategy_quantity == 0 or broker_quantity == 0:
            msg = "Close quantity override requires non-zero source exposure"
            raise ValueError(msg)
        if strategy_quantity.is_signed() != broker_quantity.is_signed():
            msg = "Strategy and broker close exposures must have the same direction"
            raise ValueError(msg)
        expected = min(abs(strategy_quantity), abs(broker_quantity))
        if abs(quantity - expected) > _CLOSE_QUANTITY_TOLERANCE:
            msg = "Close quantity override must equal the strategy/broker exposure clamp"
            raise ValueError(msg)
        observed_at = _utc(self.broker_observed_at)
        if observed_at > datetime.now(tz=UTC):
            msg = "Close quantity override cannot use a future broker observation"
            raise ValueError(msg)
        object.__setattr__(self, "account_plan_id", account_plan_id)
        object.__setattr__(self, "plan_leg_id", plan_leg_id)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "strategy_quantity", strategy_quantity)
        object.__setattr__(self, "broker_quantity", broker_quantity)
        object.__setattr__(self, "broker_observed_at", observed_at)

    @staticmethod
    def _decimal(value: Decimal, *, field_name: str) -> Decimal:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            msg = f"Close quantity override {field_name} must be finite"
            raise ValueError(msg) from exc
        if not result.is_finite():
            msg = f"Close quantity override {field_name} must be finite"
            raise ValueError(msg)
        return result

    @staticmethod
    def _validate_digest(value: str, *, field_name: str) -> None:
        if len(value) != _SHA256_HEX_LENGTH or any(
            character not in "0123456789abcdef" for character in value
        ):
            msg = f"Close quantity override {field_name} must be a lowercase SHA-256 digest"
            raise ValueError(msg)


@dataclass(frozen=True)
class TargetPositionQuantityOverride:
    """Strategy-relative target sized from one authoritative account snapshot."""

    account_plan_id: str
    plan_leg_id: str
    symbol: str
    target_allocation: Decimal
    target_quantity: Decimal
    strategy_quantity: Decimal
    broker_quantity: Decimal
    target_weight_drift_fraction: Decimal
    broker_observed_at: datetime
    reference_price: Decimal
    quote_observed_at: datetime
    quote_execution_authority: str = "shared"
    settlement_currency: str | None = None

    def __post_init__(self) -> None:
        account_plan_id = str(self.account_plan_id or "").strip()
        plan_leg_id = str(self.plan_leg_id or "").strip()
        symbol = str(self.symbol or "").strip()
        CloseQuantityOverride._validate_digest(
            account_plan_id,
            field_name="account_plan_id",
        )
        CloseQuantityOverride._validate_digest(plan_leg_id, field_name="plan_leg_id")
        if not symbol:
            msg = "Target position override requires a symbol"
            raise ValueError(msg)
        target_allocation = self._decimal(
            self.target_allocation,
            field_name="target_allocation",
        )
        target_quantity = self._decimal(self.target_quantity, field_name="target_quantity")
        strategy_quantity = self._decimal(
            self.strategy_quantity,
            field_name="strategy_quantity",
        )
        broker_quantity = self._decimal(self.broker_quantity, field_name="broker_quantity")
        drift = self._decimal(
            self.target_weight_drift_fraction,
            field_name="target_weight_drift_fraction",
        )
        reference_price = self._decimal(
            self.reference_price,
            field_name="reference_price",
        )
        if not 0 <= target_allocation <= 1:
            msg = "Target allocation must be in [0, 1]"
            raise ValueError(msg)
        if target_quantity < 0 or strategy_quantity < 0 or broker_quantity < 0:
            msg = "Long-only target position quantities must be non-negative"
            raise ValueError(msg)
        if target_allocation == 0 and target_quantity != 0:
            msg = "Zero target allocation requires zero target quantity"
            raise ValueError(msg)
        if not 0 <= drift < 1:
            msg = "Target weight drift fraction must be in [0, 1)"
            raise ValueError(msg)
        if reference_price <= 0:
            msg = "Target position override reference_price must be positive"
            raise ValueError(msg)
        if strategy_quantity - broker_quantity > _TARGET_QUANTITY_TOLERANCE:
            msg = "Strategy target exposure exceeds authoritative broker exposure"
            raise ValueError(msg)
        observed_at = _utc(self.broker_observed_at)
        quote_observed_at = _utc(self.quote_observed_at)
        quote_execution_authority = str(self.quote_execution_authority or "").strip().lower()
        if quote_execution_authority not in {"shared", "paper_only"}:
            msg = "Target position override quote authority must be shared or paper_only"
            raise ValueError(msg)
        if observed_at > datetime.now(tz=UTC):
            msg = "Target position override cannot use a future broker observation"
            raise ValueError(msg)
        if quote_observed_at > datetime.now(tz=UTC):
            msg = "Target position override cannot use a future quote observation"
            raise ValueError(msg)
        settlement_currency = _optional_currency(self.settlement_currency)
        object.__setattr__(self, "account_plan_id", account_plan_id)
        object.__setattr__(self, "plan_leg_id", plan_leg_id)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "target_allocation", target_allocation)
        object.__setattr__(self, "target_quantity", target_quantity)
        object.__setattr__(self, "strategy_quantity", strategy_quantity)
        object.__setattr__(self, "broker_quantity", broker_quantity)
        object.__setattr__(self, "target_weight_drift_fraction", drift)
        object.__setattr__(self, "broker_observed_at", observed_at)
        object.__setattr__(self, "reference_price", reference_price)
        object.__setattr__(self, "quote_observed_at", quote_observed_at)
        object.__setattr__(self, "quote_execution_authority", quote_execution_authority)
        object.__setattr__(self, "settlement_currency", settlement_currency)

    @property
    def delta_quantity(self) -> Decimal:
        """Return the signed strategy-relative change outside the no-trade band."""
        delta = self.target_quantity - self.strategy_quantity
        no_trade_quantity = self.target_quantity * self.target_weight_drift_fraction
        if abs(delta) <= max(_TARGET_QUANTITY_TOLERANCE, no_trade_quantity):
            return Decimal("0")
        return delta

    @staticmethod
    def _decimal(value: Decimal, *, field_name: str) -> Decimal:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            msg = f"Target position override {field_name} must be finite"
            raise ValueError(msg) from exc
        if not result.is_finite():
            msg = f"Target position override {field_name} must be finite"
            raise ValueError(msg)
        return result


@dataclass
class OrderIntent:
    """
    High-level intent produced by the order builder, prior to broker translation.

    Represents a single order or leg of a multi-leg order.
    """

    broker_code: str
    symbol: str
    side: str  # BUY/SELL
    quantity: float
    order_type: str = "market"
    time_in_force: str = "gtc"
    limit_price: float | None = None
    stop_price: float | None = None
    metadata: dict[str, Any] | None = None  # options legs, spread info, etc.
    currency_context: OrderCurrencyContext | None = None
    # Ephemeral execution-boundary attribution. These fields are never sent to
    # a broker or serialized into the canonical intent payload.
    _canonical_paper_order_id: int | None = field(default=None, init=False, repr=False)
    _canonical_paper_instrument_id: int | None = field(default=None, init=False, repr=False)


@dataclass
class OptionsLeg:
    """
    Single leg of an options order.
    """

    option_type: str  # CALL/PUT
    strike: float
    expiry: str  # YYYY-MM-DD
    side: str  # BUY/SELL
    quantity: int
    premium: float | None = None


@dataclass
class OptionsIntent(OrderIntent):
    """
    Options-specific order intent with leg information.
    """

    legs: list[OptionsLeg] | None = None
    spread_type: str | None = None  # bull_call, bear_put, etc.
    underlying_price: float | None = None
    max_profit: float | None = None
    max_loss: float | None = None
    breakeven: float | None = None
