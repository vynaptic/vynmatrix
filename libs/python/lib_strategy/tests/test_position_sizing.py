"""Pure position-sizing domain-kernel tests."""

from __future__ import annotations

import json

import pytest

from lib_strategy.position_sizing import (
    MissingStopBehavior,
    PositionSizingPolicy,
    PositionSizingRequest,
    PreBrokerQuantityRounding,
    SizingDecisionStatus,
)


def test_fixed_percent_applies_buying_power_and_maximum_caps() -> None:
    policy = PositionSizingPolicy.fixed_percent(
        policy_id="fixed",
        fixed_pct=0.10,
        max_position_pct=0.05,
        minimum_position_notional=1.0,
    )

    decision = policy.calculate(
        PositionSizingRequest(price=100.0, equity=100_000.0, buying_power=20_000.0)
    )

    assert decision.status is SizingDecisionStatus.ACCEPTED
    assert decision.requested_notional_value == 10_000.0
    assert decision.sizing_intent_notional_value == 5_000.0
    assert decision.quantity == 50.0
    assert decision.notional_value == 5_000.0
    assert decision.capped_by_buying_power is False
    assert decision.capped_by_max_position is True


def test_initial_stop_risk_is_fail_closed_without_a_stop() -> None:
    policy = PositionSizingPolicy.initial_stop_risk(
        policy_id="risk",
        risk_pct=0.01,
        fixed_pct_fallback=0.01,
        max_position_pct=0.05,
        minimum_position_notional=1.0,
    )

    decision = policy.calculate(
        PositionSizingRequest(price=100.0, equity=100_000.0, buying_power=100_000.0)
    )

    assert decision.status is SizingDecisionStatus.REJECTED
    assert decision.rejection_reason == "missing_stop_loss"
    assert decision.quantity == 0.0


def test_initial_stop_risk_can_use_registered_fixed_percent_fallback() -> None:
    policy = PositionSizingPolicy.initial_stop_risk(
        policy_id="production-fallback",
        risk_pct=0.01,
        fixed_pct_fallback=0.02,
        max_position_pct=0.05,
        minimum_position_notional=1.0,
        missing_stop_behavior=MissingStopBehavior.FALLBACK_FIXED_PCT,
    )

    decision = policy.calculate(
        PositionSizingRequest(price=100.0, equity=100_000.0, buying_power=100_000.0)
    )

    assert decision.accepted is True
    assert decision.fallback_reason == "missing_stop_loss"
    assert decision.quantity == 20.0
    assert decision.effective_sizing_risk_pct == 0.02


def test_risk_budget_reports_post_cap_effective_value() -> None:
    policy = PositionSizingPolicy.initial_stop_risk(
        policy_id="risk-cap",
        risk_pct=0.01,
        fixed_pct_fallback=0.01,
        max_position_pct=0.05,
        minimum_position_notional=1.0,
    )

    decision = policy.calculate(
        PositionSizingRequest(
            price=100.0,
            equity=100_000.0,
            buying_power=100_000.0,
            stop_loss=99.0,
        )
    )

    assert decision.capped_by_max_position is True
    assert decision.quantity == 50.0
    assert decision.effective_sizing_risk_amount == 50.0
    assert decision.effective_sizing_risk_pct == 0.0005


def test_minimum_is_checked_before_registered_pre_broker_rounding() -> None:
    policy = PositionSizingPolicy.fixed_percent(
        policy_id="minimum",
        fixed_pct=0.005,
        max_position_pct=0.05,
        minimum_position_notional=1.0,
        pre_broker_quantity_rounding=PreBrokerQuantityRounding.PRICE_TIERED_DECIMALS,
    )

    decision = policy.calculate(
        PositionSizingRequest(price=100.0, equity=100.0, buying_power=100.0)
    )

    assert decision.status is SizingDecisionStatus.REJECTED
    assert decision.rejection_reason == "below_minimum_notional"
    assert decision.sizing_intent_notional_value == 0.5
    assert decision.quantity == 0.0


@pytest.mark.parametrize(
    ("price", "expected_quantity"),
    [(61_000.0, 0.01639344), (50.0, 20.0), (500.0, 2.0)],
)
def test_price_tiered_pre_broker_rounding_is_deterministic(
    price: float,
    expected_quantity: float,
) -> None:
    policy = PositionSizingPolicy.fixed_percent(
        policy_id="price-tiered-rounding",
        fixed_pct=0.01,
        max_position_pct=0.05,
        minimum_position_notional=1.0,
        pre_broker_quantity_rounding=PreBrokerQuantityRounding.PRICE_TIERED_DECIMALS,
    )

    decision = policy.calculate(
        PositionSizingRequest(price=price, equity=100_000.0, buying_power=100_000.0)
    )

    assert decision.broker_input_quantity == expected_quantity
    assert decision.notional_value == expected_quantity * price


def test_policy_and_decision_payloads_are_strict_json_safe() -> None:
    policy = PositionSizingPolicy.fixed_percent(
        policy_id="json-safe",
        fixed_pct=0.01,
        max_position_pct=0.05,
        minimum_position_notional=1.0,
    )
    decision = policy.calculate(
        PositionSizingRequest(price=61_000.0, equity=100_000.0, buying_power=100_000.0)
    )

    encoded = json.dumps(
        {"policy": policy.to_dict(), "decision": decision.to_dict()},
        sort_keys=True,
        allow_nan=False,
    )

    assert "NaN" not in encoded
    assert '"pre_broker_quantity_rounding": "none"' in encoded
    assert "precision" not in encoded


@pytest.mark.parametrize(
    ("field", "value"),
    [("price", 0.0), ("equity", float("nan")), ("buying_power", -1.0)],
)
def test_sizing_request_rejects_invalid_account_inputs(field: str, value: float) -> None:
    values = {"price": 100.0, "equity": 100_000.0, "buying_power": 100_000.0}
    values[field] = value

    with pytest.raises(ValueError, match=field):
        PositionSizingRequest(
            price=values["price"],
            equity=values["equity"],
            buying_power=values["buying_power"],
        )
