"""
Futures order builder.

Constructs futures orders for:
- Perpetual contracts (crypto, some commodities)
- Dated futures contracts (equity index, commodities)

Handles leverage calculation, margin requirements, and position sizing.
"""

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from lib_common.logging import get_logger

from .config import FuturesConfig
from .models import OrderIntent

logger = get_logger(__name__)


@dataclass
class FuturesContract:
    """Represents a futures contract."""

    symbol: str
    underlying: str
    contract_type: str  # "perpetual" or "dated"
    expiry: datetime | None  # None for perpetual
    contract_size: float  # Value per point
    tick_size: float
    margin_requirement: float  # Initial margin percentage
    maintenance_margin: float  # Maintenance margin percentage
    funding_rate: float | None  # For perpetuals

    def __post_init__(self) -> None:
        """Reject a contract whose linear value cannot be calculated exactly."""
        if not self.symbol.strip() or not self.underlying.strip():
            msg = "Futures contract requires symbol and underlying"
            raise ValueError(msg)
        if self.contract_type not in {"perpetual", "dated"}:
            msg = f"Unsupported futures contract_type: {self.contract_type!r}"
            raise ValueError(msg)
        for field, value in (
            ("contract_size", self.contract_size),
            ("tick_size", self.tick_size),
            ("margin_requirement", self.margin_requirement),
            ("maintenance_margin", self.maintenance_margin),
        ):
            if not math.isfinite(value) or value <= 0:
                msg = f"Futures {field} must be finite and positive"
                raise ValueError(msg)
        if self.margin_requirement > 1 or self.maintenance_margin > self.margin_requirement:
            msg = "Futures margins require 0 < maintenance <= initial <= 1"
            raise ValueError(msg)
        if self.funding_rate is not None and not math.isfinite(self.funding_rate):
            msg = "Futures funding_rate must be finite when observed"
            raise ValueError(msg)
        if self.contract_type == "perpetual" and self.expiry is not None:
            msg = "Perpetual futures contract cannot have an expiry"
            raise ValueError(msg)
        if self.contract_type == "dated" and self.expiry is None:
            msg = "Dated futures contract requires an expiry"
            raise ValueError(msg)
        if self.expiry is not None and (
            self.expiry.tzinfo is None or self.expiry.utcoffset() is None
        ):
            msg = "Futures contract expiry must include an explicit timezone"
            raise ValueError(msg)


@dataclass
class FuturesOrderResult:
    """Result of futures order construction."""

    symbol: str
    side: str  # LONG or SHORT
    quantity: float  # Number of contracts
    contract_multiplier: float  # Settlement value per price point per contract
    reference_price: float
    leverage: float
    notional_value: float
    margin_required: float
    liquidation_price: float | None
    contract_type: str
    expiry: str | None
    notes: str = ""

    def __post_init__(self) -> None:
        """Keep the linear-contract economics complete at every boundary."""
        if not self.symbol.strip():
            msg = "Futures order requires a symbol"
            raise ValueError(msg)
        if self.side not in {"LONG", "SHORT"}:
            msg = f"Unsupported futures order side: {self.side!r}"
            raise ValueError(msg)
        for field, value in (
            ("quantity", self.quantity),
            ("contract_multiplier", self.contract_multiplier),
            ("reference_price", self.reference_price),
            ("leverage", self.leverage),
            ("notional_value", self.notional_value),
            ("margin_required", self.margin_required),
        ):
            if not math.isfinite(value) or value <= 0:
                msg = f"Futures order {field} must be finite and positive"
                raise ValueError(msg)
        if self.contract_type not in {"perpetual", "dated"}:
            msg = f"Unsupported futures contract_type: {self.contract_type!r}"
            raise ValueError(msg)
        if self.contract_type == "perpetual" and self.expiry is not None:
            msg = "Perpetual futures order cannot have an expiry"
            raise ValueError(msg)
        if self.contract_type == "dated":
            try:
                date.fromisoformat(str(self.expiry))
            except ValueError as exc:
                msg = "Dated futures order requires an ISO expiry date"
                raise ValueError(msg) from exc
        expected_notional = self.quantity * self.reference_price * self.contract_multiplier
        if not math.isclose(
            self.notional_value,
            expected_notional,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            msg = "Futures order notional does not match its contract economics"
            raise ValueError(msg)
        expected_margin = self.notional_value / self.leverage
        if not math.isclose(
            self.margin_required,
            expected_margin,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            msg = "Futures order margin does not match its leverage"
            raise ValueError(msg)


class FuturesOrderBuilder:
    """
    Builds futures orders with proper margin and leverage calculations.

    Supports:
    - Perpetual futures (crypto exchanges)
    - Dated futures (traditional exchanges)
    - Cross and isolated margin modes
    """

    def __init__(self, config: FuturesConfig) -> None:
        """
        Initialize futures builder.

        Args:
            config: Futures configuration from user preferences
        """
        self.config = config

    def build_order(  # noqa: PLR0912
        self,
        symbol: str,
        is_long: bool,
        price: float,
        notional_size: float,
        available_margin: float,
        contract_info: FuturesContract | None = None,
    ) -> FuturesOrderResult:
        """
        Build a futures order.

        Args:
            symbol: Trading symbol (e.g., "BTC-PERP", "ES")
            is_long: True for long, False for short
            price: Current price
            notional_size: Desired notional exposure
            available_margin: Available margin in account
            contract_info: Optional contract specifications

        Returns:
            FuturesOrderResult with order details
        """
        for field, value in (
            ("price", price),
            ("notional_size", notional_size),
            ("available_margin", available_margin),
        ):
            if not math.isfinite(value) or value <= 0:
                msg = f"Futures {field} must be finite and positive"
                raise ValueError(msg)
        for field, value in (
            ("target_leverage", self.config.target_leverage),
            ("max_leverage", self.config.max_leverage),
        ):
            if not math.isfinite(value) or value <= 0:
                msg = f"Futures {field} must be finite and positive"
                raise ValueError(msg)

        # Determine the exact linear contract terms.
        if contract_info:
            contract_type = contract_info.contract_type
            contract_size = contract_info.contract_size
            margin_req = contract_info.margin_requirement
        else:
            contract_type = "perpetual" if self.config.use_perpetual else "dated"
            contract_size = self.config.contract_size
            margin_req = 1 / self.config.max_leverage  # e.g., 10x = 10% margin
        if contract_type not in {"perpetual", "dated"}:
            msg = f"Unsupported futures contract_type: {contract_type!r}"
            raise ValueError(msg)
        if not math.isfinite(contract_size) or contract_size <= 0:
            msg = "Futures contract multiplier must be finite and positive"
            raise ValueError(msg)
        if not math.isfinite(margin_req) or margin_req <= 0 or margin_req > 1:
            msg = "Futures initial margin requirement must be in (0, 1]"
            raise ValueError(msg)

        # Contract margin limits are authoritative over a looser user setting.
        desired_leverage = min(
            self.config.target_leverage,
            self.config.max_leverage,
            1 / margin_req,
        )

        # Calculate number of contracts
        contracts = notional_size / (price * contract_size)

        # Round contracts
        contracts = self._round_contracts(contracts, contract_type)
        if contracts <= 0:
            msg = "Futures risk budget is below the minimum contract quantity"
            raise ValueError(msg)

        # Actual notional after rounding
        actual_notional = contracts * price * contract_size

        # Calculate margin required
        margin_required = actual_notional / desired_leverage

        # Check if we have enough margin
        if margin_required > available_margin:
            # Reduce position size
            max_notional = available_margin * desired_leverage
            contracts = self._round_contracts(max_notional / (price * contract_size), contract_type)
            if contracts <= 0:
                msg = "Available margin is below the minimum futures contract requirement"
                raise ValueError(msg)
            actual_notional = contracts * price * contract_size
            margin_required = actual_notional / desired_leverage
            if margin_required > available_margin and not math.isclose(
                margin_required,
                available_margin,
                rel_tol=1e-9,
                abs_tol=1e-9,
            ):
                msg = "Available margin is below the minimum futures contract requirement"
                raise ValueError(msg)
            logger.warning(
                "Reduced position size due to margin: %.2f contracts @ %.2fx leverage",
                contracts,
                desired_leverage,
            )

        # Determine expiry
        expiry = None
        if contract_type == "dated":
            if contract_info is not None and contract_info.expiry is not None:
                expiry = contract_info.expiry.astimezone(UTC).date().isoformat()
            else:
                # Configuration-only routes use the deterministic front-month
                # selection until a venue contract catalogue is supplied.
                expiry = self._get_front_month_expiry()

        return FuturesOrderResult(
            symbol=symbol,
            side="LONG" if is_long else "SHORT",
            quantity=contracts,
            contract_multiplier=contract_size,
            reference_price=price,
            leverage=desired_leverage,
            notional_value=actual_notional,
            margin_required=margin_required,
            # Funding and liquidation are exchange-specific and cannot be
            # inferred from the generic linear contract terms.
            liquidation_price=None,
            contract_type=contract_type,
            expiry=expiry,
            notes=f"Leverage: {desired_leverage:.1f}x, Margin: {margin_required:.2f}",
        )

    def _round_contracts(self, contracts: float, contract_type: str) -> float:
        """Round contract quantity to valid increment."""
        if contract_type == "perpetual":
            # Round down so risk limits are never exceeded by quantity rounding.
            return math.floor(contracts * 10_000) / 10_000
        # Dated futures require whole contracts; also round down.
        return float(math.floor(contracts))

    def _get_front_month_expiry(self) -> str:
        """Get the front month expiry date."""
        today = datetime.now(tz=UTC)

        # Find third Friday of current or next month
        year = today.year
        month = today.month

        # If we're past the third Friday, use next month
        third_friday = self._third_friday(year, month)
        if today > third_friday - timedelta(days=5):  # Roll 5 days before expiry
            month += 1
            if month > 12:  # noqa: PLR2004
                month = 1
                year += 1
            third_friday = self._third_friday(year, month)

        return third_friday.strftime("%Y-%m-%d")

    def _third_friday(self, year: int, month: int) -> datetime:
        """Calculate the third Friday of a month."""
        # Start from the first day of the month
        first_day = datetime(year, month, 1, tzinfo=UTC)

        # Find the first Friday
        days_until_friday = (4 - first_day.weekday()) % 7
        first_friday = first_day + timedelta(days=days_until_friday)

        # Third Friday is 2 weeks after first
        third_friday = first_friday + timedelta(weeks=2)

        return third_friday  # noqa: RET504


def to_futures_intent(
    futures_result: FuturesOrderResult,
    broker_code: str,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> list[OrderIntent]:
    """
    Convert FuturesOrderResult to OrderIntent(s).

    May return multiple intents if TP/SL are specified.

    Args:
        futures_result: Result from FuturesOrderBuilder
        broker_code: Broker identifier
        stop_loss: Optional stop loss price
        take_profit: Optional take profit price

    Returns:
        List of OrderIntents (main order + optional TP/SL)
    """
    if not str(broker_code).strip():
        msg = "Futures order intent requires a broker code"
        raise ValueError(msg)
    _validate_protective_prices(
        futures_result,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )
    intents = []
    execution_mode = "perpetual" if futures_result.contract_type == "perpetual" else "futures"
    contract_metadata = {
        "execution_mode": execution_mode,
        "contract_value_model": "linear",
        "contract_multiplier": futures_result.contract_multiplier,
        "reference_price": futures_result.reference_price,
        "leverage": futures_result.leverage,
        "notional_value": futures_result.notional_value,
        "margin_required": futures_result.margin_required,
        "liquidation_price": futures_result.liquidation_price,
        "contract_type": futures_result.contract_type,
        "expiry": futures_result.expiry,
    }

    # Main order
    main_intent = OrderIntent(
        broker_code=broker_code,
        symbol=futures_result.symbol,
        side="BUY" if futures_result.side == "LONG" else "SELL",
        quantity=futures_result.quantity,
        order_type="market",
        time_in_force="gtc",
        metadata={**contract_metadata, "purpose": "entry"},
    )
    intents.append(main_intent)

    # Stop loss order
    if stop_loss is not None:
        sl_side = "SELL" if futures_result.side == "LONG" else "BUY"
        sl_intent = OrderIntent(
            broker_code=broker_code,
            symbol=futures_result.symbol,
            side=sl_side,
            quantity=futures_result.quantity,
            order_type="stop",
            time_in_force="gtc",
            stop_price=stop_loss,
            metadata={
                **contract_metadata,
                "purpose": "stop_loss",
                "reduce_only": True,
            },
        )
        intents.append(sl_intent)

    # Take profit order
    if take_profit is not None:
        tp_side = "SELL" if futures_result.side == "LONG" else "BUY"
        tp_intent = OrderIntent(
            broker_code=broker_code,
            symbol=futures_result.symbol,
            side=tp_side,
            quantity=futures_result.quantity,
            order_type="limit",
            time_in_force="gtc",
            limit_price=take_profit,
            metadata={
                **contract_metadata,
                "purpose": "take_profit",
                "reduce_only": True,
            },
        )
        intents.append(tp_intent)

    return intents


def _validate_protective_prices(
    futures_result: FuturesOrderResult,
    *,
    stop_loss: float | None,
    take_profit: float | None,
) -> None:
    """Reject protective orders that increase rather than bound entry risk."""
    for field, value in (("stop_loss", stop_loss), ("take_profit", take_profit)):
        if value is not None and (not math.isfinite(value) or value <= 0):
            msg = f"Futures {field} must be finite and positive"
            raise ValueError(msg)
    if stop_loss is not None:
        invalid_stop = (
            futures_result.side == "LONG" and stop_loss >= futures_result.reference_price
        ) or (futures_result.side == "SHORT" and stop_loss <= futures_result.reference_price)
        if invalid_stop:
            msg = "Futures stop_loss is on the wrong side of the entry reference"
            raise ValueError(msg)
    if take_profit is not None:
        invalid_target = (
            futures_result.side == "LONG" and take_profit <= futures_result.reference_price
        ) or (futures_result.side == "SHORT" and take_profit >= futures_result.reference_price)
        if invalid_target:
            msg = "Futures take_profit is on the wrong side of the entry reference"
            raise ValueError(msg)
