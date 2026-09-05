"""Bracket sequencing: exits are placed only after the entry fills (Gap L).

On a spot broker you cannot SELL what you do not yet hold, so the stop-loss /
take-profit legs must follow the entry FILL, sized to the quantity that filled.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import SQLAlchemyError

from execution_engine.brokers.base import BrokerFill, BrokerOrderResult, OrderStatus
from execution_engine.models import OrderIntent
from execution_engine.order_executor import OrderExecutor
from execution_engine.pending_orders import PendingOrderPersistenceError
from lib_strategy.signals.signal import Signal, SignalAction


def _result(
    *,
    filled: bool,
    qty: float = 0.0,
    order_id: str = "oid",
) -> BrokerOrderResult:
    status = OrderStatus.FILLED if filled else OrderStatus.CANCELLED
    if not filled and qty == 0:
        status = OrderStatus.WORKING
    return BrokerOrderResult(
        success=True,
        status=status,
        filled_quantity=qty,
        filled_price=100.0 if qty > 0 else None,
        order_id=order_id,
    )


class _FakeBroker:
    def __init__(
        self,
        results: list[Any],
        *,
        cancel_result: bool = True,
        fills: list[BrokerFill] | None = None,
    ) -> None:
        self._results = results
        self._cancel_result = cancel_result
        self._fills = list(fills or [])
        self.submitted: list[OrderIntent] = []
        self.cancelled: list[str] = []
        self.events: list[str] = []

    async def submit_order(self, intent: OrderIntent) -> Any:
        self.submitted.append(intent)
        self.events.append(f"submit:{intent.side}:{intent.order_type}")
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def cancel_order(self, order_id: str) -> bool:
        self.cancelled.append(order_id)
        self.events.append(f"cancel:{order_id}")
        return self._cancel_result

    async def get_fills(self, order_id: str) -> list[BrokerFill]:
        assert all(fill.order_id == order_id for fill in self._fills)
        return list(self._fills)


class _FakeTracker:
    def __init__(self) -> None:
        self.pending_orders_cache: dict[str, Any] = {}

    def persist_order(self, **kwargs: Any) -> str:
        return str(kwargs["order_id"])

    def resolve_order(self, order_id: str, _res: Any, **_kw: Any) -> str:
        return order_id


class _PersistenceTracker(_FakeTracker):
    def __init__(
        self,
        *,
        failure: Exception | None = None,
        acknowledge: bool = True,
    ) -> None:
        super().__init__()
        self.failure = failure
        self.acknowledge = acknowledge

    @property
    def persistence_enabled(self) -> bool:
        return True

    def persist_order(self, **kwargs: Any) -> str:
        if self.failure is not None:
            if isinstance(self.failure, (SQLAlchemyError, OSError)):
                msg = "pending-order adapter write failed"
                raise PendingOrderPersistenceError(msg) from self.failure
            raise self.failure
        return str(kwargs["order_id"]) if self.acknowledge else ""


class _AmbiguousTracker(_PersistenceTracker):
    def __init__(self) -> None:
        super().__init__()
        self.unknown_submissions: list[dict[str, str]] = []

    def mark_submission_unknown(
        self,
        order_id: str,
        *,
        reason: str,
        client_order_id: str,
    ) -> bool:
        self.unknown_submissions.append(
            {
                "order_id": order_id,
                "reason": reason,
                "client_order_id": client_order_id,
            }
        )
        return True


class _RecordingCanonicalStore:
    def __init__(self) -> None:
        self.aborts: list[dict[str, Any]] = []

    def create_order(self, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(order_id=7001)

    def abort_pre_submission(self, **kwargs: Any) -> None:
        self.aborts.append(kwargs)


class _IdempotentCanonicalStore(_RecordingCanonicalStore):
    def __init__(
        self,
        *,
        durable_result: BrokerOrderResult,
        durable_fills: list[BrokerFill],
    ) -> None:
        super().__init__()
        self.durable_result = durable_result
        self.durable_fills = list(durable_fills)
        self.keys: list[str | None] = []
        self.local_order_id = ""

    def create_order(self, **kwargs: Any) -> SimpleNamespace:
        self.keys.append(kwargs.get("idempotency_key"))
        reused = bool(self.local_order_id)
        if not reused:
            self.local_order_id = str(kwargs["local_order_id"])
        return SimpleNamespace(
            order_id=7001,
            local_order_id=self.local_order_id,
            reused=reused,
        )

    def read_durable_result(
        self,
        _order_id: int,
    ) -> tuple[BrokerOrderResult, list[BrokerFill]]:
        return self.durable_result, list(self.durable_fills)


class _AmbiguousCanonicalStore(_RecordingCanonicalStore):
    def __init__(self) -> None:
        super().__init__()
        self.local_order_id = ""
        self.state = "new"
        self.unknown_reasons: list[str] = []

    def create_order(self, **kwargs: Any) -> SimpleNamespace:
        reused = bool(self.local_order_id)
        if not reused:
            self.local_order_id = str(kwargs["local_order_id"])
        return SimpleNamespace(
            order_id=7001,
            local_order_id=self.local_order_id,
            state=self.state,
            reused=reused,
        )

    def mark_submission_unknown(self, *, order_id: int, reason: str) -> None:
        assert order_id == 7001
        self.state = "submission_unknown"
        self.unknown_reasons.append(reason)

    def read_durable_result(
        self,
        _order_id: int,
    ) -> tuple[BrokerOrderResult, list[BrokerFill]] | None:
        raise AssertionError("submission_unknown cannot be treated as a durable rejection")


def _entry() -> OrderIntent:
    return OrderIntent("coinbase", "SOLUSD", "BUY", 0.07, "market", metadata={})


def _signal() -> Signal:
    return Signal(
        signal_id="signal-1",
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol="SOLUSD",
        action=SignalAction.LONG,
        confidence=0.8,
        timestamp=datetime.now(tz=UTC),
        instrument_id=1,
        metadata={"canonical_signal_id": 501},
    )


def test_replayed_dispatch_returns_canonical_fill_without_second_broker_order() -> None:
    result = _result(filled=True, qty=0.07, order_id="venue-order-1")
    fill = BrokerFill(
        trade_id="venue-trade-1",
        order_id="venue-order-1",
        symbol="SOLUSD",
        side="buy",
        quantity=Decimal("0.07"),
        price=Decimal("100"),
        commission=Decimal("0"),
        commission_currency="USD",
        timestamp=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        venue="coinbase",
    )
    broker = _FakeBroker([result], fills=[fill])
    canonical_store = _IdempotentCanonicalStore(
        durable_result=result,
        durable_fills=[fill],
    )
    executor = OrderExecutor(
        tracker=_FakeTracker(),
        canonical_execution_store=canonical_store,
    )

    first = asyncio.run(
        executor.execute(
            broker,
            [_entry()],
            user_id="user-1",
            signal=_signal(),
            execution_mode="spot",
            broker_code="coinbase",
            broker_environment="live",
            broker_account_id=101,
            settlement_currency="USD",
            execution_key="dispatch-dedup-key",
        )
    )
    replay = asyncio.run(
        executor.execute(
            broker,
            [_entry()],
            user_id="user-1",
            signal=_signal(),
            execution_mode="spot",
            broker_code="coinbase",
            broker_environment="live",
            broker_account_id=101,
            settlement_currency="USD",
            execution_key="dispatch-dedup-key",
        )
    )

    assert first[0].order_id == "venue-order-1"
    assert replay[0].order_id == "venue-order-1"
    assert len(broker.submitted) == 1
    assert len(canonical_store.keys) == 2
    assert canonical_store.keys[0] == canonical_store.keys[1]
    assert canonical_store.keys[0] is not None
    assert len(canonical_store.keys[0] or "") == 64


def test_ambiguous_submit_is_persisted_and_never_resubmitted() -> None:
    broker = _FakeBroker([TimeoutError("acknowledgement lost")])
    tracker = _AmbiguousTracker()
    canonical_store = _AmbiguousCanonicalStore()
    executor = OrderExecutor(
        tracker=tracker,
        canonical_execution_store=canonical_store,
    )
    kwargs = {
        "user_id": "user-1",
        "signal": _signal(),
        "execution_mode": "spot",
        "broker_code": "coinbase",
        "broker_environment": "live",
        "broker_account_id": 101,
        "settlement_currency": "USD",
        "execution_key": "dispatch-ambiguous-key",
    }

    first = asyncio.run(executor.execute(broker, [_entry()], **kwargs))
    replay = asyncio.run(executor.execute(broker, [_entry()], **kwargs))

    assert len(broker.submitted) == 1
    assert first[0].error_code == "submission_unknown"
    assert replay[0].error_code == "submission_unknown"
    assert len(tracker.unknown_submissions) == 1
    assert tracker.unknown_submissions[0]["order_id"] == canonical_store.local_order_id
    assert tracker.unknown_submissions[0]["client_order_id"] == canonical_store.local_order_id
    assert len(canonical_store.unknown_reasons) == 1


@pytest.mark.parametrize(
    "failure",
    [
        SQLAlchemyError("database write failed"),
        OSError("operating-system write failed"),
        PendingOrderPersistenceError("write was not confirmed"),
    ],
    ids=["database-error", "os-error", "persistence-error"],
)
def test_broker_submission_is_blocked_when_pending_persistence_raises(
    failure: Exception,
) -> None:
    broker = _FakeBroker([_result(filled=True, qty=0.07)])
    executor = OrderExecutor(tracker=_PersistenceTracker(failure=failure))

    results = asyncio.run(
        executor.execute(
            broker,
            [_entry()],
            user_id="user-1",
            signal=_signal(),
            broker_account_id=101,
            settlement_currency="USD",
        )
    )

    assert broker.submitted == []
    assert len(results) == 1
    assert results[0].error_code == "pending_order_persistence_failed"


@pytest.mark.parametrize(
    ("failure", "acknowledge", "reason"),
    [
        (
            PendingOrderPersistenceError("write failed"),
            True,
            "Pending-order persistence failed",
        ),
        (
            None,
            False,
            "Pending-order persistence acknowledgement mismatch",
        ),
    ],
    ids=["write-failure", "acknowledgement-mismatch"],
)
def test_pre_submission_failure_closes_canonical_order(
    failure: Exception | None,
    acknowledge: bool,
    reason: str,
) -> None:
    broker = _FakeBroker([_result(filled=True, qty=0.07)])
    canonical_store = _RecordingCanonicalStore()
    executor = OrderExecutor(
        tracker=_PersistenceTracker(failure=failure, acknowledge=acknowledge),
        canonical_execution_store=canonical_store,
    )

    result = asyncio.run(
        executor.execute(
            broker,
            [_entry()],
            user_id="user-1",
            signal=_signal(),
            execution_mode="spot",
            broker_code="coinbase",
            broker_environment="live",
            broker_account_id=101,
            settlement_currency="USD",
        )
    )

    assert broker.submitted == []
    assert result[0].error_code == "pending_order_persistence_failed"
    assert canonical_store.aborts == [
        {
            "order_id": 7001,
            "reason": reason,
            "code": "pending_order_persistence_failed",
        }
    ]


def test_broker_submission_is_blocked_without_persistence_acknowledgement() -> None:
    broker = _FakeBroker([_result(filled=True, qty=0.07)])
    executor = OrderExecutor(tracker=_PersistenceTracker(acknowledge=False))

    results = asyncio.run(
        executor.execute(
            broker,
            [_entry()],
            user_id="user-1",
            signal=_signal(),
            broker_account_id=101,
            settlement_currency="USD",
        )
    )

    assert broker.submitted == []
    assert len(results) == 1
    assert results[0].error_code == "pending_order_persistence_failed"


def test_unexpected_persistence_exception_never_reaches_broker() -> None:
    broker = _FakeBroker([_result(filled=True, qty=0.07)])
    canonical_store = _RecordingCanonicalStore()
    executor = OrderExecutor(
        tracker=_PersistenceTracker(failure=RuntimeError("persistence contract bug")),
        canonical_execution_store=canonical_store,
    )

    with pytest.raises(RuntimeError, match="persistence contract bug"):
        asyncio.run(
            executor.execute(
                broker,
                [_entry()],
                user_id="user-1",
                signal=_signal(),
                execution_mode="spot",
                broker_code="coinbase",
                broker_environment="live",
                broker_account_id=101,
                settlement_currency="USD",
            )
        )

    assert broker.submitted == []
    assert canonical_store.aborts == [
        {
            "order_id": 7001,
            "reason": "Unexpected pending-order persistence failure",
            "code": "pending_order_persistence_contract_failed",
        }
    ]


def _stop() -> OrderIntent:
    return OrderIntent(
        "coinbase",
        "SOLUSD",
        "SELL",
        0.07,
        "stop",
        stop_price=80.0,
        metadata={"purpose": "stop_loss"},
    )


def _take_profit() -> OrderIntent:
    return OrderIntent(
        "coinbase",
        "SOLUSD",
        "SELL",
        0.07,
        "limit",
        limit_price=90.0,
        metadata={"purpose": "take_profit"},
    )


def test_exits_placed_after_entry_fill_sized_to_fill() -> None:
    # entry fills 0.0686 (less than the requested 0.07 after fees/slippage)
    broker = _FakeBroker(
        [
            _result(filled=True, qty=0.0686, order_id="entry"),
            _result(filled=False, order_id="stop"),
            _result(filled=False, order_id="tp"),
        ]
    )
    ex = OrderExecutor(tracker=_FakeTracker())
    asyncio.run(ex.execute(broker, [_entry(), _stop(), _take_profit()]))

    assert len(broker.submitted) == 3
    assert broker.submitted[0].order_type == "market"  # entry first
    # both exits sized to what actually filled, not the requested 0.07
    assert broker.submitted[1].metadata["purpose"] == "stop_loss"
    assert broker.submitted[1].quantity == 0.0686
    assert broker.submitted[2].quantity == 0.0686


def test_exits_skipped_when_entry_does_not_fill() -> None:
    broker = _FakeBroker([_result(filled=False, order_id="entry")])
    ex = OrderExecutor(tracker=_FakeTracker())
    asyncio.run(ex.execute(broker, [_entry(), _stop(), _take_profit()]))

    # only the entry was submitted; unprotected SELLs were NOT sent
    assert len(broker.submitted) == 1
    assert broker.submitted[0].side == "BUY"


def test_exits_placed_for_partial_fill_on_cancelled_entry() -> None:
    # Entry timed out and was cancelled by the adapter with a PARTIAL fill: the
    # filled portion is a live position and MUST get exits sized to it (C1b).
    broker = _FakeBroker(
        [
            _result(filled=False, qty=0.03, order_id="entry"),  # cancelled, partial
            _result(filled=False, order_id="stop"),
            _result(filled=False, order_id="tp"),
        ]
    )
    ex = OrderExecutor(tracker=_FakeTracker())
    asyncio.run(ex.execute(broker, [_entry(), _stop(), _take_profit()]))

    assert len(broker.submitted) == 3
    assert broker.submitted[1].metadata["purpose"] == "stop_loss"
    assert broker.submitted[1].quantity == 0.03
    assert broker.submitted[2].quantity == 0.03


def _close() -> OrderIntent:
    return OrderIntent(
        "coinbase",
        "SOLUSD",
        "SELL",
        0.07,
        "market",
        time_in_force="ioc",
        metadata={"purpose": "close_position"},
    )


def _resting_bracket_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "order_id": "pre-abc",
        "broker_order_id": "bracket-1",
        "breaker_key": None,
        "credential_ref": None,
        "broker_environment": None,
        "symbol": "SOLUSD",
        "side": "SELL",
        "broker": "paper",
        "execution_mode": "paper",
        "status": "submitted",
    }
    row.update(overrides)
    return row


def test_close_cancels_resting_bracket_before_market_sell() -> None:
    # C2: the resting OCO bracket reserves the base balance on spot — the flatten
    # must cancel it FIRST or it rejects / orphans a live GTC bracket.
    broker = _FakeBroker([_result(filled=True, qty=0.07, order_id="close")])
    tracker = _FakeTracker()
    tracker.pending_orders_cache["bracket-1"] = _resting_bracket_row()
    ex = OrderExecutor(tracker=tracker)
    asyncio.run(ex.execute(broker, [_close()]))

    assert broker.cancelled == ["bracket-1"]
    # Cancel happened strictly BEFORE the market sell was submitted.
    assert broker.events == ["cancel:bracket-1", "submit:SELL:market"]
    # Cancelled row is removed from the pending cache.
    assert "bracket-1" not in tracker.pending_orders_cache


def test_close_proceeds_when_cancel_fails() -> None:
    # If the resting order already filled, cancel returns False — the close must
    # still go out for the remaining quantity.
    broker = _FakeBroker(
        [_result(filled=True, qty=0.07, order_id="close")],
        cancel_result=False,
    )
    tracker = _FakeTracker()
    tracker.pending_orders_cache["bracket-1"] = _resting_bracket_row()
    ex = OrderExecutor(tracker=tracker)
    results = asyncio.run(ex.execute(broker, [_close()]))

    assert broker.cancelled == ["bracket-1"]
    assert len(broker.submitted) == 1
    assert broker.submitted[0].order_type == "market"
    assert results[0].is_filled


def test_close_does_not_cancel_other_contexts_orders() -> None:
    # Scoping: another symbol / another credential context must NOT be touched.
    broker = _FakeBroker([_result(filled=True, qty=0.07, order_id="close")])
    tracker = _FakeTracker()
    tracker.pending_orders_cache["other-symbol"] = _resting_bracket_row(
        broker_order_id="other-symbol", symbol="BTCUSD"
    )
    tracker.pending_orders_cache["other-cred"] = _resting_bracket_row(
        broker_order_id="other-cred", credential_ref="user-2"
    )
    ex = OrderExecutor(tracker=tracker)
    asyncio.run(ex.execute(broker, [_close()]))

    assert broker.cancelled == []
    assert len(broker.submitted) == 1


class _DurableTracker(_FakeTracker):
    def __init__(self) -> None:
        super().__init__()
        self.durably_cancelled: list[tuple[str, str]] = []

    def cancel_working_order(self, order_id: str, *, reason: str) -> bool:
        self.durably_cancelled.append((order_id, reason))
        return True


def test_close_durably_cancels_pending_row_when_broker_confirms_cancel() -> None:
    # The broker's in-memory cancel alone left the durable pending row working;
    # the paper lifecycle then retried it forever against a flat book.
    broker = _FakeBroker([_result(filled=True, qty=0.07, order_id="close")])
    tracker = _DurableTracker()
    tracker.pending_orders_cache["bracket-1"] = _resting_bracket_row()
    asyncio.run(OrderExecutor(tracker=tracker).execute(broker, [_close()]))

    assert tracker.durably_cancelled == [("bracket-1", "Superseded by close for SOLUSD")]
    assert "bracket-1" not in tracker.pending_orders_cache


def test_close_keeps_live_broker_row_when_cancel_is_unconfirmed() -> None:
    # A live venue that did not confirm the cancel keeps its durable row for
    # reconciliation; only the paper lifecycle owns paper resting orders.
    broker = _FakeBroker(
        [_result(filled=True, qty=0.07, order_id="close")],
        cancel_result=False,
    )
    tracker = _DurableTracker()
    tracker.pending_orders_cache["bracket-1"] = _resting_bracket_row()
    asyncio.run(OrderExecutor(tracker=tracker).execute(broker, [_close()]))

    assert tracker.durably_cancelled == []
    assert "bracket-1" in tracker.pending_orders_cache
