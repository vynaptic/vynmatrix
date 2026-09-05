from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from execution_engine.brokers.base import AccountInfo, BrokerOrderResult, OrderStatus
from execution_engine.config import AccountState, BrokerType
from execution_engine.engine import ExecutionEngine
from execution_engine.execution_log_store import ExecutionLogStore
from execution_engine.execution_metrics_store import ExecutionMetricsStore
from execution_engine.execution_persistence import ExecutionPersistence
from execution_engine.execution_position_store import ExecutionPositionStore
from execution_engine.execution_result import ExecutionResult
from execution_engine.models import OrderIntent
from execution_engine.order_builder import OrderBuilder
from execution_engine.pending_orders import (
    PendingOrderPersistenceError,
    PendingOrderRepository,
)
from execution_engine.risk_breach_store import RiskBreachStore
from lib_application.db.models import (
    Base,
    Broker,
)
from lib_application.db.models import ExecutionLog as ExecutionLogModel
from lib_application.db.models import (
    ExecutionMetric,
    Instrument,
    InstrumentPrice,
    LinkedBrokerAccount,
    PendingOrder,
    Position,
    RiskBreach,
    RiskMandate,
    User,
)
from lib_strategy.signals.signal import Signal, SignalAction


class _FakeBroker:
    async def get_account_info(self) -> AccountInfo:
        return AccountInfo(
            broker=BrokerType.PAPER,
            account_id="paper-1",
            equity=10_000.0,
            available_balance=9_500.0,
            margin_used=0.0,
            unrealized_pnl=0.0,
            realized_pnl=125.0,
            positions=[],
        )


def test_metric_position_state_rejects_unknown_position_side() -> None:
    account_state = AccountState(
        broker=BrokerType.PAPER,
        equity=10_000.0,
        available_cash=10_000.0,
        positions=[
            {
                "symbol": "BTCUSD",
                "side": "mystery",
                "quantity": 1.0,
                "entry_price": 100.0,
                "current_price": 101.0,
            }
        ],
    )

    with pytest.raises(ValueError, match="Unknown position side"):
        ExecutionPersistence._build_metric_position_state(
            account_state=account_state,
            symbol="BTCUSD",
        )


class _FakeExecutionLogStore:
    def __init__(self, count: int) -> None:
        self.count = count
        self.calls: list[str] = []

    def count_daily_trades(self, *, user_id: str) -> int:
        self.calls.append(user_id)
        return self.count


class _RecordingExecutionLogStore:
    def __init__(self, log_id: str = "execution-log-1") -> None:
        self.log_id = log_id
        self.calls: list[dict[str, object]] = []

    def log(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return self.log_id


class _ReturningExecutionPersistence:
    def __init__(self, log_id: str) -> None:
        self.log_id = log_id

    def persist(self, **_kwargs: object) -> str:
        return self.log_id


class _RecordingDeduplicator:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def mark_executed(self, _key: str, **kwargs: object) -> None:
        self.calls.append(kwargs)


def test_execution_persistence_preserves_signed_rebate_currency() -> None:
    result = ExecutionResult(
        success=True,
        signal_id="sig-rebate",
        symbol="BTCUSD",
        execution_mode="futures",
        broker="delta",
        orders_submitted=1,
        orders_filled=1,
        total_quantity=1.0,
        average_price=100.0,
        total_commission=-0.25,
        order_results=[
            {
                "order_id": "delta-order-1",
                "status": "filled",
                "filled_quantity": 1.0,
                "average_price": 100.0,
                "commission": -0.25,
                "commission_currency": "usdt",
                "error": None,
            }
        ],
    )

    assert ExecutionPersistence._commission_currency(result) == "USDT"


class _FakeRiskBreachStore:
    def __init__(self) -> None:
        self.loaded = [
            {
                "breaker_key": "broker:coinbase:paper",
                "scope": "broker",
                "state": "open",
                "open_until": datetime.now(tz=UTC) + timedelta(minutes=5),
                "failure_count": 3,
                "last_reason": "service unavailable",
            }
        ]

    def load_active_circuit_breakers(self):  # type: ignore[no-untyped-def]
        return list(self.loaded)


def _session_local():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_execution_log_store_counts_only_actual_submitted_trades() -> None:
    session_local = _session_local()
    store = ExecutionLogStore.__new__(ExecutionLogStore)
    store._session_factory = session_local  # type: ignore[attr-defined]

    now = datetime(2026, 3, 8, 12, 0, tzinfo=UTC)
    yesterday = now - timedelta(days=1)

    with session_local() as session:
        session.add_all(
            [
                ExecutionLogModel(
                    log_id="log-1",
                    user_id="user-1",
                    account_id=1,
                    strategy_id="swing_high_low_pmo_v1",
                    signal_type="long",
                    execution_mode="paper",
                    execution_details={"orders_submitted": 1},
                    status="executed",
                    created_at=now,
                ),
                ExecutionLogModel(
                    log_id="log-2",
                    user_id="user-1",
                    account_id=1,
                    strategy_id="swing_high_low_pmo_v1",
                    signal_type="long",
                    execution_mode="dedup",
                    execution_details={"orders_submitted": 0},
                    status="executed",
                    created_at=now,
                ),
                ExecutionLogModel(
                    log_id="log-3",
                    user_id="user-1",
                    account_id=1,
                    strategy_id="swing_high_low_pmo_v1",
                    signal_type="hold",
                    execution_mode="paper",
                    execution_details={"orders_submitted": 0},
                    status="executed",
                    created_at=now,
                ),
                ExecutionLogModel(
                    log_id="log-4",
                    user_id="user-1",
                    account_id=1,
                    strategy_id="swing_high_low_pmo_v1",
                    signal_type="long",
                    execution_mode="notify_only",
                    execution_details={"orders_submitted": 2},
                    status="executed",
                    created_at=now,
                ),
                ExecutionLogModel(
                    log_id="log-5",
                    user_id="user-1",
                    account_id=1,
                    strategy_id="swing_high_low_pmo_v1",
                    signal_type="long",
                    execution_mode="paper",
                    execution_details={"orders_submitted": 1},
                    status="failed",
                    created_at=now,
                ),
                ExecutionLogModel(
                    log_id="log-6",
                    user_id="user-1",
                    account_id=1,
                    strategy_id="swing_high_low_pmo_v1",
                    signal_type="long",
                    execution_mode="paper",
                    execution_details={"orders_submitted": 1},
                    status="executed",
                    created_at=yesterday,
                ),
                ExecutionLogModel(
                    log_id="log-7",
                    user_id="user-1",
                    account_id=1,
                    strategy_id="swing_high_low_pmo_v1",
                    signal_type="short",
                    execution_mode="live",
                    execution_details={"orders_submitted": 2},
                    status="executed",
                    created_at=now,
                ),
            ]
        )
        session.commit()

    assert store.count_daily_trades(user_id="user-1", as_of=now) == 2


def test_execution_persistence_returns_committed_log_id_without_metrics() -> None:
    log_store = _RecordingExecutionLogStore()
    persistence = ExecutionPersistence(execution_log_store=log_store)
    signal = Signal(
        external_signal_id="ext-sig-1",
        signal_id="signal-1",
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol="BTC-USDC",
        action=SignalAction.LONG,
        confidence=0.8,
        metadata={"canonical_signal_id": 17},
    )
    result = ExecutionResult(
        success=True,
        signal_id=signal.signal_id,
        symbol=signal.symbol,
        execution_mode="paper",
        broker="paper",
        orders_submitted=1,
        orders_filled=1,
        total_quantity=0.1,
        average_price=60_000.0,
        total_commission=1.0,
        broker_account_id=101,
    )

    risk_baseline_audit = {
        "day_start_equity": 10_000.0,
        "peak_equity": 10_000.0,
        "has_persisted_account_peak": True,
        "peak_provenance": {
            "user_id": "user-1",
            "broker_account_id": 101,
            "currency": "USD",
            "source": "paper_account_initial_equity",
            "source_id": "linked-broker-account:101",
            "equity": 10_000.0,
            "observed_at": "2026-08-01T10:00:00+00:00",
        },
    }
    log_id = persistence.persist(
        user_id="user-1",
        signal=signal,
        result=result,
        score_context=None,
        account_state=None,
        profile={
            "_risk_baseline_audit": risk_baseline_audit,
            "_execution_user_strategy_config": {"_causation_event_id": "evt-cause-1"},
        },
        intents=None,
    )

    assert log_id == "execution-log-1"
    assert log_store.calls[0]["account_id"] == 101
    assert log_store.calls[0]["canonical_signal_id"] == 17
    assert log_store.calls[0]["run_id"] is not None
    details = log_store.calls[0]["details"]
    assert isinstance(details, dict)
    assert "run_id" not in details
    assert "run_id" not in details["signal"]
    assert details["risk_baseline"] == risk_baseline_audit
    # The inbound command's event_id is the command -> result trace link now
    # that no execution.results event is published alongside the log row.
    assert details["causation_event_id"] == "evt-cause-1"
    assert "outbox_store" not in log_store.calls[0]
    assert "event_message" not in log_store.calls[0]


def test_engine_links_committed_execution_log_to_decision() -> None:
    engine = ExecutionEngine(order_builder=OrderBuilder())
    persistence = _ReturningExecutionPersistence("execution-log-2")
    deduplicator = _RecordingDeduplicator()
    engine._persistence = persistence  # type: ignore[assignment]
    engine._deduplicator = deduplicator  # type: ignore[assignment]
    signal = Signal(
        external_signal_id="ext-sig-2",
        signal_id="signal-2",
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol="BTC-USDC",
        action=SignalAction.LONG,
        confidence=0.8,
    )
    result = ExecutionResult(
        success=True,
        signal_id=signal.signal_id,
        symbol=signal.symbol,
        execution_mode="paper",
        broker="paper",
        orders_submitted=1,
        orders_filled=1,
        total_quantity=0.1,
        average_price=60_000.0,
        total_commission=1.0,
    )

    finalized = engine._finalize_result(
        "dedup-key-2",
        "user-1",
        signal,
        result,
        None,
        account_state=None,
        profile={},
        intents=None,
    )

    assert finalized is result
    assert deduplicator.calls[0]["result_id"] == "execution-log-2"


def test_engine_get_account_state_uses_execution_log_count() -> None:
    log_store = _FakeExecutionLogStore(count=3)
    engine = ExecutionEngine(
        order_builder=OrderBuilder(),
        execution_log_store=log_store,
    )

    state = asyncio.run(
        engine._get_account_state(
            "user-1",
            _FakeBroker(),
            {"broker": "paper"},
        )
    )

    assert state.daily_trades == 3
    assert state.realized_pnl == 125.0
    assert state.source == "broker"
    assert log_store.calls == ["user-1"]


def test_execution_metrics_store_uses_authoritative_position_snapshot() -> None:
    session_local = _session_local()
    store = ExecutionMetricsStore(session_factory=session_local)
    now = datetime(2026, 3, 8, 12, 0, tzinfo=UTC)

    first_metric_id = store.record(
        user_id="user-1",
        account_id=101,
        strategy_id="swing_high_low_pmo_v1",
        symbol="BTC-USD",
        execution_mode="spot",
        broker="coinbase",
        settlement_currency="USDT",
        signal_id="sig-1",
        run_id="run-1",
        asset_class="crypto",
        equity=100_500.0,
        available_cash=90_000.0,
        margin_used=0.0,
        unrealized_pnl=50.0,
        realized_pnl=0.0,
        position_state_override={
            "quantity": 0.14,
            "avg_price": 70_000.0,
            "current_price": 70_357.14,
            "realized_pnl": 0.0,
        },
        signal_action="long",
        trade_price=70_286.51,
        trade_qty=0.42,
        reference_price=70_000.0,
        orders_submitted=3,
        orders_filled=3,
        total_commission=29.52,
        commission_currency="USDT",
        metadata={},
        created_at=now,
    )
    second_metric_id = store.record(
        user_id="user-1",
        account_id=101,
        strategy_id="swing_high_low_pmo_v1",
        symbol="BTC-USD",
        execution_mode="blocked",
        broker="coinbase",
        settlement_currency="USDT",
        signal_id="sig-2",
        run_id="run-2",
        asset_class="crypto",
        equity=104_025.903035,
        available_cash=104_025.903035,
        margin_used=0.0,
        unrealized_pnl=0.0,
        realized_pnl=4025.903035,
        position_state_override={
            "quantity": 0.0,
            "avg_price": 0.0,
            "current_price": 0.0,
            "realized_pnl": 4025.903035,
        },
        signal_action="long",
        trade_price=None,
        trade_qty=None,
        reference_price=71_000.0,
        orders_submitted=0,
        orders_filled=0,
        total_commission=0.0,
        commission_currency=None,
        metadata={},
        created_at=now + timedelta(minutes=1),
    )
    other_account_metric_id = store.record(
        user_id="user-1",
        account_id=202,
        strategy_id="swing_high_low_pmo_v1",
        symbol="BTC-USD",
        execution_mode="spot",
        broker="coinbase",
        settlement_currency="USDT",
        signal_id="sig-other-account",
        run_id="run-other-account",
        asset_class="crypto",
        equity=1_000.0,
        available_cash=990.0,
        margin_used=0.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        position_state_override={
            "quantity": 1.0,
            "avg_price": 10.0,
            "current_price": 10.0,
            "realized_pnl": 0.0,
        },
        signal_action="long",
        trade_price=10.0,
        trade_qty=1.0,
        reference_price=10.0,
        orders_submitted=1,
        orders_filled=1,
        total_commission=0.0,
        commission_currency=None,
        metadata={},
        created_at=now + timedelta(minutes=2),
    )

    with session_local() as session:
        first = session.get(ExecutionMetric, first_metric_id)
        second = session.get(ExecutionMetric, second_metric_id)
        other_account = session.get(ExecutionMetric, other_account_metric_id)
        assert first is not None
        assert second is not None
        assert other_account is not None
        assert first.metadata_json["position_state"]["quantity"] == 0.14
        assert first.metadata_json["position_state"]["avg_price"] == 70000.0
        assert first.metadata_json["position_state"]["last_trade_turnover"] == pytest.approx(
            29_520.3342
        )
        assert first.account_id == 101
        assert first.commission_currency == "USDT"
        assert second.metadata_json["position_state"]["quantity"] == 0.0
        assert second.metadata_json["position_state"]["avg_price"] == 0.0
        assert second.metadata_json["position_state"]["realized_pnl"] == 4025.903035
        assert second.realized_pnl == Decimal("4025.903035")
        assert other_account.metadata_json["position_state"]["turnover"] == 10.0


def test_execution_metrics_store_replays_deterministic_fill_identity_once() -> None:
    session_local = _session_local()
    store = ExecutionMetricsStore(session_factory=session_local)
    metric_id = "paper-fill-exec:77"
    payload = {
        "user_id": "user-1",
        "account_id": 101,
        "strategy_id": "swing_high_low_pmo_v1",
        "symbol": "BTC-USDC",
        "execution_mode": "paper",
        "broker": "paper",
        "settlement_currency": "USDC",
        "signal_id": "signal-77",
        "run_id": "run-77",
        "asset_class": "crypto",
        "equity": 100_050.0,
        "available_cash": 100_050.0,
        "margin_used": 0.0,
        "unrealized_pnl": 0.0,
        "realized_pnl": 50.0,
        "position_state_override": {
            "quantity": 0.0,
            "avg_price": 0.0,
            "current_price": 0.0,
            "realized_pnl": 50.0,
        },
        "signal_action": "close",
        "trade_price": 70_500.0,
        "trade_qty": 0.1,
        "reference_price": 70_500.0,
        "orders_submitted": 0,
        "orders_filled": 1,
        "total_commission": 0.0,
        "commission_currency": None,
        "metadata": {"canonical_execution_id": 77},
        "created_at": datetime(2026, 3, 8, 12, 0, tzinfo=UTC),
        "metric_id": metric_id,
    }

    assert store.record(**payload) == metric_id
    assert store.record(**payload) == metric_id
    with session_local() as session:
        assert session.query(ExecutionMetric).filter_by(metric_id=metric_id).count() == 1

    conflicting = dict(payload)
    conflicting["metadata"] = {"canonical_execution_id": 78}
    with pytest.raises(ValueError, match="changed economic identity"):
        store.record(**conflicting)


def test_risk_breach_store_round_trips_active_circuit_breakers() -> None:
    session_local = _session_local()
    store = RiskBreachStore.__new__(RiskBreachStore)
    store._session_factory = session_local  # type: ignore[attr-defined]

    open_until = datetime(2026, 3, 8, 12, 5, tzinfo=UTC)
    breach_id = store.upsert_circuit_breaker_state(
        user_id="user-1",
        scope="broker",
        breaker_key="broker:coinbase:paper",
        open_until=open_until,
        failure_count=3,
        last_reason="service unavailable",
        broker="coinbase",
        environment="paper",
    )

    with session_local() as session:
        row = session.get(RiskBreach, breach_id)
        assert row is not None
        assert row.user_id == "user-1"
        assert row.broker_account_id is None
        assert row.rule_code == "circuit_breaker:broker"
        assert row.severity == "block"
        assert row.context["breaker_key"] == "broker:coinbase:paper"

    active = store.load_active_circuit_breakers(now=datetime(2026, 3, 8, 12, 0, tzinfo=UTC))
    assert active == [
        {
            "breaker_key": "broker:coinbase:paper",
            "scope": "broker",
            "state": "open",
            "open_until": open_until,
            "failure_count": 3,
            "last_reason": "service unavailable",
        }
    ]

    assert store.clear_circuit_breaker_state(breaker_key="broker:coinbase:paper") == 1
    assert store.load_active_circuit_breakers(now=datetime(2026, 3, 8, 12, 0, tzinfo=UTC)) == []


def test_risk_breach_store_enforces_relational_account_owner() -> None:
    session_local = _session_local()
    store = RiskBreachStore.__new__(RiskBreachStore)
    store._session_factory = session_local  # type: ignore[attr-defined]
    with session_local() as session:
        session.add_all(
            [
                User(user_id="user-1", email="user-1@example.invalid", base_ccy="USD"),
                User(user_id="user-2", email="user-2@example.invalid", base_ccy="USD"),
                Broker(broker_id=1, code="paper", name="Paper", capabilities={}),
            ]
        )
        session.flush()
        session.add(
            LinkedBrokerAccount(
                account_id=101,
                user_id="user-1",
                broker_id=1,
                environment="paper",
                display_name="Primary",
                base_ccy="USD",
                paper_initial_equity=100_000,
                paper_initial_cash=100_000,
            )
        )
        session.commit()

    breach_id = store.record(
        user_id="user-1",
        broker_account_id=101,
        rule_code="max_position_pct",
        context={"strategy_id": "strategy-1"},
    )
    with session_local() as session:
        row = session.get(RiskBreach, breach_id)
        assert row is not None
        assert row.user_id == "user-1"
        assert row.broker_account_id == 101
        assert row.context["broker_account_id"] == 101

    with pytest.raises(ValueError, match="is not owned by user user-2"):
        store.record(
            user_id="user-2",
            broker_account_id=101,
            rule_code="max_position_pct",
        )
    with pytest.raises(ValueError, match="context user_id conflicts"):
        store.record(
            user_id="user-1",
            rule_code="max_position_pct",
            context={"user_id": "user-2"},
        )
    with pytest.raises(ValueError, match="context broker_account_id conflicts"):
        store.record(
            user_id="user-1",
            broker_account_id=101,
            rule_code="max_position_pct",
            context={"broker_account_id": 102},
        )


def test_risk_breach_store_loads_global_then_exact_user_mandates() -> None:
    session_local = _session_local()
    store = RiskBreachStore.__new__(RiskBreachStore)
    store._session_factory = session_local  # type: ignore[attr-defined]
    with session_local() as session:
        session.add_all(
            [
                User(user_id="user-1", email="user-1@example.invalid", base_ccy="USD"),
                User(user_id="user-2", email="user-2@example.invalid", base_ccy="USD"),
            ]
        )
        session.flush()
        session.add_all(
            [
                RiskMandate(user_id=None, rules={"max_open_positions": 20}),
                RiskMandate(user_id="user-2", rules={"max_open_positions": 2}),
                RiskMandate(user_id="user-1", rules={"max_open_positions": 5}),
                RiskMandate(
                    user_id="user-1",
                    rules={"max_open_positions": 1},
                    effective_at=datetime.now(tz=UTC) + timedelta(days=1),
                ),
            ]
        )
        session.commit()

    assert store.load_mandates(user_id="user-1") == [
        {"max_open_positions": 20},
        {"max_open_positions": 5},
    ]


def test_risk_breach_store_requires_effective_exact_user_drawdown_mandate() -> None:
    session_local = _session_local()
    store = RiskBreachStore.__new__(RiskBreachStore)
    store._session_factory = session_local  # type: ignore[attr-defined]
    with session_local() as session:
        session.add_all(
            [
                User(user_id="user-1", email="user-1@example.invalid", base_ccy="USD"),
                User(user_id="user-2", email="user-2@example.invalid", base_ccy="USD"),
            ]
        )
        session.flush()
        session.add_all(
            [
                RiskMandate(user_id=None, rules={"max_drawdown_pct": 0.05}),
                RiskMandate(user_id="user-2", rules={"max_drawdown_pct": 0.05}),
                RiskMandate(
                    user_id="user-1",
                    rules={"max_drawdown_pct": 0.05},
                    effective_at=datetime.now(tz=UTC) + timedelta(days=1),
                ),
            ]
        )
        session.commit()

    assert store.has_user_drawdown_mandate(user_id="user-1") is False

    with session_local() as session:
        session.add(RiskMandate(user_id="user-1", rules={"max_drawdown_pct": 0.10}))
        session.commit()

    assert store.has_user_drawdown_mandate(user_id="user-1") is True


def test_engine_restores_active_circuit_breakers_from_store() -> None:
    engine = ExecutionEngine(
        order_builder=OrderBuilder(),
        risk_breach_store=_FakeRiskBreachStore(),
    )

    assert engine._breaker_manager.is_open("broker:coinbase:paper") is True


def test_engine_pending_orders_for_loads_recoverable_db_rows() -> None:
    session_local = _session_local()
    engine = ExecutionEngine(
        order_builder=OrderBuilder(),
        session_factory=session_local,
    )
    breaker_key = "strategy:user-1:swing_high_low_pmo_v1:coinbase:coinbase-paper"

    with session_local() as session:
        session.add_all(
            [
                PendingOrder(
                    order_id="pre-1",
                    user_id="user-1",
                    broker_account_id=1,
                    signal_id="sig-1",
                    symbol="BTC-USD",
                    settlement_currency="USD",
                    side="BUY",
                    order_type="market",
                    quantity=1,
                    execution_mode="paper",
                    broker="coinbase",
                    broker_environment="paper",
                    credential_ref="coinbase-paper",
                    breaker_key=breaker_key,
                    status="pending",
                    strategy_id="swing_high_low_pmo_v1",
                ),
                PendingOrder(
                    order_id="pre-2",
                    user_id="user-1",
                    broker_account_id=1,
                    signal_id="sig-2",
                    symbol="ETH-USD",
                    settlement_currency="USD",
                    side="SELL",
                    order_type="limit",
                    quantity=2,
                    execution_mode="paper",
                    broker="coinbase",
                    broker_environment="paper",
                    credential_ref="coinbase-paper",
                    breaker_key=breaker_key,
                    status="submitted",
                    broker_order_id="broker-2",
                    strategy_id="swing_high_low_pmo_v1",
                ),
                PendingOrder(
                    order_id="pre-3",
                    user_id="user-1",
                    broker_account_id=1,
                    signal_id="sig-3",
                    symbol="SOL-USD",
                    settlement_currency="USD",
                    side="BUY",
                    order_type="market",
                    quantity=1,
                    execution_mode="paper",
                    broker="coinbase",
                    broker_environment="paper",
                    credential_ref="coinbase-paper",
                    breaker_key=breaker_key,
                    status="filled",
                    strategy_id="swing_high_low_pmo_v1",
                ),
            ]
        )
        session.commit()

    pending = engine.pending_orders_for(breaker_key)

    assert set(pending) == {"pre-1", "broker-2"}
    assert pending["pre-1"]["status"] == "pending"
    assert pending["broker-2"]["status"] == "submitted"
    assert pending["broker-2"]["broker_order_id"] == "broker-2"
    # Shape guard: context rehydration needs user_id in every loaded payload —
    # its absence made restart rehydration silently drop all 452 rows in the
    # Jul-2 local verification.
    assert pending["pre-1"]["user_id"] == "user-1"

    # Restart scenario: a NEW engine built over the same DB (rows now present)
    # rehydrates a reconciliation context from them (restart blindness fix) —
    # the worker can reconcile without waiting for fresh traffic to
    # re-register the context.
    restarted = ExecutionEngine(
        order_builder=OrderBuilder(),
        session_factory=session_local,
    )
    contexts = {ctx["breaker_key"]: ctx for ctx in restarted.iter_reconciliation_contexts()}
    assert breaker_key in contexts
    restored = contexts[breaker_key]
    assert restored["user_id"] == "user-1"
    assert restored["strategy_id"] == "swing_high_low_pmo_v1"
    assert restored["environment"] == "paper"


def test_pending_order_repo_persists_signal_instrument_id() -> None:
    session_local = _session_local()
    repo = PendingOrderRepository(session_local)
    with session_local() as session:
        session.add(
            Instrument(
                instr_id=11,
                canonical="BTC-USDC",
                asset_class="crypto",
                settlement_currency="USDC",
            )
        )
        session.commit()

    signal = Signal(
        external_signal_id="ext-sig-3",
        signal_id="sig-instrument-1",
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol="BTC-USDC",
        instrument_id="11",
        action=SignalAction.LONG,
        confidence=0.8,
    )
    intent = OrderIntent(
        broker_code="paper",
        symbol="BTC-USDC",
        side="BUY",
        quantity=0.1,
    )

    persisted_order_id = repo.persist(
        order_id="order-instrument-1",
        user_id="user-1",
        signal=signal,
        intent=intent,
        execution_mode="paper",
        broker_code="paper",
        broker_account_id=1,
        settlement_currency="USDC",
    )

    assert persisted_order_id == "order-instrument-1"
    with session_local() as session:
        row = session.get(PendingOrder, "order-instrument-1")
        assert row is not None
        assert row.instr_id == 11


def test_pending_order_repo_pins_exact_real_source_watermark() -> None:
    session_local = _session_local()
    repo = PendingOrderRepository(session_local)
    source_ts = datetime(2026, 7, 25, 12, 14, tzinfo=UTC)
    with session_local() as session:
        session.add(
            InstrumentPrice(
                price_id=41,
                instr_id=11,
                ts=source_ts.replace(tzinfo=None),
                timeframe="1m",
                source="coinbase_live",
                content_revision=3,
                close=Decimal("118000"),
            )
        )
        session.commit()

    signal = Signal(
        external_signal_id="ext-sig-4",
        signal_id="sig-resting-1",
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol="BTC-USDC",
        instrument_id="11",
        action=SignalAction.LONG,
        confidence=0.8,
        timestamp=source_ts + timedelta(minutes=1),
    )
    intent = OrderIntent(
        broker_code="paper",
        symbol="BTC-USDC",
        side="SELL",
        quantity=Decimal("0.1"),
        order_type="stop",
        stop_price=Decimal("110000"),
        metadata={
            "market_data_source": "coinbase_live",
            "market_data_timeframe": "1m",
            "source_price_id": 41,
            "source_content_revision": 3,
            "source_price_ts": source_ts.isoformat(),
        },
    )

    repo.persist(
        order_id="order-resting-1",
        user_id="user-1",
        signal=signal,
        intent=intent,
        execution_mode="paper",
        broker_code="paper",
        broker_environment="paper",
        broker_account_id=1,
        settlement_currency="USDC",
    )

    with session_local() as session:
        row = session.get(PendingOrder, "order-resting-1")
        assert row is not None
        assert row.last_market_data_ts == source_ts.replace(tzinfo=None)
        assert row.last_market_data_revision == 3


def test_pending_order_repo_rejects_changed_source_watermark() -> None:
    session_local = _session_local()
    repo = PendingOrderRepository(session_local)
    source_ts = datetime(2026, 7, 25, 12, 14, tzinfo=UTC)
    with session_local() as session:
        session.add(
            InstrumentPrice(
                price_id=42,
                instr_id=11,
                ts=source_ts.replace(tzinfo=None),
                timeframe="1m",
                source="coinbase_live",
                content_revision=4,
                close=Decimal("118000"),
            )
        )
        session.commit()

    signal = Signal(
        external_signal_id="ext-sig-5",
        signal_id="sig-resting-conflict",
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol="BTC-USDC",
        instrument_id="11",
        action=SignalAction.LONG,
        confidence=0.8,
        timestamp=source_ts + timedelta(minutes=1),
    )
    intent = OrderIntent(
        broker_code="paper",
        symbol="BTC-USDC",
        side="SELL",
        quantity=Decimal("0.1"),
        order_type="stop",
        stop_price=Decimal("110000"),
        metadata={
            "market_data_source": "coinbase_live",
            "market_data_timeframe": "1m",
            "source_price_id": 42,
            "source_content_revision": 3,
            "source_price_ts": source_ts.isoformat(),
        },
    )

    with pytest.raises(ValueError, match="provenance changed"):
        repo.persist(
            order_id="order-resting-conflict",
            user_id="user-1",
            signal=signal,
            intent=intent,
            execution_mode="paper",
            broker_code="paper",
            broker_environment="paper",
            broker_account_id=1,
            settlement_currency="USDC",
        )


def test_pending_order_repo_mark_terminal_resolves_row_by_broker_order_id() -> None:
    # Reconciliation terminal-sync: a stranded 'submitted' row is forced terminal
    # (matched by broker_order_id OR order_id) and stops loading as recoverable.
    session_local = _session_local()
    repo = PendingOrderRepository(session_local)

    with session_local() as session:
        session.add(
            PendingOrder(
                order_id="pre-9",
                user_id="user-1",
                broker_account_id=1,
                signal_id="sig-9",
                symbol="BTC-USD",
                settlement_currency="USD",
                side="BUY",
                order_type="market",
                quantity=1,
                execution_mode="live",
                broker="coinbase",
                broker_environment="live",
                credential_ref="coinbase-live",
                breaker_key="strategy:user-1:s1:coinbase:coinbase-live",
                status="submitted",
                broker_order_id="broker-9",
                strategy_id="s1",
            )
        )
        session.commit()

    assert repo.mark_terminal(
        "broker-9",
        status="cancelled",
        reason="reconciliation terminal-sync",
    )

    with session_local() as session:
        row = session.query(PendingOrder).filter_by(order_id="pre-9").one()
        assert row.status == "cancelled"
        assert row.resolved_at is not None
        assert row.error_message == "reconciliation terminal-sync"
    assert repo.load() == {}  # no longer recoverable → no more warn-flood


def test_pending_order_repo_mark_terminal_missing_row_is_noop() -> None:
    session_local = _session_local()
    repo = PendingOrderRepository(session_local)

    assert not repo.mark_terminal("does-not-exist", status="expired")

    assert repo.load() == {}


@pytest.mark.parametrize(
    "failure",
    [SQLAlchemyError("database unavailable"), OSError("storage unavailable")],
    ids=["sqlalchemy", "os"],
)
def test_pending_order_repo_write_failures_raise(
    failure: Exception,
) -> None:
    def failing_session_factory() -> object:
        raise failure

    repo = PendingOrderRepository(failing_session_factory)
    signal = Signal(
        external_signal_id="ext-sig-6",
        signal_id="sig-write-failure",
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol="BTC-USDC",
        instrument_id="11",
        action=SignalAction.LONG,
        confidence=0.8,
    )
    intent = OrderIntent(
        broker_code="paper",
        symbol="BTC-USDC",
        side="BUY",
        quantity=0.1,
    )

    with pytest.raises(PendingOrderPersistenceError) as exc_info:
        repo.persist(
            order_id="order-write-failure",
            user_id="user-1",
            signal=signal,
            intent=intent,
            execution_mode="paper",
            broker_code="paper",
            broker_account_id=1,
            settlement_currency="USDC",
        )

    assert exc_info.value.__cause__ is failure


def test_pending_order_repo_requires_durable_adapter_for_writes() -> None:
    repo = PendingOrderRepository(None)
    signal = Signal(
        external_signal_id="ext-sig-7",
        signal_id="sig-no-adapter",
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol="BTC-USDC",
        instrument_id="11",
        action=SignalAction.LONG,
        confidence=0.8,
    )

    with pytest.raises(PendingOrderPersistenceError, match="not configured"):
        repo.persist(
            order_id="order-no-adapter",
            user_id="user-1",
            signal=signal,
            intent=OrderIntent(
                broker_code="paper",
                symbol="BTC-USDC",
                side="BUY",
                quantity=0.1,
            ),
            execution_mode="paper",
            broker_code="paper",
            broker_account_id=1,
            settlement_currency="USDC",
        )


def test_pending_order_repo_preserves_cancelled_partial_fill_economics() -> None:
    session_local = _session_local()
    repo = PendingOrderRepository(session_local)
    with session_local() as session:
        session.add(
            PendingOrder(
                order_id="pre-partial",
                user_id="user-1",
                broker_account_id=1,
                signal_id="sig-partial",
                symbol="BTC-USD",
                settlement_currency="USD",
                side="BUY",
                order_type="market",
                quantity=Decimal("1"),
                execution_mode="spot",
                broker="coinbase",
                broker_environment="live",
                status="submitted",
            )
        )
        session.commit()

    resolved_order_id = repo.resolve(
        "pre-partial",
        BrokerOrderResult(
            success=True,
            order_id="cb-partial",
            status=OrderStatus.CANCELLED,
            filled_quantity=0.25,
            filled_price=40000.0,
            commission=Decimal("2.50"),
            commission_currency="EUR",
        ),
    )

    assert resolved_order_id == "pre-partial"
    with session_local() as session:
        row = session.get(PendingOrder, "pre-partial")
        assert row is not None
        assert row.status == "cancelled"
        assert row.broker_order_id == "cb-partial"
        assert row.filled_quantity == Decimal("0.25000000")
        assert row.filled_price == Decimal("40000.00000000")
        assert row.commission == Decimal("2.50000000")
        assert row.commission_currency == "EUR"
        assert row.resolved_at is not None


def test_pending_order_repo_refuses_unconfirmed_resolution() -> None:
    repo = PendingOrderRepository(_session_local())

    with pytest.raises(PendingOrderPersistenceError, match="does not exist"):
        repo.resolve(
            "missing-order",
            BrokerOrderResult.accepted("broker-missing"),
        )


def test_position_store_rejects_unlinked_paper_route_without_auto_provision() -> None:
    session_local = _session_local()
    store = ExecutionPositionStore(session_factory=session_local)

    with session_local() as session:
        session.add(
            User(
                user_id="user-1",
                email="user-1@example.com",
                full_name="User One",
                base_ccy="USDT",
            )
        )
        broker = Broker(code="coinbase", name="Coinbase", capabilities={"assets": ["crypto"]})
        session.add(broker)
        session.flush()
        session.add(
            Instrument(
                asset_class="crypto",
                canonical="BTC-USD",
                exchange="coinbase",
                settlement_currency="USD",
            )
        )
        session.add(
            LinkedBrokerAccount(
                account_id=1,
                user_id="user-1",
                broker_id=broker.broker_id,
                environment="live",
                display_name="Binance Live",
                external_ref="BIN-LIVE-001",
                base_ccy="USDT",
                status="connected",
            )
        )
        session.commit()

    with pytest.raises(ValueError, match="Connected broker account 1 is unavailable"):
        store.sync_positions(
            user_id="user-1",
            broker_code="coinbase",
            account_id=1,
            environment="paper",
            positions=[
                {
                    "symbol": "BTC-USD",
                    "side": "long",
                    "quantity": 0.5,
                    "entry_price": 100_000.0,
                    "current_price": 101_000.0,
                    "quantity_unit": "asset",
                    "contract_multiplier": None,
                    "gross_notional": 50_500.0,
                    "notional_currency": "USDT",
                }
            ],
        )

    assert (
        store.resolve_account_id(
            user_id="user-1",
            broker_code="coinbase",
            account_id=1,
            environment="live",
        )
        == 1
    )
    for invalid_route in (
        {
            "user_id": "another-user",
            "broker_code": "coinbase",
            "account_id": 1,
            "environment": "live",
        },
        {
            "user_id": "user-1",
            "broker_code": "deribit",
            "account_id": 1,
            "environment": "live",
        },
    ):
        with pytest.raises(ValueError, match="Connected broker account 1 is unavailable"):
            store.resolve_account_id(**invalid_route)
    with pytest.raises(ValueError, match="positive integer account_id"):
        store.resolve_account_id(
            user_id="user-1",
            broker_code="coinbase",
            account_id=0,
            environment="live",
        )

    with session_local() as session:
        paper_account = (
            session.query(LinkedBrokerAccount)
            .join(Broker, LinkedBrokerAccount.broker_id == Broker.broker_id)
            .filter(
                LinkedBrokerAccount.user_id == "user-1",
                Broker.code == "coinbase",
                LinkedBrokerAccount.environment == "paper",
            )
            .first()
        )
        assert paper_account is None

        live_account = (
            session.query(LinkedBrokerAccount)
            .join(Broker, LinkedBrokerAccount.broker_id == Broker.broker_id)
            .filter(
                LinkedBrokerAccount.user_id == "user-1",
                Broker.code == "coinbase",
                LinkedBrokerAccount.environment == "live",
            )
            .first()
        )
        assert live_account is not None

        live_positions = session.query(Position).filter_by(account_id=live_account.account_id).all()
        assert live_positions == []


def test_position_store_preserves_canonical_short_position() -> None:
    session_local = _session_local()
    store = ExecutionPositionStore(session_factory=session_local)

    with session_local() as session:
        session.add(User(user_id="user-1", email="u1@example.com", full_name="U1", base_ccy="USDT"))
        broker = Broker(code="coinbase", name="Coinbase", capabilities={"assets": ["crypto"]})
        session.add(broker)
        session.flush()
        session.add(
            Instrument(
                asset_class="crypto",
                canonical="BTC-USD",
                exchange="coinbase",
                settlement_currency="USD",
            )
        )
        session.add(
            LinkedBrokerAccount(
                account_id=1,
                user_id="user-1",
                broker_id=broker.broker_id,
                environment="live",
                display_name="Binance Live",
                external_ref="BIN-LIVE-001",
                base_ccy="USDT",
                status="connected",
            )
        )
        session.commit()

    store.sync_positions(
        user_id="user-1",
        broker_code="coinbase",
        account_id=1,
        environment="live",
        positions=[
            {
                "symbol": "BTC-USD",
                "side": "short",
                "quantity": 24.4,
                "entry_price": 100.0,
                "current_price": 100.0,
                "quantity_unit": "asset",
                "contract_multiplier": None,
                "gross_notional": 2440.0,
                "notional_currency": "USDT",
            }
        ],
    )

    with session_local() as session:
        short_positions = session.query(Position).filter(Position.qty < 0).all()
        assert len(short_positions) == 1
        assert short_positions[0].qty == Decimal("-24.40000000")
        assert short_positions[0].gross_notional == Decimal("2440.00000000")


def test_pending_order_signal_identity_is_the_canonical_external_id() -> None:
    """The durable pending row must store external_signal_id — the restart
    drain guard compares it against the canonical projection chain, and the
    per-runtime Signal.signal_id never appears in canonical persistence (the
    old fallback crashlooped the engine on the first delayed-fill restart)."""
    session_local = _session_local()
    repo = PendingOrderRepository(session_local)

    signal = Signal(
        external_signal_id="ext-canonical-1",
        signal_id="runtime-uuid-1",
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol="BTC-USDC",
        action=SignalAction.LONG,
        confidence=0.8,
    )
    intent = OrderIntent(
        broker_code="paper",
        symbol="BTC-USDC",
        side="BUY",
        quantity=0.1,
    )

    repo.persist(
        order_id="order-identity-1",
        user_id="user-1",
        signal=signal,
        intent=intent,
        execution_mode="paper",
        broker_code="paper",
        broker_account_id=1,
        settlement_currency="USDC",
    )
    with session_local() as session:
        row = session.get(PendingOrder, "order-identity-1")
        assert row is not None
        assert row.signal_id == "ext-canonical-1"

    stripped = replace(signal, external_signal_id=None)
    with pytest.raises(ValueError, match="external_signal_id"):
        repo.persist(
            order_id="order-identity-2",
            user_id="user-1",
            signal=stripped,
            intent=intent,
            execution_mode="paper",
            broker_code="paper",
            broker_account_id=1,
            settlement_currency="USDC",
        )
