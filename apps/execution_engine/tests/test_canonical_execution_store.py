from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from execution_engine.brokers.base import BrokerFill, BrokerOrderResult, OrderStatus
from execution_engine.canonical_execution_store import (
    CanonicalExecutionStore,
    CanonicalFillConflictError,
    CanonicalOrderIdentityError,
)
from execution_engine.models import OrderIntent
from lib_application.db.models import (
    Base,
    Broker,
    CanonicalSignal,
    Execution,
    Instrument,
    LinkedBrokerAccount,
    Order,
    Strategy,
    User,
)
from lib_application.db.models import (
    OrderIntent as PersistedOrderIntent,
)


def _store() -> tuple[CanonicalExecutionStore, sessionmaker]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine, expire_on_commit=False)
    with session_local() as session:
        session.add_all(
            [
                User(user_id="user-1", email="user-1@example.com", base_ccy="USD"),
                User(user_id="user-2", email="user-2@example.com", base_ccy="EUR"),
                Strategy(strategy_id="strategy-1", strategy_name="Strategy One"),
                Instrument(
                    instr_id=1,
                    asset_class="crypto",
                    canonical="BTC/USD",
                    settlement_currency="USD",
                ),
                Instrument(
                    instr_id=2,
                    asset_class="crypto",
                    canonical="ETH/USD",
                    settlement_currency="USD",
                ),
            ]
        )
        broker = Broker(code="coinbase", name="Coinbase", capabilities={})
        session.add(broker)
        session.flush()
        session.add(
            LinkedBrokerAccount(
                account_id=101,
                user_id="user-1",
                broker_id=broker.broker_id,
                environment="live",
                display_name="Coinbase NL",
                base_ccy="USD",
                status="connected",
            )
        )
        session.add(
            CanonicalSignal(
                signal_id=501,
                strategy_id="strategy-1",
                instr_id=1,
                action="long",
                external_signal_id="real-contract-signal-1",
                ts=datetime(2026, 7, 25, tzinfo=UTC),
            )
        )
        session.commit()
    return CanonicalExecutionStore(session_factory=session_local), session_local


def _intent() -> OrderIntent:
    return OrderIntent(
        broker_code="coinbase",
        symbol="BTC-USD",
        side="BUY",
        quantity=1.0,
        order_type="market",
        metadata={"signal_id": "real-contract-signal-1"},
    )


def _fill(
    trade_id: str,
    *,
    order_id: str = "cb-order-1",
    quantity: str = "0.4",
    price: str = "100",
    commission: str = "0.40",
    timestamp: datetime = datetime(2026, 7, 25, 10, 0, tzinfo=UTC),
    raw_response: dict[str, object] | None = None,
) -> BrokerFill:
    return BrokerFill(
        trade_id=trade_id,
        order_id=order_id,
        symbol="BTC-USD",
        side="buy",
        quantity=Decimal(quantity),
        price=Decimal(price),
        commission=Decimal(commission),
        commission_currency="USD",
        timestamp=timestamp,
        venue="coinbase",
        raw_response=raw_response,
    )


def test_canonical_store_requires_owned_explicit_account() -> None:
    store, _ = _store()

    with pytest.raises(CanonicalOrderIdentityError, match="explicit linked broker"):
        store.create_order(
            local_order_id="pre-missing",
            user_id="user-1",
            account_id=None,
            broker_code="coinbase",
            execution_mode="spot",
            strategy_id="strategy-1",
            canonical_signal_id=501,
            instr_id=1,
            intent=_intent(),
            settlement_currency="USD",
            broker_environment="live",
        )

    with pytest.raises(CanonicalOrderIdentityError, match="not owned"):
        store.create_order(
            local_order_id="pre-wrong-tenant",
            user_id="user-2",
            account_id=101,
            broker_code="coinbase",
            execution_mode="spot",
            strategy_id="strategy-1",
            canonical_signal_id=501,
            instr_id=1,
            intent=_intent(),
            settlement_currency="USD",
            broker_environment="live",
        )


def test_canonical_store_rejects_instrument_mismatch_with_signal() -> None:
    store, _ = _store()

    with pytest.raises(CanonicalOrderIdentityError, match="belongs to instrument 1, not 2"):
        store.create_order(
            local_order_id="pre-wrong-instrument",
            user_id="user-1",
            account_id=101,
            broker_code="coinbase",
            execution_mode="spot",
            strategy_id="strategy-1",
            canonical_signal_id=501,
            instr_id=2,
            intent=_intent(),
            settlement_currency="USD",
            broker_environment="live",
        )


def test_canonical_store_rejects_route_and_intent_broker_mismatch() -> None:
    store, _ = _store()
    mismatched = _intent()
    mismatched.broker_code = "deribit"

    with pytest.raises(CanonicalOrderIdentityError, match="does not match intent broker"):
        store.create_order(
            local_order_id="pre-wrong-broker",
            user_id="user-1",
            account_id=101,
            broker_code="coinbase",
            execution_mode="spot",
            strategy_id="strategy-1",
            canonical_signal_id=501,
            instr_id=1,
            intent=mismatched,
            settlement_currency="USD",
            broker_environment="live",
        )


def test_canonical_store_closes_pre_submission_abort_without_deleting_audit_row() -> None:
    store, session_local = _store()
    ref = store.create_order(
        local_order_id="pre-aborted",
        user_id="user-1",
        account_id=101,
        broker_code="coinbase",
        execution_mode="spot",
        strategy_id="strategy-1",
        canonical_signal_id=501,
        instr_id=1,
        intent=_intent(),
        settlement_currency="USD",
        idempotency_key="abort-retry-key",
        broker_environment="live",
    )

    store.abort_pre_submission(
        order_id=ref.order_id,
        reason="Pending-order persistence failed",
        code="pending_order_persistence_failed",
    )

    with session_local() as session:
        intent = session.get(PersistedOrderIntent, ref.intent_id)
        order = session.get(Order, ref.order_id)
        assert intent is not None
        assert order is not None
        assert intent.status == "rejected"
        assert order.state == "rejected"
        assert order.broker_order_ref is None
        assert intent.payload["pre_submission_abort"]["code"] == (
            "pending_order_persistence_failed"
        )
        assert intent.payload["pre_submission_abort"]["reason"] == (
            "Pending-order persistence failed"
        )
        assert intent.payload["pre_submission_abort"]["recorded_at"].endswith("+00:00")

    retried = store.create_order(
        local_order_id="pre-aborted-retry",
        user_id="user-1",
        account_id=101,
        broker_code="coinbase",
        execution_mode="spot",
        strategy_id="strategy-1",
        canonical_signal_id=501,
        instr_id=1,
        intent=_intent(),
        settlement_currency="USD",
        idempotency_key="abort-retry-key",
        broker_environment="live",
    )
    assert retried.order_id == ref.order_id
    assert retried.intent_id == ref.intent_id
    assert retried.reused is False
    assert retried.local_order_id == "pre-aborted-retry"
    with session_local() as session:
        intent = session.get(PersistedOrderIntent, ref.intent_id)
        order = session.get(Order, ref.order_id)
        assert intent is not None
        assert order is not None
        assert intent.status == "routed"
        assert order.state == "new"
        assert "pre_submission_abort" not in intent.payload


def test_canonical_store_persists_exact_fills_and_replays_trade_ids_idempotently(  # noqa: PLR0915
) -> None:
    store, session_local = _store()
    ref = store.create_order(
        local_order_id="pre-1",
        user_id="user-1",
        account_id=101,
        broker_code="coinbase",
        execution_mode="spot",
        strategy_id="strategy-1",
        canonical_signal_id=501,
        instr_id=1,
        intent=_intent(),
        settlement_currency="USD",
        idempotency_key="execute:user-1:signal-1",
        broker_environment="live",
    )

    partial = BrokerOrderResult(
        success=True,
        order_id="cb-order-1",
        status=OrderStatus.PARTIALLY_FILLED,
        filled_quantity=0.4,
        filled_price=100.0,
        commission=Decimal("0.40"),
        commission_currency="USD",
    )
    first_fill = _fill("cb-trade-1")
    first_exec_ids = store.apply_broker_result(
        order_id=ref.order_id,
        result=partial,
        fills=[first_fill],
        instr_id=1,
    )
    assert len(first_exec_ids) == 1
    # Replaying the identical reconciliation response is idempotent.
    assert (
        store.apply_broker_result(
            order_id=ref.order_id,
            result=partial,
            fills=[first_fill],
            instr_id=1,
        )
        == []
    )

    second_fill = _fill(
        "cb-trade-2",
        quantity="0.6",
        price="110",
        commission="0.70",
        timestamp=datetime(2026, 7, 25, 10, 1, tzinfo=UTC),
    )
    final = BrokerOrderResult(
        success=True,
        order_id="cb-order-1",
        status=OrderStatus.FILLED,
        filled_quantity=1.0,
        filled_price=106.0,
        commission=Decimal("1.10"),
        commission_currency="USD",
    )
    second_exec_ids = store.apply_broker_result(
        order_id=ref.order_id,
        result=final,
        fills=[first_fill, second_fill],
        instr_id=1,
    )
    assert len(second_exec_ids) == 1

    replay = store.create_order(
        local_order_id="must-not-replace-original",
        user_id="user-1",
        account_id=101,
        broker_code="coinbase",
        execution_mode="spot",
        strategy_id="strategy-1",
        canonical_signal_id=501,
        instr_id=1,
        intent=_intent(),
        settlement_currency="USD",
        idempotency_key="execute:user-1:signal-1",
        broker_environment="live",
    )
    assert replay.order_id == ref.order_id
    assert replay.intent_id == ref.intent_id
    assert replay.local_order_id == "pre-1"
    assert replay.state == "filled"
    assert replay.broker_order_ref == "cb-order-1"
    assert replay.reused is True

    changed_intent = _intent()
    changed_intent.quantity = 2
    with pytest.raises(CanonicalOrderIdentityError, match="idempotency collision"):
        store.create_order(
            local_order_id="economic-conflict",
            user_id="user-1",
            account_id=101,
            broker_code="coinbase",
            execution_mode="spot",
            strategy_id="strategy-1",
            canonical_signal_id=501,
            instr_id=1,
            intent=changed_intent,
            settlement_currency="USD",
            idempotency_key="execute:user-1:signal-1",
            broker_environment="live",
        )

    durable = store.read_durable_result(ref.order_id)
    assert durable is not None
    durable_result, durable_fills = durable
    assert durable_result.status == OrderStatus.FILLED
    assert durable_result.order_id == "cb-order-1"
    assert durable_result.filled_quantity == 1
    assert durable_result.filled_price == pytest.approx(106)
    assert durable_result.commission == Decimal("1.10000000")
    assert [fill.trade_id for fill in durable_fills] == ["cb-trade-1", "cb-trade-2"]

    with session_local() as session:
        intent = session.get(PersistedOrderIntent, ref.intent_id)
        order = session.get(Order, ref.order_id)
        fills = (
            session.query(Execution)
            .filter(Execution.order_id == ref.order_id)
            .order_by(Execution.exec_id)
            .all()
        )
        assert intent is not None
        assert intent.account_id == 101
        assert intent.strategy_id == "strategy-1"
        assert intent.canonical_signal_id == 501
        assert intent.idempotency_key == "execute:user-1:signal-1"
        assert intent.side == "BUY"
        assert intent.execution_mode == "spot"
        assert intent.broker_environment == "live"
        assert intent.status == "routed"
        assert intent.payload["local_order_id"] == "pre-1"
        assert order is not None
        assert order.account_id == 101
        assert order.settlement_currency == "USD"
        assert order.broker_order_ref == "cb-order-1"
        assert order.state == "filled"
        assert len(fills) == 2
        assert [fill.trade_id for fill in fills] == ["cb-trade-1", "cb-trade-2"]
        assert sum((fill.qty for fill in fills), Decimal("0")) == Decimal("1.00000000")
        notional = sum((fill.qty * fill.price for fill in fills), Decimal("0"))
        assert notional == pytest.approx(Decimal("106"))
        assert sum((fill.fee_amount or Decimal("0") for fill in fills), Decimal("0")) == Decimal(
            "1.10000000"
        )
        assert {fill.fee_ccy for fill in fills} == {"USD"}
        assert fills[0].fill_ts == datetime(2026, 7, 25, 10, 0)
        assert fills[1].fill_ts == datetime(2026, 7, 25, 10, 1)
        assert session.query(PersistedOrderIntent).count() == 1
        assert session.query(Order).count() == 1

    changed_fill = _fill("cb-trade-1", commission="0.41")
    with pytest.raises(CanonicalFillConflictError, match="changed after persistence"):
        store.apply_broker_result(
            order_id=ref.order_id,
            result=partial,
            fills=[changed_fill],
            instr_id=1,
        )


def test_durable_result_reconstructs_exact_source_provenance() -> None:
    store, _session_local = _store()
    ref = store.create_order(
        local_order_id="historical-order",
        user_id="user-1",
        account_id=101,
        broker_code="coinbase",
        execution_mode="spot",
        strategy_id="strategy-1",
        canonical_signal_id=501,
        instr_id=1,
        intent=_intent(),
        settlement_currency="USD",
        idempotency_key="historical-exact-fill",
        broker_environment="live",
    )
    provenance = {
        "source_price_id": 101,
        "source_content_revision": 3,
        "trigger_policy_version": "historical-next-bar-open-v1",
    }
    fill = _fill(
        "historical-trade",
        quantity="1",
        raw_response=provenance,
    )
    result = BrokerOrderResult(
        success=True,
        order_id="cb-order-1",
        status=OrderStatus.FILLED,
        filled_quantity=1,
        filled_price=100,
        commission=Decimal("0.40"),
        commission_currency="USD",
        timestamp=fill.timestamp,
    )
    store.apply_broker_result(
        order_id=ref.order_id,
        result=result,
        fills=[fill],
        instr_id=1,
    )

    durable = store.read_durable_result(ref.order_id)

    assert durable is not None
    durable_result, durable_fills = durable
    assert durable_result.timestamp == fill.timestamp
    assert len(durable_fills) == 1
    assert durable_fills[0].raw_response == provenance
    assert (
        store.apply_broker_result(
            order_id=ref.order_id,
            result=durable_result,
            fills=durable_fills,
            instr_id=1,
        )
        == []
    )


@pytest.mark.parametrize(
    ("commission", "expected_commission"),
    [
        ("2.0020002670189518763134", Decimal("2.00200027")),
        ("-0.000000005", Decimal("-0.00000001")),
    ],
    ids=["positive-fee", "negative-rebate-tie"],
)
def test_canonical_store_replays_fill_at_exact_postgres_numeric_precision(
    commission: str,
    expected_commission: Decimal,
) -> None:
    store, session_local = _store()
    ref = store.create_order(
        local_order_id="pre-db-numeric-round-trip",
        user_id="user-1",
        account_id=101,
        broker_code="coinbase",
        execution_mode="spot",
        strategy_id="strategy-1",
        canonical_signal_id=501,
        instr_id=1,
        intent=_intent(),
        settlement_currency="USD",
        broker_environment="live",
    )
    fill = _fill(
        "db-numeric-round-trip",
        quantity="0.032368659999",
        price="61849.95816999999",
        commission=commission,
    )
    result = BrokerOrderResult.filled(
        order_id=fill.order_id,
        quantity=float(fill.quantity),
        price=float(fill.price),
        commission=fill.commission,
        commission_currency=fill.commission_currency,
    )

    assert store.apply_broker_result(
        order_id=ref.order_id,
        result=result,
        fills=[fill],
        instr_id=1,
    )
    assert (
        store.apply_broker_result(
            order_id=ref.order_id,
            result=result,
            fills=[fill],
            instr_id=1,
        )
        == []
    )

    with session_local() as session:
        execution = session.query(Execution).filter(Execution.order_id == ref.order_id).one()
        assert execution.qty == Decimal("0.03236866")
        assert execution.price == Decimal("61849.95817000")
        assert execution.fee_amount == expected_commission

    changed_fill = _fill(
        fill.trade_id,
        quantity=str(fill.quantity),
        price="61849.95817001",
        commission=commission,
    )
    changed_result = BrokerOrderResult.filled(
        order_id=changed_fill.order_id,
        quantity=float(changed_fill.quantity),
        price=float(changed_fill.price),
        commission=changed_fill.commission,
        commission_currency=changed_fill.commission_currency,
    )
    with pytest.raises(CanonicalFillConflictError, match="changed after persistence"):
        store.apply_broker_result(
            order_id=ref.order_id,
            result=changed_result,
            fills=[changed_fill],
            instr_id=1,
        )


@pytest.mark.parametrize(
    ("quantity", "price", "message"),
    [
        ("0.000000004", "100", "requires positive quantity"),
        ("1", "1000000000000", "price exceeds NUMERIC"),
    ],
    ids=["quantity-rounds-to-zero", "price-out-of-range"],
)
def test_canonical_store_rejects_fill_outside_exact_ledger_representation(
    quantity: str,
    price: str,
    message: str,
) -> None:
    store, _session_local = _store()
    ref = store.create_order(
        local_order_id="pre-invalid-db-numeric",
        user_id="user-1",
        account_id=101,
        broker_code="coinbase",
        execution_mode="spot",
        strategy_id="strategy-1",
        canonical_signal_id=501,
        instr_id=1,
        intent=_intent(),
        settlement_currency="USD",
        broker_environment="live",
    )
    fill = _fill(
        "invalid-db-numeric",
        quantity=quantity,
        price=price,
        commission="0",
    )
    result = BrokerOrderResult.filled(
        order_id=fill.order_id,
        quantity=float(fill.quantity),
        price=float(fill.price),
        commission=fill.commission,
        commission_currency=fill.commission_currency,
    )

    with pytest.raises(CanonicalFillConflictError, match=message):
        store.apply_broker_result(
            order_id=ref.order_id,
            result=result,
            fills=[fill],
            instr_id=1,
        )


@pytest.mark.parametrize(
    ("fill_overrides", "message"),
    [
        ({"side": "sell"}, "does not match canonical order"),
        ({"symbol": "ETH-USD"}, "does not match canonical order"),
    ],
    ids=["wrong-side", "wrong-symbol"],
)
def test_canonical_store_rejects_fill_identity_mismatch_and_replay(
    fill_overrides: dict[str, str],
    message: str,
) -> None:
    store, session_local = _store()
    intent = _intent()
    intent.metadata = {"broker_symbol": "BTC-USD"}
    ref = store.create_order(
        local_order_id="pre-fill-identity",
        user_id="user-1",
        account_id=101,
        broker_code="coinbase",
        execution_mode="spot",
        strategy_id="strategy-1",
        canonical_signal_id=501,
        instr_id=1,
        intent=intent,
        settlement_currency="USD",
        broker_environment="live",
    )
    exact = _fill("identity-fill", quantity="1", commission="0")
    result = BrokerOrderResult(
        success=True,
        order_id="cb-order-1",
        status=OrderStatus.FILLED,
        filled_quantity=1,
        filled_price=100,
    )
    assert store.apply_broker_result(
        order_id=ref.order_id,
        result=result,
        fills=[exact],
        instr_id=1,
    )

    changed = BrokerFill(
        trade_id=exact.trade_id,
        order_id=exact.order_id,
        symbol=fill_overrides.get("symbol", exact.symbol),
        side=fill_overrides.get("side", exact.side),
        quantity=exact.quantity,
        price=exact.price,
        commission=exact.commission,
        commission_currency=exact.commission_currency,
        timestamp=exact.timestamp,
        venue=exact.venue,
    )
    with pytest.raises(CanonicalFillConflictError, match=message):
        store.apply_broker_result(
            order_id=ref.order_id,
            result=result,
            fills=[changed],
            instr_id=1,
        )

    with session_local() as session:
        assert session.query(Execution).filter(Execution.order_id == ref.order_id).count() == 1


def test_canonical_store_rejects_fill_instrument_mismatch_with_signal() -> None:
    store, _ = _store()
    ref = store.create_order(
        local_order_id="pre-fill-wrong-instrument",
        user_id="user-1",
        account_id=101,
        broker_code="coinbase",
        execution_mode="spot",
        strategy_id="strategy-1",
        canonical_signal_id=501,
        instr_id=1,
        intent=_intent(),
        settlement_currency="USD",
        broker_environment="live",
    )

    with pytest.raises(CanonicalFillConflictError, match="does not match signal"):
        store.apply_broker_result(
            order_id=ref.order_id,
            result=BrokerOrderResult(
                success=True,
                order_id="cb-wrong-instrument",
                status=OrderStatus.FILLED,
                filled_quantity=1,
                filled_price=100,
            ),
            fills=[
                _fill(
                    "cb-wrong-instrument-trade",
                    order_id="cb-wrong-instrument",
                    quantity="1",
                    commission="0",
                )
            ],
            instr_id=2,
        )


def test_canonical_store_rejects_status_fill_without_exact_trade_records() -> None:
    store, _ = _store()
    ref = store.create_order(
        local_order_id="pre-fee-currency",
        user_id="user-1",
        account_id=101,
        broker_code="coinbase",
        execution_mode="spot",
        strategy_id="strategy-1",
        canonical_signal_id=501,
        instr_id=1,
        intent=_intent(),
        settlement_currency="USD",
        broker_environment="live",
    )

    with pytest.raises(CanonicalFillConflictError, match="without any exact fills"):
        store.apply_broker_result(
            order_id=ref.order_id,
            result=BrokerOrderResult(
                success=True,
                order_id="cb-missing-fills",
                status=OrderStatus.FILLED,
                filled_quantity=1,
                filled_price=100,
                commission=Decimal("0.10"),
            ),
            fills=[],
            instr_id=1,
        )


def test_broker_fill_requires_explicit_fee_currency() -> None:
    with pytest.raises(ValueError, match="commission_currency"):
        BrokerFill(
            trade_id="cb-trade-no-fee-currency",
            order_id="cb-order-1",
            symbol="BTC-USD",
            side="buy",
            quantity=Decimal("1"),
            price=Decimal("100"),
            commission=Decimal("0"),
            commission_currency="",
            timestamp=datetime(2026, 7, 25, 10, 0, tzinfo=UTC),
            venue="coinbase",
        )


def test_canonical_store_rejects_incomplete_exact_fill_set() -> None:
    store, _ = _store()
    ref = store.create_order(
        local_order_id="pre-incomplete-fills",
        user_id="user-1",
        account_id=101,
        broker_code="coinbase",
        execution_mode="spot",
        strategy_id="strategy-1",
        canonical_signal_id=501,
        instr_id=1,
        intent=_intent(),
        settlement_currency="USD",
        broker_environment="live",
    )

    with pytest.raises(CanonicalFillConflictError, match="incomplete fill set"):
        store.apply_broker_result(
            order_id=ref.order_id,
            result=BrokerOrderResult(
                success=True,
                order_id="cb-order-1",
                status=OrderStatus.FILLED,
                filled_quantity=1,
                filled_price=100,
            ),
            fills=[_fill("only-half", quantity="0.5")],
            instr_id=1,
        )
