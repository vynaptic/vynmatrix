"""Reference-fill parity between validation and runtime paper execution."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from dev_cli.validation.backtest.execution import (
    ExecutionCostModel,
    adverse_execution_price,
    execution_fill_costs,
)
from execution_engine.brokers.paper import PaperBroker
from execution_engine.models import OrderCurrencyContext, OrderIntent

_OBSERVED_AT = datetime(2026, 7, 25, 12, tzinfo=UTC)


def test_single_reference_fill_has_identical_price_and_commission() -> None:
    cost_model = ExecutionCostModel(
        scenario_name="paper-parity",
        commission_bps=10,
        slippage_bps=5,
        half_spread_bps=0,
        impact_bps=0,
        historical_basis="backward-applied-scenario",
    )
    rates = cost_model.to_paper_rates()
    reference_price = 100.0
    quantity = 2.0
    expected_price = adverse_execution_price(
        reference_price,
        transaction_side=1,
        cost_model=cost_model,
    )
    expected_costs = execution_fill_costs(
        reference_price=reference_price,
        execution_price=expected_price,
        quantity=quantity,
        cost_model=cost_model,
    )

    broker = PaperBroker(
        account_id="reference-fill-parity",
        starting_balance=Decimal("100000"),
        available_cash=Decimal("100000"),
        currency="USD",
        commission_pct=float(rates.commission_fraction),
        slippage_pct=float(rates.slippage_fraction),
    )
    broker.set_price("BTC-USD", reference_price)
    result = asyncio.run(
        broker.submit_order(
            OrderIntent(
                broker_code="paper",
                symbol="BTC-USD",
                side="BUY",
                quantity=quantity,
                order_type="market",
                metadata={"signal_id": "reference-fill-parity"},
                currency_context=OrderCurrencyContext(
                    account_currency="USD",
                    settlement_currency="USD",
                    account_to_settlement_rate=Decimal("1"),
                    requested_at=_OBSERVED_AT,
                    observed_at=_OBSERVED_AT,
                    source="identity",
                ),
            )
        )
    )

    assert result.success is True
    assert result.filled_price == pytest.approx(expected_price)
    assert float(result.commission) == pytest.approx(expected_costs.commission)
    assert result.commission_currency == "USD"


def test_validation_rejects_cost_dimensions_paper_cannot_represent() -> None:
    model = ExecutionCostModel(half_spread_bps=1)

    with pytest.raises(ValueError, match="cannot represent"):
        model.to_paper_rates()
