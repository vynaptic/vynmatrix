"""Coinbase Advanced Trade order denomination and precision rules."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from enum import StrEnum
from typing import NoReturn


def _invalid(message: str) -> NoReturn:
    raise ValueError(message)


def _positive_increment(value: str, *, field_name: str) -> str:
    try:
        increment = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        message = f"{field_name} must be a positive decimal increment"
        raise ValueError(message) from exc
    if not increment.is_finite() or increment <= 0:
        _invalid(f"{field_name} must be a positive decimal increment")
    return format(increment, "f")


def quantize_down_to_increment(value: float, increment: str) -> str:
    """Return Coinbase-compatible downward quantization as a decimal string."""

    if not math.isfinite(value) or value < 0.0:
        _invalid("value must be finite and non-negative")
    decimal_increment = Decimal(_positive_increment(increment, field_name="increment"))
    decimal_value = Decimal(str(value))
    quantized = (decimal_value / decimal_increment).to_integral_value(
        rounding=ROUND_DOWN
    ) * decimal_increment
    return format(quantized.quantize(decimal_increment), "f")


class MarketOrderSide(StrEnum):
    """Transaction side accepted by Coinbase market orders."""

    BUY = "buy"
    SELL = "sell"


class MarketOrderDenomination(StrEnum):
    """Size field required by a Coinbase market-order request."""

    BASE_SIZE = "base_size"
    QUOTE_SIZE = "quote_size"


@dataclass(frozen=True, slots=True)
class MarketOrderPrecision:
    """Coinbase product increments used for market-order translation."""

    base_increment: str
    quote_increment: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "base_increment",
            _positive_increment(self.base_increment, field_name="base_increment"),
        )
        object.__setattr__(
            self,
            "quote_increment",
            _positive_increment(self.quote_increment, field_name="quote_increment"),
        )


@dataclass(frozen=True, slots=True)
class BrokerMarketOrderSize:
    """Coinbase market-order size translated from a base-unit intent."""

    side: MarketOrderSide
    denomination: MarketOrderDenomination
    amount: str
    sizing_reference_price: float | None

    def __post_init__(self) -> None:
        amount = Decimal(self.amount)
        if not amount.is_finite() or amount < 0:
            _invalid("market-order amount must be finite and non-negative")

    def base_quantity_at_fill(self, fill_price: float) -> float:
        """Resolve filled base units under the registered full-fill assumption."""

        if not math.isfinite(fill_price) or fill_price <= 0.0:
            _invalid("fill_price must be finite and positive")
        amount = float(Decimal(self.amount))
        if self.denomination is MarketOrderDenomination.BASE_SIZE:
            return amount
        return amount / fill_price

    def quote_notional_at_fill(self, fill_price: float) -> float:
        """Resolve quote notional under the registered full-fill assumption."""

        if self.denomination is MarketOrderDenomination.QUOTE_SIZE:
            return float(Decimal(self.amount))
        return self.base_quantity_at_fill(fill_price) * fill_price

    def to_dict(self) -> dict[str, object]:
        return {
            "side": self.side.value,
            "denomination": self.denomination.value,
            "amount": self.amount,
            "sizing_reference_price": self.sizing_reference_price,
        }


def translate_coinbase_market_order_size(
    *,
    base_quantity: float,
    side: MarketOrderSide,
    precision: MarketOrderPrecision,
    reference_price: float | None,
) -> BrokerMarketOrderSize:
    """Apply Advanced Trade's asymmetric market-order denomination exactly."""

    if not math.isfinite(base_quantity) or base_quantity < 0.0:
        _invalid("base_quantity must be finite and non-negative")
    if not isinstance(side, MarketOrderSide):
        message = "side must be a MarketOrderSide"
        raise TypeError(message)
    if side is MarketOrderSide.BUY:
        if reference_price is None or not math.isfinite(reference_price) or reference_price <= 0:
            _invalid("reference_price must be finite and positive for a market buy")
        amount = quantize_down_to_increment(
            base_quantity * reference_price,
            precision.quote_increment,
        )
        denomination = MarketOrderDenomination.QUOTE_SIZE
    else:
        amount = quantize_down_to_increment(base_quantity, precision.base_increment)
        denomination = MarketOrderDenomination.BASE_SIZE
    return BrokerMarketOrderSize(
        side=side,
        denomination=denomination,
        amount=amount,
        sizing_reference_price=reference_price,
    )


__all__ = [
    "BrokerMarketOrderSize",
    "MarketOrderDenomination",
    "MarketOrderPrecision",
    "MarketOrderSide",
    "quantize_down_to_increment",
    "translate_coinbase_market_order_size",
]
