"""Characterization and shared-kernel parity tests for production sizing."""

from __future__ import annotations

import pytest

from execution_engine.config import AccountState, BrokerType, SizingConfig, SizingMethod
from execution_engine.position_sizer import PositionSizer
from lib_infrastructure.brokers.adapters.coinbase_order_sizing import (
    MarketOrderPrecision,
    MarketOrderSide,
    translate_coinbase_market_order_size,
)
from lib_strategy.position_sizing import (
    MissingStopBehavior,
    PositionSizingPolicy,
    PositionSizingRequest,
    PreBrokerQuantityRounding,
)


def _account(*, equity: float = 100_000.0, buying_power: float = 100_000.0) -> AccountState:
    return AccountState(
        broker=BrokerType.PAPER,
        equity=equity,
        available_cash=buying_power,
    )


def test_fixed_percent_matches_shared_kernel_for_current_binding_semantics() -> None:
    config = SizingConfig(
        method=SizingMethod.FIXED_PCT,
        fixed_pct=0.01,
        max_position_pct=0.05,
        min_position_size=1.0,
    )
    account = _account()
    price = 61_000.0
    shared = PositionSizingPolicy.fixed_percent(
        policy_id="parity",
        fixed_pct=0.01,
        max_position_pct=0.05,
        minimum_position_notional=1.0,
        pre_broker_quantity_rounding=PreBrokerQuantityRounding.PRICE_TIERED_DECIMALS,
    ).calculate(
        PositionSizingRequest(
            price=price,
            equity=account.equity,
            buying_power=account.buying_power,
        )
    )

    actual = PositionSizer(config).calculate(price=price, account=account)

    assert actual.quantity == shared.quantity
    assert actual.notional_value == shared.quantity * price
    assert actual.risk_amount == shared.effective_sizing_risk_amount
    assert actual.risk_pct == shared.effective_sizing_risk_pct
    assert actual.risk_amount == pytest.approx(999.99984)
    assert actual.risk_pct == pytest.approx(0.0099999984)
    assert actual.method_used is SizingMethod.FIXED_PCT


def test_position_sizer_output_translates_at_coinbase_adapter_boundary() -> None:
    config = SizingConfig(
        method=SizingMethod.FIXED_PCT,
        fixed_pct=0.01,
        max_position_pct=0.05,
        min_position_size=1.0,
    )
    price = 61_000.0
    precision = MarketOrderPrecision(
        base_increment="0.00000001",
        quote_increment="0.01",
    )
    production = PositionSizer(config).calculate(price=price, account=_account())
    production_order = translate_coinbase_market_order_size(
        base_quantity=production.quantity,
        side=MarketOrderSide.BUY,
        precision=precision,
        reference_price=price,
    )
    # Existing production semantics first round the sub-unit BTC quantity to
    # eight decimals, then Coinbase converts that quantity to quote size.
    assert production_order.amount == "999.99"


@pytest.mark.parametrize(
    ("price", "equity", "expected_quantity"),
    [
        (61_000.0, 100_000.0, 0.01639344),
        (50.0, 100_000.0, 20.0),
        (500.0, 100_000.0, 2.0),
    ],
)
def test_price_tiered_rounding_preserves_execution_quantity_behavior(
    price: float,
    equity: float,
    expected_quantity: float,
) -> None:
    config = SizingConfig(
        method=SizingMethod.FIXED_PCT,
        fixed_pct=0.01,
        max_position_pct=0.05,
        min_position_size=1.0,
    )

    result = PositionSizer(config).calculate(
        price=price,
        account=_account(equity=equity, buying_power=equity),
    )

    assert result.quantity == expected_quantity
    assert result.notional_value == expected_quantity * price


def test_risk_percent_reports_post_cap_effective_risk() -> None:
    config = SizingConfig(
        method=SizingMethod.RISK_PCT,
        fixed_pct=0.01,
        risk_pct=0.01,
        max_position_pct=0.05,
        min_position_size=1.0,
    )
    account = _account()
    price = 100.0
    stop = 99.0
    shared = PositionSizingPolicy.initial_stop_risk(
        policy_id="parity",
        risk_pct=0.01,
        fixed_pct_fallback=0.01,
        max_position_pct=0.05,
        minimum_position_notional=1.0,
        missing_stop_behavior=MissingStopBehavior.FALLBACK_FIXED_PCT,
    ).calculate(
        PositionSizingRequest(
            price=price,
            equity=account.equity,
            buying_power=account.buying_power,
            stop_loss=stop,
        )
    )

    actual = PositionSizer(config).calculate(
        price=price,
        account=account,
        stop_loss=stop,
    )

    assert shared.capped_by_max_position is True
    assert shared.quantity == 50.0
    assert shared.effective_sizing_risk_amount == 50.0
    assert actual.risk_amount == shared.effective_sizing_risk_amount == 50.0
    assert actual.risk_pct == shared.effective_sizing_risk_pct == 0.0005
    assert actual.quantity == shared.quantity
    assert actual.notional_value == shared.notional_value
    assert actual.notes == "Risk per unit: 1.00 settlement units | Capped at 5% of portfolio"


@pytest.mark.parametrize(
    ("stop_loss", "expected_note"),
    [
        (None, ""),
        (100.0, ""),
    ],
)
def test_risk_percent_missing_stop_fallback_is_unchanged(
    stop_loss: float | None,
    expected_note: str,
) -> None:
    config = SizingConfig(
        method=SizingMethod.RISK_PCT,
        fixed_pct=0.01,
        risk_pct=0.01,
        max_position_pct=0.05,
        min_position_size=1.0,
    )

    actual = PositionSizer(config).calculate(
        price=100.0,
        account=_account(),
        stop_loss=stop_loss,
    )

    assert actual.method_used is SizingMethod.FIXED_PCT
    assert actual.quantity == 10.0
    assert actual.notional_value == 1_000.0
    assert actual.risk_amount == 1_000.0
    assert actual.risk_pct == 0.01
    assert actual.notes == expected_note


def test_shared_kernel_preserves_buying_power_then_maximum_limit_ordering() -> None:
    config = SizingConfig(
        method=SizingMethod.RISK_PCT,
        risk_pct=0.01,
        max_position_pct=0.05,
        min_position_size=1.0,
    )

    result = PositionSizer(config).calculate(
        price=100.0,
        account=_account(buying_power=2_000.0),
        stop_loss=90.0,
    )

    assert result.quantity == 20.0
    assert result.notional_value == 2_000.0
    assert result.risk_amount == 200.0
    assert result.risk_pct == 0.002
    assert "Capped" not in result.notes


def test_shared_kernel_preserves_pre_rounding_minimum_rejection() -> None:
    config = SizingConfig(
        method=SizingMethod.FIXED_PCT,
        fixed_pct=0.005,
        max_position_pct=0.05,
        min_position_size=1.0,
    )

    result = PositionSizer(config).calculate(
        price=100.0,
        account=_account(equity=100.0, buying_power=100.0),
    )

    assert result.quantity == 0.0
    assert result.notional_value == 0.0
    assert result.risk_amount == 0.0
    assert result.risk_pct == 0.0
    assert result.notes == " | Below minimum notional (1.0)"


def test_risk_budget_reports_post_rounding_exposure_without_a_cap() -> None:
    config = SizingConfig(
        method=SizingMethod.RISK_PCT,
        risk_pct=0.01,
        max_position_pct=1.0,
        min_position_size=1.0,
    )

    result = PositionSizer(config).calculate(
        price=123.45,
        account=_account(),
        stop_loss=120.12,
    )

    assert result.risk_amount == pytest.approx(999.999)
    assert result.risk_pct == pytest.approx(0.00999999)


def test_fixed_amount_reports_post_cap_post_rounding_risk() -> None:
    config = SizingConfig(
        method=SizingMethod.FIXED_AMOUNT,
        fixed_amount=10_000.0,
        max_position_pct=0.05,
        min_position_size=1.0,
    )

    result = PositionSizer(config).calculate(
        price=333.0,
        account=_account(),
    )

    assert result.quantity == 15.02
    assert result.notional_value == pytest.approx(5_001.66)
    assert result.risk_amount == result.notional_value
    assert result.risk_pct == pytest.approx(0.0500166)


@pytest.mark.parametrize(
    ("price", "account", "stop_loss", "message"),
    [
        (0.0, _account(), None, "price"),
        (100.0, _account(equity=0.0), None, "equity"),
        (100.0, _account(buying_power=-1.0), None, "buying_power"),
        (100.0, _account(), float("nan"), "stop_loss"),
    ],
)
def test_shared_production_kernel_intentionally_fails_closed_on_invalid_inputs(
    price: float,
    account: AccountState,
    stop_loss: float | None,
    message: str,
) -> None:
    """Invalid account inputs fail closed instead of sizing an order."""

    with pytest.raises(ValueError, match=message):
        PositionSizer(SizingConfig(method=SizingMethod.RISK_PCT)).calculate(
            price=price,
            account=account,
            stop_loss=stop_loss,
        )
