from __future__ import annotations

from decimal import Decimal

from execution_engine.brokers.base import BrokerOrderResult, OrderStatus
from execution_engine.config import BrokerType, ExecutionMode
from execution_engine.execution_result import (
    ExecutionResult,
    aggregate_order_results,
    blocked_result,
    execution_log_status,
)


def _result(
    *,
    success: bool,
    orders_submitted: int,
    reason: str | None = None,
    execution_mode: str = "spot",
) -> ExecutionResult:
    return ExecutionResult(
        success=success,
        signal_id="sig",
        symbol="BTCUSD",
        execution_mode=execution_mode,
        broker="paper",
        orders_submitted=orders_submitted,
        orders_filled=orders_submitted,
        total_quantity=float(orders_submitted),
        average_price=100.0 if orders_submitted else 0.0,
        total_commission=0.0,
        reason=reason,
    )


def test_blocked_result_uses_standard_zero_order_shape() -> None:
    result = blocked_result(
        signal_id="sig-blocked",
        symbol="BTCUSD",
        execution_mode="spot",
        broker="paper",
        error_message="risk blocked",
    )

    assert result.success is False
    assert result.orders_submitted == 0
    assert result.orders_filled == 0
    assert result.average_price == 0.0
    assert result.error_message == "risk blocked"
    assert result.to_dict()["executed_at"].endswith("+00:00")


def test_aggregate_order_results_uses_filled_weighted_average_and_errors() -> None:
    result = aggregate_order_results(
        signal_id="sig-filled",
        symbol="ETHUSD",
        exec_mode=ExecutionMode.SPOT,
        broker_type=BrokerType.PAPER,
        order_results=[
            BrokerOrderResult.filled("order-1", quantity=1.0, price=100.0, commission=0.5),
            BrokerOrderResult.filled("order-2", quantity=3.0, price=110.0, commission=0.7),
            BrokerOrderResult.rejected("insufficient margin"),
        ],
    )

    assert result.success is True
    assert result.orders_submitted == 3
    assert result.orders_filled == 2
    assert result.total_quantity == 4.0
    assert result.average_price == 107.5
    assert result.total_commission == 1.2
    assert result.error_message == "insufficient margin"


def test_cancelled_partial_fill_keeps_quantity_price_and_fee_economics() -> None:
    result = aggregate_order_results(
        signal_id="sig-partial",
        symbol="BTCUSD",
        exec_mode=ExecutionMode.SPOT,
        broker_type=BrokerType.COINBASE,
        broker_account_id=101,
        order_results=[
            BrokerOrderResult(
                success=True,
                order_id="cb-partial",
                status=OrderStatus.CANCELLED,
                filled_quantity=0.25,
                filled_price=40000.0,
                commission=Decimal("2.50"),
                commission_currency="EUR",
            )
        ],
    )

    assert result.success is True
    assert result.orders_filled == 1
    assert result.total_quantity == 0.25
    assert result.average_price == 40000.0
    assert result.total_commission == 2.5
    assert result.broker_account_id == 101
    assert result.to_dict()["broker_account_id"] == 101
    assert result.order_results[0]["status"] == "cancelled"
    assert result.order_results[0]["commission_currency"] == "EUR"


def test_execution_log_status_distinguishes_no_op_from_fill_and_failure() -> None:
    # A real fill (orders submitted) → executed.
    assert execution_log_status(_result(success=True, orders_submitted=1)) == "executed"
    # A successful decision that submitted nothing → no_op (EX-2): does not
    # count as a trade, a metric success, or a feedback did_execute.
    assert (
        execution_log_status(_result(success=True, orders_submitted=0, reason="close_no_position"))
        == "no_op"
    )
    # A real broker/infra failure stays failed.
    assert execution_log_status(_result(success=False, orders_submitted=0)) == "failed"
    # A policy/risk block (execution_mode="blocked") is NOT a failure (M1): it is
    # a deterministic platform decision, recorded distinctly so it does not poison
    # the error-rate signal.
    assert (
        execution_log_status(_result(success=False, orders_submitted=0, execution_mode="blocked"))
        == "blocked"
    )


def test_to_dict_omits_reason_when_absent_and_includes_it_for_no_op() -> None:
    assert "reason" not in _result(success=True, orders_submitted=1).to_dict()
    no_op = _result(success=True, orders_submitted=0, reason="notify_only").to_dict()
    assert no_op["reason"] == "notify_only"
