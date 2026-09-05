"""DB-2: signal_performance records did_execute so feedback is grounded in real
trading (whether the signal was actually traded), separate from the hypothetical
price-move signal-quality metric."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from feedback_loop_engine.engine import FeedbackLoopEngine
from feedback_loop_engine.evaluator import SignalEvaluator
from feedback_loop_engine.models import Direction, EvaluationHorizon, SignalEvaluation
from lib_application.db.models import (
    Base,
    Broker,
    CanonicalSignal,
    Execution,
    ExecutionDecisionLog,
    Instrument,
    LinkedBrokerAccount,
    Order,
    OrderIntent,
    SignalPerformance,
    Strategy,
    User,
)

TS = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)


def _evaluator() -> SignalEvaluator:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return SignalEvaluator(engine)


def _evaluation() -> SignalEvaluation:
    return SignalEvaluation(
        signal_id=1,
        strategy_id="s1",
        strat_ver_id=None,
        instr_id=1,
        symbol="BTCUSD",
        predicted_direction=Direction.LONG,
        confidence=0.8,
        signal_ts=TS,
        entry_price=100.0,
        evaluation_horizon=EvaluationHorizon.D1,
        evaluation_ts=TS,
        exit_price=110.0,
        actual_direction=Direction.LONG,
        price_change_pct=0.1,
        is_correct=True,
        pnl_pct=0.1,
    )


def _row(evaluator: SignalEvaluator) -> SignalPerformance:
    with Session(evaluator._engine) as session:
        return session.execute(select(SignalPerformance)).scalars().one()


def test_persist_records_did_execute_true() -> None:
    evaluator = _evaluator()
    evaluator.persist_evaluation(_evaluation(), did_execute=True)
    assert _row(evaluator).did_execute is True


def test_persist_records_did_execute_false() -> None:
    evaluator = _evaluator()
    evaluator.persist_evaluation(_evaluation(), did_execute=False)
    assert _row(evaluator).did_execute is False


def test_did_execute_defaults_to_none() -> None:
    # Unknown execution status (no link) stays NULL, distinct from False.
    evaluator = _evaluator()
    evaluator.persist_evaluation(_evaluation())
    assert _row(evaluator).did_execute is None


def test_duplicate_signal_horizon_persistence_is_a_noop() -> None:
    evaluator = _evaluator()

    first_id, first_inserted = evaluator.persist_evaluation(
        _evaluation(),
        did_execute=False,
    )
    replay_id, replay_inserted = evaluator.persist_evaluation(
        _evaluation(),
        did_execute=False,
    )

    assert replay_id == first_id
    assert first_inserted is True
    assert replay_inserted is False
    assert _row(evaluator).perf_id == first_id


def test_execution_attribution_uses_canonical_signal_not_shared_run_id() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                User(user_id="u1", email="u1@example.com", base_ccy="USD"),
                User(user_id="u2", email="u2@example.com", base_ccy="USD"),
                Broker(broker_id=1, code="paper", name="Paper"),
                Strategy(strategy_id="s1", strategy_name="S1", asset_class="crypto"),
                Instrument(
                    instr_id=1,
                    asset_class="crypto",
                    canonical="BTC-USDC",
                    settlement_currency="USDC",
                ),
                Instrument(
                    instr_id=2,
                    asset_class="crypto",
                    canonical="ETH-USDC",
                    settlement_currency="USDC",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                LinkedBrokerAccount(
                    account_id=11,
                    user_id="u1",
                    broker_id=1,
                    environment="paper",
                    display_name="U1 dedicated paper",
                    base_ccy="USD",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
                LinkedBrokerAccount(
                    account_id=22,
                    user_id="u2",
                    broker_id=1,
                    environment="paper",
                    display_name="U2 dedicated paper",
                    base_ccy="USD",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
                LinkedBrokerAccount(
                    account_id=33,
                    user_id="u2",
                    broker_id=1,
                    environment="paper",
                    display_name="U2 second dedicated paper",
                    base_ccy="USD",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
                CanonicalSignal(
                    signal_id=101,
                    strategy_id="s1",
                    instr_id=1,
                    action="long",
                    run_id="shared-run",
                    external_signal_id="btc-signal",
                    ts=TS,
                ),
                CanonicalSignal(
                    signal_id=202,
                    strategy_id="s1",
                    instr_id=2,
                    action="long",
                    run_id="shared-run",
                    external_signal_id="eth-signal",
                    ts=TS,
                ),
            ]
        )
        session.flush()
        btc_intent = OrderIntent(
            user_id="u1",
            account_id=11,
            strategy_id="s1",
            canonical_signal_id=101,
            idempotency_key="btc-intent",
            side="BUY",
            execution_mode="spot",
            broker_environment="paper",
            method="SPOT",
            payload={},
            status="routed",
        )
        eth_intent = OrderIntent(
            user_id="u2",
            account_id=22,
            strategy_id="s1",
            canonical_signal_id=202,
            idempotency_key="eth-intent",
            side="BUY",
            execution_mode="spot",
            broker_environment="paper",
            method="SPOT",
            payload={},
            status="routed",
        )
        btc_second_user_intent = OrderIntent(
            user_id="u2",
            account_id=33,
            strategy_id="s1",
            canonical_signal_id=101,
            idempotency_key="btc-second-user-intent",
            side="BUY",
            execution_mode="spot",
            broker_environment="paper",
            method="SPOT",
            payload={},
            status="routed",
        )
        session.add_all([btc_intent, eth_intent, btc_second_user_intent])
        session.flush()
        btc_order = Order(
            intent_id=btc_intent.intent_id,
            broker_id=1,
            account_id=11,
            settlement_currency="USDC",
            client_order_id="btc-order",
            state="filled",
        )
        eth_order = Order(
            intent_id=eth_intent.intent_id,
            broker_id=1,
            account_id=22,
            settlement_currency="USDC",
            client_order_id="eth-order",
            state="filled",
        )
        btc_second_user_order = Order(
            intent_id=btc_second_user_intent.intent_id,
            broker_id=1,
            account_id=33,
            settlement_currency="USDC",
            client_order_id="btc-second-user-order",
            state="filled",
        )
        session.add_all([btc_order, eth_order, btc_second_user_order])
        session.flush()
        session.add_all(
            [
                ExecutionDecisionLog(
                    decision_id=1001,
                    user_id="u1",
                    instr_id=1,
                    canonical_signal_id=101,
                    broker_account_id=11,
                    lineage_schema_version="v1",
                    should_execute=True,
                    status="executed",
                    idempotency_key="btc-decision",
                ),
                ExecutionDecisionLog(
                    decision_id=2002,
                    user_id="u2",
                    instr_id=2,
                    canonical_signal_id=202,
                    broker_account_id=22,
                    lineage_schema_version="v1",
                    should_execute=True,
                    status="executed",
                    idempotency_key="eth-decision",
                ),
                ExecutionDecisionLog(
                    decision_id=1002,
                    user_id="u2",
                    instr_id=1,
                    canonical_signal_id=101,
                    broker_account_id=33,
                    lineage_schema_version="v1",
                    should_execute=True,
                    status="executed",
                    idempotency_key="btc-second-user-decision",
                ),
                Execution(
                    order_id=btc_order.order_id,
                    instr_id=1,
                    fill_ts=TS,
                    qty=Decimal("0.01"),
                    price=Decimal("100"),
                    fee_ccy="USDC",
                    fee_amount=Decimal("0.01"),
                    venue="paper",
                    trade_id="btc-fill",
                ),
                Execution(
                    order_id=eth_order.order_id,
                    instr_id=2,
                    fill_ts=TS,
                    qty=Decimal("0.02"),
                    price=Decimal("50"),
                    fee_ccy="USDC",
                    fee_amount=Decimal("0.01"),
                    venue="paper",
                    trade_id="eth-fill",
                ),
                Execution(
                    order_id=btc_second_user_order.order_id,
                    instr_id=1,
                    fill_ts=TS,
                    qty=Decimal("0.03"),
                    price=Decimal("100"),
                    fee_ccy="USDC",
                    fee_amount=Decimal("0.01"),
                    venue="paper",
                    trade_id="btc-second-user-fill",
                ),
            ]
        )
        session.commit()

    lineage = FeedbackLoopEngine(engine)._signal_execution_lineage(101)

    assert lineage["canonical_signal_id"] == 101
    assert lineage["decision_ids"] == [1001, 1002]
    assert lineage["decisions"] == [
        {
            "decision_id": 1001,
            "user_id": "u1",
            "broker_account_id": 11,
            "binding_id": None,
            "should_execute": True,
            "status": "executed",
        },
        {
            "decision_id": 1002,
            "user_id": "u2",
            "broker_account_id": 33,
            "binding_id": None,
            "should_execute": True,
            "status": "executed",
        },
    ]
    assert lineage["account_ids"] == [11, 33]
    assert lineage["user_ids"] == ["u1", "u2"]
    assert len(lineage["intent_ids"]) == 2
    assert len(lineage["order_ids"]) == 2
    assert len(lineage["fill_ids"]) == 2
