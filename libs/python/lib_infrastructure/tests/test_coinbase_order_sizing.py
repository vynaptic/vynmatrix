"""Coinbase order-denomination and precision boundary tests."""

from __future__ import annotations

import pytest

from lib_infrastructure.brokers.adapters.coinbase_order_sizing import (
    MarketOrderDenomination,
    MarketOrderPrecision,
    MarketOrderSide,
    quantize_down_to_increment,
    translate_coinbase_market_order_size,
)


def _precision(
    *,
    base_increment: str = "0.001",
    quote_increment: str = "0.01",
) -> MarketOrderPrecision:
    return MarketOrderPrecision(
        base_increment=base_increment,
        quote_increment=quote_increment,
    )


def test_market_buy_uses_quote_increment_only() -> None:
    translated = translate_coinbase_market_order_size(
        base_quantity=10.009 / 33.0,
        side=MarketOrderSide.BUY,
        precision=_precision(base_increment="1"),
        reference_price=33.0,
    )

    assert translated.denomination is MarketOrderDenomination.QUOTE_SIZE
    assert translated.amount == "10.00"
    assert translated.base_quantity_at_fill(33.0) == pytest.approx(10.0 / 33.0)
    assert translated.quote_notional_at_fill(40.0) == 10.0


def test_market_sell_uses_base_increment_only() -> None:
    translated = translate_coinbase_market_order_size(
        base_quantity=0.3039,
        side=MarketOrderSide.SELL,
        precision=_precision(quote_increment="100"),
        reference_price=33.337,
    )

    assert translated.denomination is MarketOrderDenomination.BASE_SIZE
    assert translated.amount == "0.303"
    assert translated.base_quantity_at_fill(30.0) == 0.303
    assert translated.quote_notional_at_fill(30.0) == pytest.approx(9.09)


@pytest.mark.parametrize("increment", ["0", "-0.1", "nan", "invalid"])
def test_quantization_rejects_invalid_product_increment(increment: str) -> None:
    with pytest.raises(ValueError, match="positive decimal increment"):
        quantize_down_to_increment(1.0, increment)


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf")])
def test_quantization_rejects_invalid_value(value: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        quantize_down_to_increment(value, "0.01")


def test_market_buy_requires_reference_price() -> None:
    with pytest.raises(ValueError, match="reference_price"):
        translate_coinbase_market_order_size(
            base_quantity=1.0,
            side=MarketOrderSide.BUY,
            precision=_precision(),
            reference_price=None,
        )
