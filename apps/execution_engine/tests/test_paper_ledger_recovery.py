"""Crash-window recovery from canonical local-paper fill truth."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from execution_engine.broker_resolver import (
    BrokerResolver,
)
from execution_engine.brokers.base import BrokerFill, BrokerOrderResult, OrderStatus
from execution_engine.brokers.paper import PaperBroker
from execution_engine.canonical_execution_store import (
    CanonicalExecutionStore,
    CanonicalFillConflictError,
)
from execution_engine.config import BrokerType, FuturesConfig
from execution_engine.execution_position_store import ExecutionPositionStore
from execution_engine.futures_builder import FuturesOrderBuilder, to_futures_intent
from execution_engine.models import (
    OptionsIntent,
    OptionsLeg,
    OrderCurrencyContext,
    OrderIntent,
)
from execution_engine.paper_ledger_recovery import (
    PaperLedgerRecoveryError,
    recover_paper_ledger,
)
from execution_engine.pending_orders import PendingOrderRepository
from execution_engine.reconciliation import ReconciliationWorker
from execution_engine.reconciliation_tracker import ReconciliationTracker
from lib_application.db.models import (
    Base,
    Broker,
    CanonicalSignal,
    ExecutionMetric,
    Instrument,
    LinkedBrokerAccount,
    Order,
    PendingOrder,
    Position,
    Strategy,
    User,
)
from lib_application.db.models import (
    OrderIntent as CanonicalOrderIntent,
)

_REQUESTED_AT = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
_BUY_FILLED_AT = datetime(2026, 7, 24, 12, 0, 1, tzinfo=UTC)
_SELL_FILLED_AT = datetime(2026, 7, 24, 13, 0, 1, tzinfo=UTC)


def _session_local() -> sessionmaker:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [
                User(user_id="user-nl", email="nl@example.com", base_ccy="EUR"),
                User(user_id="user-in", email="in@example.com", base_ccy="INR"),
                Strategy(strategy_id="strategy-1", strategy_name="Strategy One"),
                Instrument(
                    instr_id=1,
                    asset_class="crypto",
                    canonical="BTC-USD",
                    settlement_currency="USD",
                ),
            ]
        )
        broker = Broker(code="coinbase", name="Coinbase", capabilities={})
        session.add(broker)
        session.flush()
        session.add_all(
            [
                LinkedBrokerAccount(
                    account_id=101,
                    user_id="user-nl",
                    broker_id=broker.broker_id,
                    environment="paper",
                    display_name="NL Coinbase Paper",
                    base_ccy="EUR",
                    paper_initial_equity=Decimal("10000"),
                    paper_initial_cash=Decimal("10000"),
                    status="connected",
                ),
                LinkedBrokerAccount(
                    account_id=102,
                    user_id="user-nl",
                    broker_id=broker.broker_id,
                    environment="paper",
                    display_name="NL Coinbase Paper 2",
                    base_ccy="EUR",
                    paper_initial_equity=Decimal("20000"),
                    paper_initial_cash=Decimal("20000"),
                    status="connected",
                ),
                LinkedBrokerAccount(
                    account_id=201,
                    user_id="user-in",
                    broker_id=broker.broker_id,
                    environment="paper",
                    display_name="IN Coinbase Paper",
                    base_ccy="INR",
                    paper_initial_equity=Decimal("30000"),
                    paper_initial_cash=Decimal("30000"),
                    status="connected",
                ),
            ]
        )
        session.commit()
    return factory


def _context(
    *,
    account_currency: str = "EUR",
    rate: str = "1.25",
    requested_at: datetime = _REQUESTED_AT,
) -> OrderCurrencyContext:
    return OrderCurrencyContext(
        account_currency=account_currency,
        settlement_currency="USD",
        account_to_settlement_rate=Decimal(rate),
        requested_at=requested_at,
        observed_at=requested_at,
        source="ecb",
    )


def _broker_fill(
    *,
    local_order_id: str,
    side: str,
    quantity: Decimal | float | str,
    price: Decimal | float | str,
    commission: Decimal | str,
    filled_at: datetime,
    trade_id: str | None = None,
) -> BrokerFill:
    return BrokerFill(
        trade_id=trade_id or f"trade:{local_order_id}",
        order_id=f"broker:{local_order_id}",
        symbol="BTC-USD",
        side=side.lower(),
        quantity=Decimal(str(quantity)),
        price=Decimal(str(price)),
        commission=Decimal(str(commission)),
        commission_currency="USD",
        timestamp=filled_at,
        venue="coinbase-paper",
    )


def _append_fill(
    session_local: sessionmaker,
    *,
    local_order_id: str,
    side: str,
    quantity: float,
    price: float,
    commission: str,
    filled_at: datetime,
    account_id: int = 101,
    user_id: str = "user-nl",
    context: OrderCurrencyContext | None = None,
) -> int:
    with session_local() as session:
        signal = CanonicalSignal(
            strategy_id="strategy-1",
            instr_id=1,
            action="long" if side == "BUY" else "flat",
            external_signal_id=f"signal:{local_order_id}",
            ts=filled_at,
        )
        session.add(signal)
        session.flush()
        signal_id = int(signal.signal_id)
        session.commit()

    store = CanonicalExecutionStore(session_factory=session_local)
    order = store.create_order(
        local_order_id=local_order_id,
        user_id=user_id,
        account_id=account_id,
        broker_code="coinbase",
        execution_mode="spot",
        strategy_id="strategy-1",
        canonical_signal_id=signal_id,
        instr_id=1,
        intent=OrderIntent(
            broker_code="coinbase",
            symbol="BTC-USD",
            side=side,
            quantity=quantity,
            order_type="market",
            currency_context=context,
        ),
        settlement_currency="USD",
        broker_environment="paper",
    )
    result = BrokerOrderResult(
        success=True,
        order_id=f"broker:{local_order_id}",
        status=OrderStatus.FILLED,
        filled_quantity=quantity,
        filled_price=price,
        commission=Decimal(commission),
        commission_currency="USD",
        timestamp=filled_at,
    )
    exec_ids = store.apply_broker_result(
        order_id=order.order_id,
        result=result,
        fills=[
            _broker_fill(
                local_order_id=local_order_id,
                side=side,
                quantity=quantity,
                price=price,
                commission=commission,
                filled_at=filled_at,
            )
        ],
        instr_id=1,
    )
    assert len(exec_ids) == 1
    return exec_ids[0]


def _append_contract_fill(
    session_local: sessionmaker,
    *,
    execution_mode: str,
    local_order_id: str | None = None,
    side: str = "BUY",
    quantity: float = 2,
    reference_price: float = 100,
    fill_price: float = 101,
    commission: str = "1",
    rate: str = "1.25",
    filled_at: datetime = _BUY_FILLED_AT,
    reduce_only: bool = False,
) -> int:
    local_order_id = local_order_id or f"{execution_mode}-contract"
    with session_local() as session:
        signal = CanonicalSignal(
            strategy_id="strategy-1",
            instr_id=1,
            action="long" if side == "BUY" else "flat",
            external_signal_id=f"signal:{local_order_id}",
            ts=filled_at,
        )
        session.add(signal)
        session.flush()
        signal_id = int(signal.signal_id)
        session.commit()

    result = FuturesOrderBuilder(
        FuturesConfig(
            contract_size=5,
            max_leverage=10,
            target_leverage=2,
            use_perpetual=execution_mode == "perpetual",
        )
    ).build_order(
        symbol="BTC-USD",
        is_long=side == "BUY",
        price=reference_price,
        notional_size=quantity * reference_price * 5,
        available_margin=10000,
    )
    intent = to_futures_intent(result, broker_code="coinbase")[0]
    intent.currency_context = _context(rate=rate, requested_at=filled_at)
    if reduce_only:
        assert intent.metadata is not None
        intent.metadata["purpose"] = "close_position"
        intent.metadata["reduce_only"] = True

    store = CanonicalExecutionStore(session_factory=session_local)
    order = store.create_order(
        local_order_id=local_order_id,
        user_id="user-nl",
        account_id=101,
        broker_code="coinbase",
        execution_mode=execution_mode,
        strategy_id="strategy-1",
        canonical_signal_id=signal_id,
        instr_id=1,
        intent=intent,
        settlement_currency="USD",
        broker_environment="paper",
    )
    exec_ids = store.apply_broker_result(
        order_id=order.order_id,
        result=BrokerOrderResult(
            success=True,
            order_id=f"broker:{local_order_id}",
            status=OrderStatus.FILLED,
            filled_quantity=result.quantity,
            # A real fill need not equal the builder's sizing price. Without
            # the observed contract multiplier, neither persisted notional
            # nor quantity * fill price can recover exact economics.
            filled_price=fill_price,
            commission=Decimal(commission),
            commission_currency="USD",
            timestamp=filled_at,
        ),
        fills=[
            _broker_fill(
                local_order_id=local_order_id,
                side=side,
                quantity=result.quantity,
                price=fill_price,
                commission=commission,
                filled_at=filled_at,
            )
        ],
        instr_id=1,
    )
    assert len(exec_ids) == 1
    return exec_ids[0]


def _recover(
    session_local: sessionmaker,
    position_store: ExecutionPositionStore,
    *,
    account_id: int = 101,
    user_id: str = "user-nl",
    account_currency: str = "EUR",
):
    return recover_paper_ledger(
        session_local,
        position_store,
        user_id=user_id,
        broker_code="coinbase",
        environment="paper",
        account_id=account_id,
        account_currency=account_currency,
        configured_equity=None,
        configured_cash=None,
    )


def test_restart_replays_committed_fill_and_repairs_missing_projections() -> None:
    session_local = _session_local()
    position_store = ExecutionPositionStore(session_factory=session_local)
    buy_exec_id = _append_fill(
        session_local,
        local_order_id="buy-1",
        side="BUY",
        quantity=2,
        price=100,
        commission="2",
        filled_at=_BUY_FILLED_AT,
        context=_context(rate="1.25"),
    )

    # This is the real crash window: canonical fill committed, neither later
    # projection exists.
    with session_local() as session:
        assert session.query(ExecutionMetric).count() == 0
        assert session.query(Position).count() == 0

    seed = _recover(session_local, position_store)
    assert seed.execution_count == 1
    assert seed.last_execution_id == buy_exec_id
    assert seed.cash_balance == Decimal("9838.4")
    assert seed.realized_pnl == Decimal("-1.6")
    assert len(seed.positions) == 1
    position = seed.positions[0]
    assert position.symbol == "BTC-USD"
    assert position.quantity == Decimal("2.00000000")
    assert position.entry_price == Decimal("100.00000000")
    assert position.current_price == Decimal("100.00000000")
    assert position.account_cost_basis == Decimal("160")
    assert position.gross_notional == Decimal("160")
    assert position.notional_currency == "EUR"

    repaired = position_store.list_positions(
        user_id="user-nl",
        broker_code="coinbase",
        account_id=101,
        environment="paper",
    )
    assert len(repaired) == 1
    assert repaired[0]["symbol"] == "BTC-USD"
    assert repaired[0]["gross_notional"] == 160.0
    with session_local() as session:
        assert session.query(ExecutionMetric).count() == 0


def test_fresh_resolver_rehydrates_broker_from_canonical_fill() -> None:
    session_local = _session_local()
    position_store = ExecutionPositionStore(session_factory=session_local)
    _append_fill(
        session_local,
        local_order_id="buy-before-resolver-restart",
        side="BUY",
        quantity=2,
        price=100,
        commission="2",
        filled_at=_BUY_FILLED_AT,
        context=_context(rate="1.25"),
    )
    resolver = BrokerResolver(
        ledger_seeder=partial(
            recover_paper_ledger,
            session_local,
            position_store,
        ),
        use_local_paper_broker=True,
    )

    broker = asyncio.run(
        resolver.get_broker(
            BrokerType.COINBASE,
            "paper",
            "coinbase-paper-101",
            user_id="user-nl",
            broker_account_id=101,
            account_currency="EUR",
            settlement_currency="USD",
        )
    )

    assert isinstance(broker, PaperBroker)
    account = asyncio.run(broker.get_account_info())
    assert account.currency == "EUR"
    assert account.available_balance + account.margin_used == pytest.approx(9838.4)
    assert account.equity == pytest.approx(9998.4)
    assert account.realized_pnl == pytest.approx(-1.6)
    assert [(position.symbol, position.quantity) for position in account.positions] == [
        ("BTC-USD", 2.0)
    ]


def test_paper_fill_is_durable_before_process_local_book_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_local = _session_local()
    position_store = ExecutionPositionStore(session_factory=session_local)
    with session_local() as session:
        signal = CanonicalSignal(
            strategy_id="strategy-1",
            instr_id=1,
            action="long",
            external_signal_id="signal:durable-paper-boundary",
            ts=_BUY_FILLED_AT,
        )
        session.add(signal)
        session.flush()
        signal_id = int(signal.signal_id)
        session.commit()

    store = CanonicalExecutionStore(session_factory=session_local)
    intent = OrderIntent(
        broker_code="coinbase",
        symbol="BTC-USD",
        side="BUY",
        quantity=1,
        order_type="market",
        currency_context=_context(rate="1.25"),
    )
    order = store.create_order(
        local_order_id="durable-paper-boundary",
        user_id="user-nl",
        account_id=101,
        broker_code="coinbase",
        execution_mode="spot",
        strategy_id="strategy-1",
        canonical_signal_id=signal_id,
        instr_id=1,
        intent=intent,
        settlement_currency="USD",
        broker_environment="paper",
    )
    breaker_key = "strategy:user-nl:strategy-1:coinbase:coinbase-paper-101"
    with session_local() as session:
        session.add(
            PendingOrder(
                order_id="pre-durable-paper-boundary",
                user_id="user-nl",
                signal_id=str(signal_id),
                instr_id=1,
                broker_account_id=101,
                canonical_order_id=order.order_id,
                symbol="BTC-USD",
                settlement_currency="USD",
                side="BUY",
                order_type="market",
                quantity=Decimal("1"),
                execution_mode="spot",
                broker="coinbase",
                broker_environment="paper",
                credential_ref="coinbase-paper-101",
                breaker_key=breaker_key,
                status="pending",
                strategy_id="strategy-1",
            )
        )
        session.commit()
    paper = PaperBroker(
        account_id="101",
        starting_balance=Decimal("10000"),
        available_cash=Decimal("10000"),
        currency="EUR",
        slippage_pct=0,
        commission_pct=0.01,
        durable_fill_committer=store.apply_broker_result,
    )
    paper.set_currency_context("BTC-USD", _context(rate="1.25"))
    paper.set_price("BTC-USD", 100)
    paper.bind_canonical_order(intent, order_id=order.order_id, instrument_id=1)

    def fail_after_durable_commit(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("process stopped after durable fill")

    monkeypatch.setattr(paper, "_update_position", fail_after_durable_commit)
    with pytest.raises(RuntimeError, match="process stopped after durable fill"):
        asyncio.run(paper.submit_order(intent))

    # The process-local trade list never advanced, but the exact fill did.
    assert paper.get_trade_history() == []
    with session_local() as session:
        persisted_order = session.get(Order, order.order_id)
        assert persisted_order is not None
        assert persisted_order.state == "filled"
        assert persisted_order.broker_order_ref
        assert len(persisted_order.broker_order_ref) == 12

    with pytest.raises(CanonicalFillConflictError, match="already filled"):
        store.apply_broker_result(
            order_id=order.order_id,
            result=BrokerOrderResult.rejected("post-commit process failure"),
            fills=[],
            instr_id=1,
        )

    # A fresh resolver reconstructs the lost process state from canonical truth.
    resolver = BrokerResolver(
        ledger_seeder=partial(
            recover_paper_ledger,
            session_local,
            position_store,
        ),
        use_local_paper_broker=True,
        paper_fill_committer=store.apply_broker_result,
    )
    restarted = asyncio.run(
        resolver.get_broker(
            BrokerType.COINBASE,
            "paper",
            "coinbase-paper-101",
            user_id="user-nl",
            broker_account_id=101,
            account_currency="EUR",
            settlement_currency="USD",
        )
    )
    assert isinstance(restarted, PaperBroker)
    account = asyncio.run(restarted.get_account_info())
    assert account.equity == pytest.approx(9999.2)
    assert account.realized_pnl == pytest.approx(-0.8)
    assert [(position.symbol, position.quantity) for position in account.positions] == [
        ("BTC-USD", 1.0)
    ]

    # The pending projection was committed before submission but never saw the
    # broker response. A new process has no PaperBroker trade history, so broker
    # status lookup would report "not found". Canonical fill truth must close the
    # projection as filled rather than expiring or stranding it.
    pending_repo = PendingOrderRepository(session_local)
    tracker = ReconciliationTracker(
        pending_order_repo=pending_repo,
        canonical_execution_store=store,
    )

    class _RestartedEngine:
        def __init__(self) -> None:
            self._pending_order_repo = pending_repo
            self._reconciliation_tracker = tracker
            self.health: list[tuple[bool, str | None]] = []

        def iter_reconciliation_contexts(self):  # type: ignore[no-untyped-def]
            return tracker.iter_contexts()

        async def get_reconciliation_broker(self, _context):  # type: ignore[no-untyped-def]
            return restarted

        def pending_orders_for(self, key):  # type: ignore[no-untyped-def]
            return tracker.pending_orders_for(key)

        def mark_reconciliation_health(  # type: ignore[no-untyped-def]
            self,
            *,
            healthy: bool,
            error: str | None = None,
        ) -> None:
            self.health.append((healthy, error))

        @staticmethod
        def is_broker_system_error(**_kwargs):  # type: ignore[no-untyped-def]
            return False

        @staticmethod
        def emit_alert(**_kwargs):  # type: ignore[no-untyped-def]
            return None

        @staticmethod
        def open_circuit_breaker(**_kwargs):  # type: ignore[no-untyped-def]
            msg = "Recovered durable paper fill must not open a circuit breaker"
            raise AssertionError(msg)

    restarted_engine = _RestartedEngine()
    worker = ReconciliationWorker(
        engine=restarted_engine,
        position_store=position_store,
        risk_breach_store=None,
        interval_sec=300,
    )
    asyncio.run(worker.reconcile_once())

    with session_local() as session:
        pending = session.get(PendingOrder, "pre-durable-paper-boundary")
        assert pending is not None
        assert pending.status == "filled"
        assert pending.broker_order_id == persisted_order.broker_order_ref
        assert pending.filled_quantity == Decimal("1.00000000")
        assert pending.filled_price == Decimal("100.00000000")
        assert pending.commission == Decimal("1.00000000")
    assert restarted_engine.health == [(True, None)]


def test_restart_after_close_prunes_stale_position_without_synthesizing_metrics() -> None:
    session_local = _session_local()
    position_store = ExecutionPositionStore(session_factory=session_local)
    _append_fill(
        session_local,
        local_order_id="buy-before-crash",
        side="BUY",
        quantity=2,
        price=100,
        commission="2",
        filled_at=_BUY_FILLED_AT,
        context=_context(rate="1.25"),
    )
    first = _recover(session_local, position_store)
    assert len(first.positions) == 1

    close_exec_id = _append_fill(
        session_local,
        local_order_id="sell-then-crash",
        side="SELL",
        quantity=2,
        price=125,
        commission="2.5",
        filled_at=_SELL_FILLED_AT,
        context=_context(rate="1.20", requested_at=_SELL_FILLED_AT),
    )
    # Simulate process death before ExecutionPersistence reaches metrics/positions.
    assert position_store.list_positions(
        user_id="user-nl",
        broker_code="coinbase",
        account_id=101,
        environment="paper",
    )

    recovered = _recover(session_local, position_store)
    assert recovered.last_execution_id == close_exec_id
    assert recovered.positions == ()
    assert recovered.cash_balance == Decimal("10044.65")
    assert recovered.realized_pnl == pytest.approx(Decimal("44.65"))
    assert (
        position_store.list_positions(
            user_id="user-nl",
            broker_code="coinbase",
            account_id=101,
            environment="paper",
        )
        == []
    )
    with session_local() as session:
        assert session.query(ExecutionMetric).count() == 0

    # Recovery is a projection rebuild, not another economic event.
    repeated = _recover(session_local, position_store)
    assert repeated == recovered
    with session_local() as session:
        assert session.query(ExecutionMetric).count() == 0


def test_recovery_rejects_late_mutation_of_a_persisted_venue_fill() -> None:
    session_local = _session_local()
    position_store = ExecutionPositionStore(session_factory=session_local)
    _append_fill(
        session_local,
        local_order_id="buy-before-late-fee",
        side="BUY",
        quantity=1,
        price=100,
        commission="1",
        filled_at=_BUY_FILLED_AT,
        context=_context(rate="1.25"),
    )
    with session_local() as session:
        order_id = int(
            session.query(Order.order_id)
            .filter(Order.broker_order_ref == "broker:buy-before-late-fee")
            .scalar()
        )
    with pytest.raises(CanonicalFillConflictError, match="changed after persistence"):
        CanonicalExecutionStore(session_factory=session_local).apply_broker_result(
            order_id=order_id,
            result=BrokerOrderResult(
                success=True,
                order_id="broker:buy-before-late-fee",
                status=OrderStatus.FILLED,
                filled_quantity=1,
                filled_price=100,
                commission=Decimal("1.5"),
                commission_currency="USD",
                timestamp=_SELL_FILLED_AT,
            ),
            fills=[
                _broker_fill(
                    local_order_id="buy-before-late-fee",
                    side="BUY",
                    quantity=1,
                    price=100,
                    commission="1.5",
                    filled_at=_BUY_FILLED_AT,
                )
            ],
            instr_id=1,
        )

    recovered = _recover(session_local, position_store)
    assert recovered.execution_count == 1
    assert recovered.cash_balance == Decimal("9919.2")
    assert recovered.realized_pnl == Decimal("-0.8")
    assert len(recovered.positions) == 1
    assert recovered.positions[0].account_cost_basis == Decimal("80")
    with session_local() as session:
        assert session.query(ExecutionMetric).count() == 0


def test_recovery_reconstructs_multi_leg_option_book_from_persisted_legs() -> None:
    session_local = _session_local()
    with session_local() as session:
        signal = CanonicalSignal(
            strategy_id="strategy-1",
            instr_id=1,
            action="long",
            external_signal_id="signal:option-spread",
            ts=_BUY_FILLED_AT,
        )
        session.add(signal)
        session.flush()
        signal_id = int(signal.signal_id)
        session.commit()
    store = CanonicalExecutionStore(session_factory=session_local)
    order = store.create_order(
        local_order_id="option-spread",
        user_id="user-nl",
        account_id=101,
        broker_code="coinbase",
        execution_mode="options_spread",
        strategy_id="strategy-1",
        canonical_signal_id=signal_id,
        instr_id=1,
        intent=OptionsIntent(
            broker_code="coinbase",
            symbol="BTC-USD",
            side="BUY",
            quantity=1,
            order_type="market",
            metadata={"contract_multiplier": 100},
            currency_context=_context(rate="1.25"),
            legs=[
                OptionsLeg(
                    option_type="CALL",
                    strike=100,
                    expiry="2026-08-28",
                    side="BUY",
                    quantity=1,
                    premium=5,
                ),
                OptionsLeg(
                    option_type="CALL",
                    strike=110,
                    expiry="2026-08-28",
                    side="SELL",
                    quantity=1,
                    premium=2,
                ),
            ],
        ),
        settlement_currency="USD",
        broker_environment="paper",
    )
    store.apply_broker_result(
        order_id=order.order_id,
        result=BrokerOrderResult(
            success=True,
            order_id="broker:option-spread",
            status=OrderStatus.FILLED,
            filled_quantity=1,
            filled_price=3,
            commission=Decimal("0.7"),
            commission_currency="USD",
            timestamp=_BUY_FILLED_AT,
        ),
        fills=[
            _broker_fill(
                local_order_id="option-spread",
                side="BUY",
                quantity=1,
                price=3,
                commission="0.7",
                filled_at=_BUY_FILLED_AT,
            )
        ],
        instr_id=1,
    )

    recovered = recover_paper_ledger(
        session_local,
        None,
        user_id="user-nl",
        broker_code="coinbase",
        environment="paper",
        account_id=101,
        account_currency="EUR",
        configured_equity=None,
        configured_cash=None,
    )
    assert recovered.cash_balance == Decimal("9759.44")
    assert recovered.realized_pnl == Decimal("-0.56")
    assert recovered.current_equity == Decimal("9999.44")
    assert [
        (
            position.symbol,
            position.side,
            position.quantity,
            position.account_cost_basis,
        )
        for position in recovered.positions
    ] == [
        (
            "BTC-USD:2026-08-28:100:CALL",
            "LONG",
            Decimal("1"),
            Decimal("400"),
        ),
        (
            "BTC-USD:2026-08-28:110:CALL",
            "SHORT",
            Decimal("1"),
            Decimal("160"),
        ),
    ]


def test_recovery_isolates_user_and_account_partitions() -> None:
    session_local = _session_local()
    position_store = ExecutionPositionStore(session_factory=session_local)
    other_exec_id = _append_fill(
        session_local,
        local_order_id="other-account",
        side="BUY",
        quantity=1,
        price=100,
        commission="1",
        filled_at=_BUY_FILLED_AT,
        account_id=102,
        context=_context(rate="1.25"),
    )

    first = _recover(session_local, position_store)
    assert first.execution_count == 0
    assert first.last_execution_id is None
    assert first.cash_balance == Decimal("10000")
    assert first.positions == ()

    second = _recover(session_local, position_store, account_id=102)
    assert second.execution_count == 1
    assert second.last_execution_id == other_exec_id
    assert second.cash_balance == Decimal("19919.2")
    assert len(second.positions) == 1


def test_missing_persisted_fx_provenance_fails_closed_without_projection_mutation() -> None:
    session_local = _session_local()
    position_store = ExecutionPositionStore(session_factory=session_local)
    _append_fill(
        session_local,
        local_order_id="missing-fx",
        side="BUY",
        quantity=1,
        price=100,
        commission="1",
        filled_at=_BUY_FILLED_AT,
        context=None,
    )

    with pytest.raises(PaperLedgerRecoveryError, match="omitted persisted FX provenance"):
        _recover(session_local, position_store)
    with session_local() as session:
        assert session.query(Position).count() == 0
        assert session.query(ExecutionMetric).count() == 0


@pytest.mark.parametrize(
    ("execution_mode", "canonical_method"),
    [("futures", "FUTURES"), ("perpetual", "PERP")],
)
def test_contract_restart_replays_exact_linear_futures_economics(
    execution_mode: str,
    canonical_method: str,
) -> None:
    session_local = _session_local()
    position_store = ExecutionPositionStore(session_factory=session_local)
    exec_id = _append_contract_fill(
        session_local,
        execution_mode=execution_mode,
    )

    with session_local() as session:
        canonical_intent = session.query(CanonicalOrderIntent).one()
        assert canonical_intent.method == canonical_method
        metadata = canonical_intent.payload["metadata"]
        assert metadata["notional_value"] == 1000.0
        assert metadata["margin_required"] == 500.0
        assert "contract_size" not in metadata
        assert metadata["contract_multiplier"] == 5
        assert metadata["contract_value_model"] == "linear"

    recovered = _recover(session_local, position_store)
    assert recovered.execution_count == 1
    assert recovered.last_execution_id == exec_id
    assert recovered.cash_balance == Decimal("9999.2")
    assert recovered.realized_pnl == Decimal("-0.8")
    assert recovered.current_equity == Decimal("9999.2")
    assert len(recovered.positions) == 1
    position = recovered.positions[0]
    assert position.quantity == Decimal("2.00000000")
    assert position.quantity_unit == "contracts"
    assert position.contract_multiplier == Decimal("5")
    assert position.leverage == Decimal("2")
    assert position.contract_type == ("perpetual" if execution_mode == "perpetual" else "dated")
    assert position.account_cost_basis == Decimal("808")
    assert position.gross_notional == Decimal("808")

    # Resolver startup is the production restart boundary. It remains
    # contract-valued and never reinterprets quantity as spot assets.
    resolver = BrokerResolver(
        ledger_seeder=partial(
            recover_paper_ledger,
            session_local,
            position_store,
        ),
        use_local_paper_broker=True,
    )
    broker = asyncio.run(
        resolver.get_broker(
            BrokerType.COINBASE,
            "paper",
            "coinbase-paper-101",
            user_id="user-nl",
            broker_account_id=101,
            account_currency="EUR",
            settlement_currency="USD",
        )
    )
    assert isinstance(broker, PaperBroker)
    account = asyncio.run(broker.get_account_info())
    assert account.equity == pytest.approx(9999.2)
    assert account.margin_used == pytest.approx(404)
    assert account.available_balance == pytest.approx(9595.2)
    assert account.positions[0].quantity_unit == "contracts"
    assert account.positions[0].contract_multiplier == 5
    assert account.positions[0].leverage == 2
    with session_local() as session:
        persisted = session.query(Position).one()
        assert persisted.quantity_unit == "contracts"
        assert persisted.contract_multiplier == Decimal("5")
        assert persisted.gross_notional == Decimal("808")
        assert session.query(ExecutionMetric).count() == 0


@pytest.mark.parametrize("execution_mode", ["futures", "perpetual"])
def test_contract_restart_matches_cross_currency_partial_and_full_close(
    execution_mode: str,
) -> None:
    session_local = _session_local()
    position_store = ExecutionPositionStore(session_factory=session_local)
    _append_contract_fill(
        session_local,
        execution_mode=execution_mode,
        local_order_id=f"{execution_mode}-open",
        side="BUY",
        quantity=2,
        reference_price=100,
        fill_price=100,
        commission="10",
        rate="1.25",
        filled_at=_BUY_FILLED_AT,
    )
    partial_exec_id = _append_contract_fill(
        session_local,
        execution_mode=execution_mode,
        local_order_id=f"{execution_mode}-partial-close",
        side="SELL",
        quantity=1,
        reference_price=110,
        fill_price=110,
        commission="5.5",
        rate="1.1",
        filled_at=_SELL_FILLED_AT,
        reduce_only=True,
    )

    partial = _recover(session_local, position_store)
    assert partial.last_execution_id == partial_exec_id
    assert partial.cash_balance == Decimal("10087")
    assert partial.realized_pnl == Decimal("87")
    assert partial.current_equity == Decimal("10187")
    assert len(partial.positions) == 1
    open_position = partial.positions[0]
    assert open_position.quantity == Decimal("1.00000000")
    assert open_position.account_cost_basis == Decimal("400")
    assert open_position.gross_notional == Decimal("500")
    assert open_position.quantity_unit == "contracts"
    assert open_position.contract_multiplier == Decimal("5")
    assert open_position.leverage == Decimal("2")
    with session_local() as session:
        projected = session.query(Position).one()
        assert projected.quantity_unit == "contracts"
        assert projected.contract_multiplier == Decimal("5")
        assert projected.gross_notional == Decimal("500")

    full_exec_id = _append_contract_fill(
        session_local,
        execution_mode=execution_mode,
        local_order_id=f"{execution_mode}-full-close",
        side="SELL",
        quantity=1,
        reference_price=110,
        fill_price=110,
        commission="5.5",
        rate="1.1",
        filled_at=_SELL_FILLED_AT.replace(minute=1),
        reduce_only=True,
    )
    closed = _recover(session_local, position_store)
    assert closed.last_execution_id == full_exec_id
    assert closed.cash_balance == Decimal("10182")
    assert closed.realized_pnl == Decimal("182")
    assert closed.current_equity == Decimal("10182")
    assert closed.positions == ()
    with session_local() as session:
        assert session.query(Position).count() == 0
        assert session.query(ExecutionMetric).count() == 0


@pytest.mark.parametrize("execution_mode", ["futures", "perpetual"])
def test_contract_restart_rejects_missing_multiplier_without_projection_mutation(
    execution_mode: str,
) -> None:
    session_local = _session_local()
    position_store = ExecutionPositionStore(session_factory=session_local)
    exec_id = _append_contract_fill(
        session_local,
        execution_mode=execution_mode,
    )
    with session_local() as session:
        canonical_intent = session.query(CanonicalOrderIntent).one()
        payload = dict(canonical_intent.payload)
        metadata = dict(payload["metadata"])
        metadata.pop("contract_multiplier")
        payload["metadata"] = metadata
        canonical_intent.payload = payload
        session.commit()

    with pytest.raises(
        PaperLedgerRecoveryError,
        match=rf"execution {exec_id} contract_multiplier must be explicitly persisted",
    ):
        _recover(session_local, position_store)
    with session_local() as session:
        assert session.query(Position).count() == 0
        assert session.query(ExecutionMetric).count() == 0
