from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from execution_engine.brokers.base import BrokerOrderResult
from execution_engine.brokers.paper import PaperBroker
from execution_engine.canonical_execution_store import CanonicalExecutionStore
from execution_engine.config import AccountState
from execution_engine.execution_metrics_store import ExecutionMetricsStore
from execution_engine.execution_persistence import ExecutionPersistence
from execution_engine.metrics.pnl_service import PnLService
from execution_engine.models import (
    HISTORICAL_REPLAY_FILL_POLICY,
    OrderCurrencyContext,
    OrderIntent,
)
from execution_engine.paper_order_lifecycle import (
    CommittedCandle,
    PaperOrderLifecycleWorker,
    evaluate_trigger,
)
from lib_application.db.models import (
    Base,
    Broker,
    CanonicalSignal,
    Execution,
    ExecutionMetric,
    Instrument,
    InstrumentPrice,
    LinkedBrokerAccount,
    OutboxEvent,
    PendingOrder,
    Strategy,
    User,
)

_BAR_TS = datetime(2026, 7, 25, 12, 1, tzinfo=UTC)


def _candle(
    *,
    open_price: str = "118000",
    high: str = "119000",
    low: str = "116500",
    close: str = "118400",
    volume: str = "8",
    price_id: int = 9001,
) -> CommittedCandle:
    return CommittedCandle(
        price_id=price_id,
        instr_id=1,
        ts=_BAR_TS,
        timeframe="1m",
        source="coinbase_live",
        content_revision=1,
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


def test_worker_retries_known_database_failure() -> None:
    worker = PaperOrderLifecycleWorker(
        engine=object(),
        session_factory=lambda: None,
        interval_sec=0.001,
    )
    attempts = 0

    async def _process_once() -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SQLAlchemyError("database temporarily unavailable")
        await worker.stop()
        return 0

    worker.process_once = _process_once  # type: ignore[method-assign]

    asyncio.run(worker.run())

    assert attempts == 2


def test_worker_does_not_hide_unexpected_runtime_defect() -> None:
    worker = PaperOrderLifecycleWorker(
        engine=object(),
        session_factory=lambda: None,
        interval_sec=0.001,
    )

    async def _process_once() -> int:
        raise RuntimeError("unexpected lifecycle defect")

    worker.process_once = _process_once  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="unexpected lifecycle defect"):
        asyncio.run(worker.run())


def test_stop_cross_uses_stop_reference() -> None:
    decision = evaluate_trigger(
        side="SELL",
        order_type="stop",
        trigger_price=Decimal("117000"),
        limit_price=None,
        candle=_candle(),
    )

    assert decision is not None
    assert decision.reason == "stop_cross"
    assert decision.reference_price == Decimal("117000")


def test_target_cross_uses_limit_reference() -> None:
    decision = evaluate_trigger(
        side="SELL",
        order_type="limit",
        trigger_price=None,
        limit_price=Decimal("118750"),
        candle=_candle(),
    )

    assert decision is not None
    assert decision.reason == "limit_cross"
    assert decision.reference_price == Decimal("118750")
    assert decision.apply_market_slippage is False


def test_same_bar_bracket_chooses_adverse_stop() -> None:
    decision = evaluate_trigger(
        side="SELL",
        order_type="bracket",
        trigger_price=Decimal("117000"),
        limit_price=Decimal("118750"),
        candle=_candle(),
    )

    assert decision is not None
    assert decision.reason == "stop_cross"
    assert decision.reference_price == Decimal("117000")


def test_gap_through_stop_uses_first_available_open() -> None:
    decision = evaluate_trigger(
        side="SELL",
        order_type="stop",
        trigger_price=Decimal("117000"),
        limit_price=None,
        candle=_candle(
            open_price="116250",
            high="117500",
            low="115900",
            close="116800",
        ),
    )

    assert decision is not None
    assert decision.reason == "stop_gap"
    assert decision.reference_price == Decimal("116250")


class _Engine:
    def __init__(self, broker: PaperBroker, sessions: sessionmaker) -> None:
        self._broker = broker
        self._execution_position_store = None
        self._persistence = ExecutionPersistence(
            execution_metrics_store=ExecutionMetricsStore(session_factory=sessions),
            pnl_service=PnLService(session_factory=sessions),
            session_factory=sessions,
        )
        self.paper_order_lifecycle_health: dict[str, Any] = {
            "healthy": False,
            "error": "initial pass not complete",
            "failed_orders": 0,
        }

    async def get_reconciliation_broker(self, _context: dict[str, Any]) -> PaperBroker:
        return self._broker

    async def _get_account_state(self, *args: Any, **kwargs: Any) -> AccountState:
        profile = kwargs.get("profile") if "profile" in kwargs else args[2]
        assert isinstance(profile, dict)
        assert profile["broker"] == "paper"
        assert profile["broker_account_id"] == 101
        assert profile["broker_environment"] == "paper"
        assert profile["base_ccy"] == "USD"
        account = profile["accounts"]["101"]
        assert account["broker"] == "paper"
        assert account["environment"] == "paper"
        assert account["status"] == "connected"
        account_info = await self._broker.get_account_info()
        return AccountState(
            broker=account_info.broker,
            equity=account_info.equity,
            available_cash=account_info.available_balance,
            margin_used=account_info.margin_used,
            unrealized_pnl=account_info.unrealized_pnl,
            realized_pnl=account_info.realized_pnl,
            open_positions=len(account_info.positions),
            account_id=account_info.account_id,
            currency=account_info.currency,
            positions=[
                {
                    "symbol": position.symbol,
                    "side": position.side,
                    "quantity": position.quantity,
                    "entry_price": position.entry_price,
                    "current_price": position.current_price,
                    "quantity_unit": position.quantity_unit,
                    "contract_multiplier": position.contract_multiplier,
                    "gross_notional": position.gross_notional,
                    "notional_currency": position.notional_currency,
                }
                for position in account_info.positions
            ],
        )

    def _persist_paper_fill_projections(self, **kwargs: Any) -> str:
        return self._persistence.persist_paper_fill_projections(**kwargs)

    async def _update_daily_nav_snapshot(self, **kwargs: Any) -> None:
        await self._persistence.update_daily_nav_snapshot(**kwargs)

    def mark_paper_order_lifecycle_health(
        self,
        *,
        healthy: bool,
        error: str | None,
        failed_orders: int = 0,
    ) -> None:
        self.paper_order_lifecycle_health = {
            "healthy": healthy,
            "error": error,
            "failed_orders": failed_orders,
        }


def _currency_context(at: datetime) -> OrderCurrencyContext:
    return OrderCurrencyContext(
        account_currency="USD",
        settlement_currency="USD",
        account_to_settlement_rate=Decimal("1"),
        requested_at=at,
        observed_at=at,
        source="identity",
    )


async def _runtime() -> tuple[sessionmaker, PaperBroker, PaperOrderLifecycleWorker, int]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    created_at = _BAR_TS - timedelta(minutes=1)
    with sessions() as session:
        session.add_all(
            [
                User(user_id="user-1", email="paper@example.com", base_ccy="USD"),
                Strategy(strategy_id="swing_high_low_pmo_v1", strategy_name="Swing PMO"),
                Instrument(
                    instr_id=1,
                    asset_class="crypto",
                    canonical="BTC/USDC",
                    settlement_currency="USD",
                ),
                InstrumentPrice(
                    price_id=8999,
                    instr_id=1,
                    ts=created_at.replace(tzinfo=None),
                    timeframe="1m",
                    open=Decimal("100000"),
                    high=Decimal("100000"),
                    low=Decimal("100000"),
                    close=Decimal("100000"),
                    volume=Decimal("1"),
                    source="coinbase_live",
                    content_revision=1,
                ),
            ]
        )
        broker_row = Broker(code="paper", name="Local Paper", capabilities={})
        session.add(broker_row)
        session.flush()
        session.add(
            LinkedBrokerAccount(
                account_id=101,
                user_id="user-1",
                broker_id=broker_row.broker_id,
                environment="paper",
                display_name="BTC canary",
                base_ccy="USD",
                status="connected",
                paper_initial_equity=Decimal("250000"),
                paper_initial_cash=Decimal("250000"),
            )
        )
        session.add(
            CanonicalSignal(
                signal_id=501,
                strategy_id="swing_high_low_pmo_v1",
                instr_id=1,
                action="long",
                external_signal_id="coinbase-btc-usdc-20260725T1200Z-long",
                ts=created_at.replace(tzinfo=None),
            )
        )
        session.commit()

    store = CanonicalExecutionStore(session_factory=sessions)
    broker = PaperBroker(
        account_id="101",
        starting_balance=Decimal("250000"),
        available_cash=Decimal("250000"),
        currency="USD",
        slippage_pct=0,
        commission_pct=0,
        durable_fill_committer=store.apply_broker_result,
    )
    context = _currency_context(created_at)
    broker.set_price("BTC-USDC", 100_000)
    entry = OrderIntent(
        broker_code="paper",
        symbol="BTC-USDC",
        side="BUY",
        quantity=1,
        order_type="market",
        metadata={
            "signal_id": "coinbase-btc-usdc-20260725T1200Z-long",
            "historical_replay": True,
            "source_price_id": 8999,
            "source_content_revision": 1,
            "source_price_ts": created_at.isoformat(),
            "trigger_policy_version": HISTORICAL_REPLAY_FILL_POLICY,
        },
        currency_context=context,
    )
    entry_ref = store.create_order(
        local_order_id="local-entry-1",
        user_id="user-1",
        account_id=101,
        broker_code="paper",
        execution_mode="paper",
        strategy_id="swing_high_low_pmo_v1",
        canonical_signal_id=501,
        instr_id=1,
        intent=entry,
        settlement_currency="USD",
        idempotency_key="paper-entry-1",
        broker_environment="paper",
    )
    broker.bind_canonical_order(entry, order_id=entry_ref.order_id, instrument_id=1)
    assert (await broker.submit_order(entry)).is_filled

    resting = OrderIntent(
        broker_code="paper",
        symbol="BTC-USDC",
        side="SELL",
        quantity=1,
        order_type="bracket",
        stop_price=Decimal("117000"),
        limit_price=Decimal("120000"),
        metadata={
            "purpose": "bracket",
            "parent_signal_id": "coinbase-btc-usdc-20260725T1200Z-long",
            "reduce_only": True,
            "market_data_source": "coinbase_live",
            "market_data_timeframe": "1m",
        },
        currency_context=context,
    )
    ref = store.create_order(
        local_order_id="local-bracket-1",
        user_id="user-1",
        account_id=101,
        broker_code="paper",
        execution_mode="paper",
        strategy_id="swing_high_low_pmo_v1",
        canonical_signal_id=501,
        instr_id=1,
        intent=resting,
        settlement_currency="USD",
        idempotency_key="paper-bracket-1",
        broker_environment="paper",
    )
    store.apply_broker_result(
        order_id=ref.order_id,
        result=BrokerOrderResult.accepted("paper-resting-1"),
        fills=[],
        instr_id=1,
    )
    with sessions() as session:
        session.add(
            PendingOrder(
                order_id="local-bracket-1",
                client_order_id="coinbase-btc-usdc-20260725T1200Z-long:bracket",
                user_id="user-1",
                signal_id="coinbase-btc-usdc-20260725T1200Z-long",
                instr_id=1,
                broker_account_id=101,
                canonical_order_id=ref.order_id,
                symbol="BTC-USDC",
                settlement_currency="USD",
                side="SELL",
                order_type="bracket",
                quantity=Decimal("1"),
                trigger_price=Decimal("117000"),
                limit_price=Decimal("120000"),
                purpose="bracket",
                time_in_force="gtc",
                reduce_only=True,
                parent_order_id="coinbase-btc-usdc-20260725T1200Z-long",
                oco_group_id="coinbase-btc-usdc-20260725T1200Z-long",
                execution_mode="paper",
                broker="paper",
                broker_environment="paper",
                status="working",
                broker_order_id="paper-resting-1",
                strategy_id="swing_high_low_pmo_v1",
                created_at=created_at.replace(tzinfo=None),
                submitted_at=created_at.replace(tzinfo=None),
                market_data_source="coinbase_live",
                market_data_timeframe="1m",
            )
        )
        session.commit()
    worker = PaperOrderLifecycleWorker(
        engine=_Engine(broker, sessions),
        session_factory=sessions,
        max_volume_participation=Decimal("1"),
    )
    return sessions, broker, worker, ref.order_id


def test_partial_fill_duplicate_bar_and_restart_are_durable() -> None:
    asyncio.run(_assert_partial_fill_duplicate_bar_and_restart_are_durable())


async def _assert_partial_fill_duplicate_bar_and_restart_are_durable() -> None:
    sessions, broker, worker, canonical_order_id = await _runtime()
    with sessions() as session:
        session.add(
            InstrumentPrice(
                price_id=9001,
                instr_id=1,
                ts=_BAR_TS.replace(tzinfo=None),
                timeframe="1m",
                open=Decimal("118000"),
                high=Decimal("120500"),
                low=Decimal("116500"),
                close=Decimal("118400"),
                volume=Decimal("0.4"),
                source="coinbase_live",
                content_revision=1,
            )
        )
        session.commit()

    assert await worker.process_once() == 1
    with sessions() as session:
        pending = session.get(PendingOrder, "local-bracket-1")
        fills = session.query(Execution).filter_by(order_id=canonical_order_id).all()
        assert pending.status == "partially_filled"
        assert pending.cumulative_filled_quantity == Decimal("0.40000000")
        assert pending.pending_projection_exec_id is None
        assert len(fills) == 1
        assert fills[0].source_price_id == 9001
        outbox = session.query(OutboxEvent).one()
        assert outbox.payload["orders_filled"] == 1
        metrics = session.query(ExecutionMetric).all()
        assert len(metrics) == 1
        assert metrics[0].metric_id == f"paper-fill-exec:{fills[0].exec_id}"
        assert metrics[0].orders_filled == 1
        assert metrics[0].realized_pnl == Decimal("6800.00000000")
        assert metrics[0].created_at.replace(tzinfo=UTC) == _BAR_TS
        contributions = metrics[0].metadata_json["realized_pnl_contributions"]
        assert [item["exit_exec_id"] for item in contributions] == [fills[0].exec_id]

    assert await worker.process_once() == 0
    restarted = PaperOrderLifecycleWorker(
        engine=_Engine(broker, sessions),
        session_factory=sessions,
        max_volume_participation=Decimal("1"),
    )
    assert await restarted.process_once() == 0

    with sessions() as session:
        session.add(
            InstrumentPrice(
                price_id=9002,
                instr_id=1,
                ts=(_BAR_TS + timedelta(minutes=1)).replace(tzinfo=None),
                timeframe="1m",
                open=Decimal("116500"),
                high=Decimal("117000"),
                low=Decimal("115500"),
                close=Decimal("116000"),
                volume=Decimal("0.6"),
                source="coinbase_live",
                content_revision=1,
            )
        )
        session.commit()

    assert await restarted.process_once() == 1
    with sessions() as session:
        pending = session.get(PendingOrder, "local-bracket-1")
        fills = session.query(Execution).filter_by(order_id=canonical_order_id).all()
        assert pending.status == "filled"
        assert pending.cumulative_filled_quantity == Decimal("1.00000000")
        assert pending.pending_projection_exec_id is None
        assert len(fills) == 2
        assert session.query(OutboxEvent).count() == 2
        metrics = (
            session.query(ExecutionMetric)
            .order_by(ExecutionMetric.created_at, ExecutionMetric.metric_id)
            .all()
        )
        assert len(metrics) == 2
        assert metrics[-1].orders_filled == 1
        assert metrics[-1].realized_pnl == Decimal("16700.00000000")
        assert metrics[-1].metadata_json["position_state"]["quantity"] == 0.0
        assert [
            item["exit_exec_id"] for item in metrics[-1].metadata_json["realized_pnl_contributions"]
        ] == [fills[-1].exec_id]


def test_terminal_projection_checkpoint_drains_after_restart() -> None:
    asyncio.run(_assert_terminal_projection_checkpoint_drains_after_restart())


async def _assert_terminal_projection_checkpoint_drains_after_restart() -> None:
    sessions, broker, worker, canonical_order_id = await _runtime()
    with sessions() as session:
        session.add(
            InstrumentPrice(
                price_id=9010,
                instr_id=1,
                ts=_BAR_TS.replace(tzinfo=None),
                timeframe="1m",
                open=Decimal("118000"),
                high=Decimal("120500"),
                low=Decimal("116500"),
                close=Decimal("118400"),
                volume=Decimal("1"),
                source="coinbase_live",
                content_revision=1,
            )
        )
        session.commit()

    def _fail_projection(**_kwargs: Any) -> str:
        raise RuntimeError("projection store unavailable")

    worker._engine._persist_paper_fill_projections = _fail_projection
    with pytest.raises(RuntimeError, match="projection store unavailable"):
        await worker.process_once()

    with sessions() as session:
        pending = session.get(PendingOrder, "local-bracket-1")
        fills = session.query(Execution).filter_by(order_id=canonical_order_id).all()
        assert pending.status == "filled"
        assert pending.pending_projection_exec_id == fills[0].exec_id
        assert pending.error_message.startswith("paper_lifecycle_error:")
        assert session.query(ExecutionMetric).count() == 0

    restarted = PaperOrderLifecycleWorker(
        engine=_Engine(broker, sessions),
        session_factory=sessions,
        max_volume_participation=Decimal("1"),
    )
    assert await restarted.process_once() == 1
    with sessions() as session:
        pending = session.get(PendingOrder, "local-bracket-1")
        metric = session.query(ExecutionMetric).one()
        assert pending.pending_projection_exec_id is None
        assert pending.error_message is None
        assert metric.metric_id == f"paper-fill-exec:{fills[0].exec_id}"
        assert metric.orders_filled == 1
        assert metric.realized_pnl == Decimal("17000.00000000")

    assert await restarted.process_once() == 0
    with sessions() as session:
        assert session.query(ExecutionMetric).count() == 1


def test_malformed_order_is_isolated_and_retains_durable_failure() -> None:
    asyncio.run(_assert_malformed_order_is_isolated_and_retains_durable_failure())


async def _assert_malformed_order_is_isolated_and_retains_durable_failure() -> None:
    sessions, broker, worker, _canonical_order_id = await _runtime()
    created_at = _BAR_TS - timedelta(minutes=1)
    context = _currency_context(created_at)
    healthy_intent = OrderIntent(
        broker_code="paper",
        symbol="BTC-USDC",
        side="SELL",
        quantity=1,
        order_type="stop",
        stop_price=Decimal("117000"),
        metadata={
            "purpose": "stop_loss",
            "parent_signal_id": "coinbase-btc-usdc-20260725T1200Z-long",
            "reduce_only": True,
            "market_data_source": "coinbase_live",
            "market_data_timeframe": "1m",
        },
        currency_context=context,
    )
    store = CanonicalExecutionStore(session_factory=sessions)
    healthy_ref = store.create_order(
        local_order_id="local-stop-2",
        user_id="user-1",
        account_id=101,
        broker_code="paper",
        execution_mode="paper",
        strategy_id="swing_high_low_pmo_v1",
        canonical_signal_id=501,
        instr_id=1,
        intent=healthy_intent,
        settlement_currency="USD",
        idempotency_key="paper-stop-2",
        broker_environment="paper",
    )
    store.apply_broker_result(
        order_id=healthy_ref.order_id,
        result=BrokerOrderResult.accepted("paper-resting-2"),
        fills=[],
        instr_id=1,
    )
    with sessions() as session:
        malformed = session.get(PendingOrder, "local-bracket-1")
        assert malformed is not None
        malformed.order_type = "unsupported"
        session.add(
            PendingOrder(
                order_id="local-stop-2",
                client_order_id="coinbase-btc-usdc-20260725T1200Z-long:stop-2",
                user_id="user-1",
                signal_id="coinbase-btc-usdc-20260725T1200Z-long",
                instr_id=1,
                broker_account_id=101,
                canonical_order_id=healthy_ref.order_id,
                symbol="BTC-USDC",
                settlement_currency="USD",
                side="SELL",
                order_type="stop",
                quantity=Decimal("1"),
                trigger_price=Decimal("117000"),
                purpose="stop_loss",
                time_in_force="gtc",
                reduce_only=True,
                parent_order_id="coinbase-btc-usdc-20260725T1200Z-long",
                execution_mode="paper",
                broker="paper",
                broker_environment="paper",
                status="working",
                broker_order_id="paper-resting-2",
                strategy_id="swing_high_low_pmo_v1",
                created_at=created_at.replace(tzinfo=None),
                submitted_at=created_at.replace(tzinfo=None),
                market_data_source="coinbase_live",
                market_data_timeframe="1m",
            )
        )
        session.add(
            InstrumentPrice(
                price_id=9001,
                instr_id=1,
                ts=_BAR_TS.replace(tzinfo=None),
                timeframe="1m",
                open=Decimal("116500"),
                high=Decimal("117500"),
                low=Decimal("115500"),
                close=Decimal("116000"),
                volume=Decimal("2"),
                source="coinbase_live",
                content_revision=1,
            )
        )
        session.commit()

    assert await worker.process_once() == 1

    with sessions() as session:
        malformed = session.get(PendingOrder, "local-bracket-1")
        healthy = session.get(PendingOrder, "local-stop-2")
        fills = session.query(Execution).filter_by(order_id=healthy_ref.order_id).all()
        assert malformed is not None
        assert malformed.status == "working"
        assert (malformed.error_message or "").startswith("paper_lifecycle_error:")
        assert healthy is not None
        assert healthy.status == "filled"
        assert len(fills) == 1
    assert worker.is_healthy() is False


def test_reduce_only_order_without_position_is_cancelled_durably() -> None:
    asyncio.run(_assert_reduce_only_order_without_position_is_cancelled_durably())


async def _assert_reduce_only_order_without_position_is_cancelled_durably() -> None:
    from lib_application.db.models import Order

    sessions, _broker, _worker, canonical_order_id = await _runtime()
    # The protected long was closed elsewhere: after a restart the rehydrated
    # paper book is flat while the bracket row is still working.
    flat_broker = PaperBroker(
        account_id="101",
        starting_balance=Decimal("250000"),
        available_cash=Decimal("250000"),
        currency="USD",
        slippage_pct=0,
        commission_pct=0,
    )
    engine = _Engine(flat_broker, sessions)
    worker = PaperOrderLifecycleWorker(
        engine=engine,
        session_factory=sessions,
        max_volume_participation=Decimal("1"),
    )
    with sessions() as session:
        session.add(
            InstrumentPrice(
                price_id=9001,
                instr_id=1,
                ts=_BAR_TS.replace(tzinfo=None),
                timeframe="1m",
                open=Decimal("118000"),
                high=Decimal("119000"),
                low=Decimal("116500"),
                close=Decimal("118400"),
                volume=Decimal("8"),
                source="coinbase_live",
                content_revision=1,
            )
        )
        session.commit()

    assert await worker.process_once() == 0

    with sessions() as session:
        pending = session.get(PendingOrder, "local-bracket-1")
        assert pending.status == "cancelled"
        assert pending.resolved_at is not None
        assert "no long position" in pending.error_message
        assert session.get(Order, canonical_order_id).state == "canceled"
        assert session.query(Execution).filter_by(order_id=canonical_order_id).count() == 0
    assert engine.paper_order_lifecycle_health["healthy"] is True
    assert engine.paper_order_lifecycle_health["failed_orders"] == 0
    # A later bar never revisits the cancelled row.
    assert await worker.process_once() == 0
