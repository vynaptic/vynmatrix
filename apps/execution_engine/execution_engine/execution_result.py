"""Execution result DTOs and order-result aggregation helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .brokers.base import BrokerOrderResult
from .config import BrokerType, ExecutionMode


@dataclass
class ExecutionResult:
    """Result of executing a signal through the engine."""

    success: bool
    signal_id: str
    symbol: str
    execution_mode: str
    broker: str
    orders_submitted: int
    orders_filled: int
    total_quantity: float
    average_price: float
    total_commission: float
    order_results: list[dict[str, Any]] = field(default_factory=list)
    error_message: str | None = None
    broker_account_id: int | None = None
    settlement_currency: str | None = None
    # Why a successful result submitted no order (no-op): e.g. "hold_no_action",
    # "close_no_position", "notify_only", "no_order_built". None for real fills
    # and for failures (which carry error_message instead). Lets RCA distinguish
    # "CLOSE flattened" from "nothing to close" on the no-intents path (EX-2).
    reason: str | None = None
    # Why a blocked result was blocked (e.g. "account_state_unavailable",
    # "stale_market_data", a risk rule_code). Carries the transient-vs-terminal
    # discriminator the engine already computes so is_retryable_failure can tell a
    # momentarily-unavailable dependency (retryable) from a deterministic policy
    # rejection (terminal) — see RETRYABLE_BLOCK_REASONS (RG-2). None for fills,
    # no-ops, and non-block failures.
    block_reason: str | None = None
    executed_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization.

        ``reason`` and ``block_reason`` are omitted when None so the serialized
        shape (golden snapshots, ``execution_details``, result event) stays free
        of null noise; each appears only on the result that carries one.
        """
        result = asdict(self)
        result["executed_at"] = self.executed_at.isoformat()
        if result.get("reason") is None:
            result.pop("reason", None)
        if result.get("block_reason") is None:
            result.pop("block_reason", None)
        if result.get("broker_account_id") is None:
            result.pop("broker_account_id", None)
        if result.get("settlement_currency") is None:
            result.pop("settlement_currency", None)
        return result


# Execution modes that represent a *terminal* outcome: a deliberate policy /
# validation / freshness / circuit-breaker / score gate ("blocked"), or a
# duplicate skip ("dedup"). A non-success result in any other mode is treated as
# a *transient* infrastructure failure (broker unavailable, connect/timeout,
# order rejected) that is safe to retry — the dedup claim is the idempotency
# boundary, so a retry cannot double-submit a filled order.
_TERMINAL_EXECUTION_MODES = frozenset({"blocked", "dedup"})

# Transient infrastructure block reasons (live path): the dependency was
# momentarily unavailable or stale, so a redelivered command can legitimately
# succeed. A "blocked" result is retryable ONLY when its block_reason is one of
# these; every other blocked reason (a deterministic policy/gate rejection that
# would re-block identically) and a missing reason stay terminal. Safe-by-default:
# widening retry eligibility requires adding a reason here explicitly (RG-2).
# Kept in sync with the engine freshness helpers by
# test_freshness_reasons_are_retryable.
RETRYABLE_BLOCK_REASONS = frozenset(
    {
        "account_state_unavailable",
        "account_state_timestamp_missing",
        "account_state_timestamp_future",
        "account_state_stale",
        "sizing_state_unavailable",
        "market_data_missing",
        "market_data_price_missing",
        "market_data_timestamp_missing",
        "market_data_timestamp_future",
        "stale_market_data",
        "market_session_unavailable",
        "risk_baseline_unavailable",
    }
)


def is_retryable_failure(result: ExecutionResult) -> bool:
    """Whether a result is a transient failure that the outbox relay should retry.

    A non-success result in a real execution mode (broker unavailable,
    connect/timeout, order rejected) is transient and retryable. A terminal-mode
    result ("blocked"/"dedup") is retryable only when it carries a recognized
    *transient* block_reason (a momentarily-unavailable dependency); deterministic
    policy/gate rejections — and any blocked result without a recognized reason —
    return False so the relay does not retry an outcome that cannot change (RG-2).
    """
    if result.success:
        return False
    if result.execution_mode not in _TERMINAL_EXECUTION_MODES:
        return True
    # Terminal modes: only a "blocked" result with a recognized transient reason is
    # retryable. "dedup" (a duplicate skip) is always terminal — the original was
    # already handled, so a retry must never re-fire it.
    if result.execution_mode == "blocked":
        return result.block_reason in RETRYABLE_BLOCK_REASONS
    return False


def execution_log_status(result: ExecutionResult) -> str:
    """Map an execution result to its persisted ``execution_logs.status``.

    A *no_op* is a successful decision that submitted no order — HOLD, a CLOSE
    with no open position, NOTIFY_ONLY, or sizing/price producing no intent. It
    is recorded distinctly from a real fill (``executed``) so it does not inflate
    executed-trade counts (``count_daily_trades``), the success metric, or the
    feedback loop's ``did_execute`` check, all of which key off ``executed`` (EX-2).

    A policy/risk *block* (``execution_mode == "blocked"`` — e.g. a spot long-only
    SHORT rejection, a risk-cap breach, or a freshness guard) is recorded as
    ``blocked``, NOT ``failed`` (M1): it is a deterministic platform decision, not
    a broker/infra failure, so conflating them poisons the error-rate signal that
    monitoring keys off. ``execution_logs.status`` is a free string (no CHECK), so
    this needs no migration. Real failures (broker unavailable, fill error) stay
    ``failed``.
    """
    if not result.success:
        return "blocked" if result.execution_mode == "blocked" else "failed"
    if result.orders_submitted == 0:
        return "no_op"
    return "executed"


def blocked_result(
    *,
    signal_id: str,
    symbol: str,
    execution_mode: str,
    broker: str,
    error_message: str,
    block_reason: str | None = None,
) -> ExecutionResult:
    """Build a standard blocked execution result."""
    return ExecutionResult(
        success=False,
        signal_id=signal_id,
        symbol=symbol,
        execution_mode=execution_mode,
        broker=broker,
        orders_submitted=0,
        orders_filled=0,
        total_quantity=0.0,
        average_price=0.0,
        total_commission=0.0,
        error_message=error_message,
        block_reason=block_reason,
    )


def aggregate_order_results(
    *,
    signal_id: str,
    symbol: str,
    exec_mode: ExecutionMode,
    broker_type: BrokerType,
    order_results: list[BrokerOrderResult],
    broker_account_id: int | None = None,
    settlement_currency: str | None = None,
) -> ExecutionResult:
    """Aggregate broker order results into a single execution result."""
    orders_submitted = len(order_results)
    filled_results = [result for result in order_results if result.has_fill]
    orders_filled = len(filled_results)

    total_quantity = sum(result.filled_quantity for result in filled_results)
    total_value = sum(result.filled_quantity * result.average_price for result in filled_results)
    total_commission = sum((result.commission for result in filled_results), Decimal("0"))

    average_price = total_value / total_quantity if total_quantity > 0 else 0.0
    error_messages = [result.error_message for result in order_results if result.error_message]
    has_unknown_submission = any(
        result.error_code == "submission_unknown" for result in order_results
    )

    return ExecutionResult(
        success=not has_unknown_submission and (orders_filled > 0 or orders_submitted == 0),
        signal_id=signal_id,
        symbol=symbol,
        execution_mode="blocked" if has_unknown_submission else exec_mode.value,
        broker=broker_type.value,
        orders_submitted=orders_submitted,
        orders_filled=orders_filled,
        total_quantity=total_quantity,
        average_price=average_price,
        total_commission=float(total_commission),
        broker_account_id=broker_account_id,
        settlement_currency=settlement_currency,
        order_results=[
            {
                "order_id": result.order_id,
                "status": result.status.value,
                "filled_quantity": result.filled_quantity,
                "average_price": result.average_price,
                "commission": float(result.commission),
                "commission_currency": result.commission_currency,
                "error": result.error_message,
                **({"error_code": result.error_code} if result.error_code else {}),
            }
            for result in order_results
        ],
        error_message="; ".join(error_messages) if error_messages else None,
        block_reason="submission_unknown" if has_unknown_submission else None,
    )
