"""Phase 5 go-live gate: programmatic soak acceptance checks.

Each documented criterion (DEPLOYMENT.md "Promotion acceptance criteria") is
queried from the database and must hold for the soak to pass. These tests lock
the all-green verdict and that each individual signal can fail the gate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lib_application.db.models import (
    Base,
    Broker,
    CanonicalSignal,
    DailyNav,
    Execution,
    ExecutionLog,
    Instrument,
    InstrumentPrice,
    LinkedBrokerAccount,
    Order,
    OrderIntent,
    OutboxEvent,
    Position,
    ServiceHeartbeat,
    Strategy,
    User,
)
from lib_application.services.soak_acceptance import (
    SoakThresholds,
    check_soak_acceptance,
)

NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
TH = SoakThresholds()


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _exec_log(log_id: str, status: str, canonical_signal_id: int | None) -> ExecutionLog:
    return ExecutionLog(
        log_id=log_id,
        user_id="u1",
        account_id=1,
        strategy_id="s1",
        signal_type="long",
        execution_mode="spot",
        status=status,
        canonical_signal_id=canonical_signal_id,
    )


def _seed_canonical_fill_lineage(s: Session) -> None:
    s.add_all(
        [
            User(user_id="u1", email="u1@example.invalid", base_ccy="USD"),
            Broker(broker_id=1, code="coinbase", name="Coinbase", capabilities={}),
            Instrument(
                instr_id=1,
                asset_class="crypto",
                canonical="BTC-USDC",
                settlement_currency="USDC",
            ),
            Strategy(strategy_id="s1", strategy_name="EMA Cross Scalper"),
        ]
    )
    s.flush()
    s.add(
        LinkedBrokerAccount(
            account_id=1,
            user_id="u1",
            broker_id=1,
            environment="paper",
            display_name="Coinbase paper",
            base_ccy="USD",
            paper_initial_equity=10_000,
            paper_initial_cash=10_000,
        )
    )
    s.add(
        CanonicalSignal(
            signal_id=1,
            strategy_id="s1",
            instr_id=1,
            action="long",
            external_signal_id="ext-soak-signal-1",
            ts=(NOW - timedelta(minutes=1)).replace(tzinfo=None),
        )
    )
    s.add(
        OrderIntent(
            intent_id=1,
            user_id="u1",
            account_id=1,
            strategy_id="s1",
            canonical_signal_id=1,
            side="BUY",
            execution_mode="spot",
            broker_environment="paper",
            method="SPOT",
            payload={"symbol": "BTC-USDC"},
            status="routed",
            created_at=NOW - timedelta(minutes=1),
        )
    )
    s.flush()
    s.add(
        Order(
            order_id=1,
            intent_id=1,
            broker_id=1,
            account_id=1,
            settlement_currency="USDC",
            broker_order_ref="paper-order-1",
            state="filled",
            routed_at=NOW - timedelta(minutes=1),
        )
    )
    s.flush()
    s.add(
        Execution(
            exec_id=1,
            order_id=1,
            instr_id=1,
            fill_ts=(NOW - timedelta(seconds=30)).replace(tzinfo=None),
            qty=1,
            price=30_000,
            fee_ccy="USDC",
            fee_amount=3,
            venue="coinbase",
            trade_id="paper-order-1:fill-1",
        )
    )


def _seed_all_green(s: Session) -> None:
    _seed_canonical_fill_lineage(s)
    s.add(
        ServiceHeartbeat(
            service_name="feedback_loop_engine", last_success_at=NOW - timedelta(minutes=5)
        )
    )
    # prices.ts is a naive column; store naive UTC (the checker reads naive as UTC).
    s.add(
        InstrumentPrice(
            instr_id=1, ts=(NOW - timedelta(minutes=2)).replace(tzinfo=None), close=30000
        )
    )
    s.add_all(
        [
            OutboxEvent(
                topic="execution.commands", event_type="cmd", payload={}, status="published"
            ),
            OutboxEvent(topic="execution.commands", event_type="cmd", payload={}, status="pending"),
        ]
    )
    s.add_all(
        [
            _exec_log("l1", "executed", 101),
            _exec_log("l2", "executed", 102),
            _exec_log("l3", "no_op", 103),
        ]
    )
    # A recent daily_nav row so the nav_recorded (RiskGuard baseline) check passes.
    s.add(
        DailyNav(
            user_id="u1",
            account_id=1,
            date=NOW.date(),
            nav_ccy="USD",
            nav_value=10_000,
        )
    )
    s.commit()


def _verdicts(report) -> dict[str, bool]:  # type: ignore[no-untyped-def]
    return {c.name: c.passed for c in report.checks}


def test_all_green_passes() -> None:
    with _session() as s:
        _seed_all_green(s)
        report = check_soak_acceptance(s, now=NOW, alerts_deliverable=True, thresholds=TH)
    assert report.passed
    assert all(_verdicts(report).values())


def test_nav_for_one_filled_account_does_not_certify_another() -> None:
    with _session() as s:
        _seed_all_green(s)
        s.add(
            LinkedBrokerAccount(
                account_id=2,
                user_id="u1",
                broker_id=1,
                environment="paper",
                display_name="Second Coinbase paper",
                base_ccy="USD",
                paper_initial_equity=10_000,
                paper_initial_cash=10_000,
            )
        )
        s.add(
            OrderIntent(
                intent_id=2,
                user_id="u1",
                account_id=2,
                strategy_id="s1",
                canonical_signal_id=1,
                side="BUY",
                execution_mode="spot",
                broker_environment="paper",
                method="SPOT",
                payload={"symbol": "BTC-USDC"},
                status="routed",
                created_at=NOW - timedelta(minutes=1),
            )
        )
        s.flush()
        s.add(
            Order(
                order_id=2,
                intent_id=2,
                broker_id=1,
                account_id=2,
                settlement_currency="USDC",
                broker_order_ref="paper-order-2",
                state="filled",
                routed_at=NOW - timedelta(minutes=1),
            )
        )
        s.flush()
        s.add(
            Execution(
                exec_id=2,
                order_id=2,
                instr_id=1,
                fill_ts=(NOW - timedelta(seconds=20)).replace(tzinfo=None),
                qty=1,
                price=30_100,
                fee_ccy="USDC",
                fee_amount=3,
                venue="coinbase",
                trade_id="paper-order-2:fill-1",
            )
        )
        s.commit()

        report = check_soak_acceptance(s, now=NOW, alerts_deliverable=True, thresholds=TH)

    assert _verdicts(report)["nav_recorded"] is False
    nav_check = next(check for check in report.checks if check.name == "nav_recorded")
    assert "missing=[2]" in nav_check.detail


def test_empty_db_fails_multiple_checks() -> None:
    with _session() as s:
        report = check_soak_acceptance(s, now=NOW, alerts_deliverable=False, thresholds=TH)
    v = _verdicts(report)
    assert not report.passed
    # No heartbeat, no prices, no fills, no alert sink -> those fail; backlog/dups
    # are vacuously fine on an empty DB.
    assert v["feedback_liveness"] is False
    assert v["market_data_freshness"] is False
    assert v["signal_activity"] is False
    assert v["execution_fills"] is False
    assert v["alert_sink"] is False
    assert v["outbox_backlog"] is True
    assert v["duplicate_submissions"] is True
    assert v["positions_consistency"] is True
    assert v["nav_recorded"] is False


def test_stale_feedback_heartbeat_fails() -> None:
    with _session() as s:
        _seed_all_green(s)
        hb = s.get(ServiceHeartbeat, "feedback_loop_engine")
        assert hb is not None
        hb.last_success_at = NOW - timedelta(seconds=TH.heartbeat_max_age_s + 60)
        s.commit()
        report = check_soak_acceptance(s, now=NOW, alerts_deliverable=True, thresholds=TH)
    assert _verdicts(report)["feedback_liveness"] is False
    assert not report.passed


def test_stale_market_data_fails() -> None:
    with _session() as s:
        _seed_all_green(s)
        s.add(
            InstrumentPrice(
                instr_id=1,
                ts=(NOW - timedelta(seconds=TH.market_data_max_age_s + 120)).replace(tzinfo=None),
                close=30000,
            )
        )
        # Remove the fresh row so the latest is the stale one.
        s.query(InstrumentPrice).filter(
            InstrumentPrice.ts == (NOW - timedelta(minutes=2)).replace(tzinfo=None)
        ).delete()
        s.commit()
        report = check_soak_acceptance(s, now=NOW, alerts_deliverable=True, thresholds=TH)
    assert _verdicts(report)["market_data_freshness"] is False


def test_dead_letter_fails_outbox() -> None:
    with _session() as s:
        _seed_all_green(s)
        s.add(
            OutboxEvent(
                topic="execution.commands", event_type="cmd", payload={}, status="dead_letter"
            )
        )
        s.commit()
        report = check_soak_acceptance(s, now=NOW, alerts_deliverable=True, thresholds=TH)
    assert _verdicts(report)["outbox_backlog"] is False


def test_outbox_backlog_over_ceiling_fails() -> None:
    tight = SoakThresholds(outbox_pending_max=1)
    with _session() as s:
        _seed_all_green(s)  # already 1 pending
        s.add(OutboxEvent(topic="t", event_type="cmd", payload={}, status="pending"))
        s.commit()
        report = check_soak_acceptance(s, now=NOW, alerts_deliverable=True, thresholds=tight)
    assert _verdicts(report)["outbox_backlog"] is False


def test_executed_log_without_canonical_fill_fails_execution_fills() -> None:
    with _session() as s:
        s.add(ServiceHeartbeat(service_name="feedback_loop_engine", last_success_at=NOW))
        s.add(InstrumentPrice(instr_id=1, ts=NOW.replace(tzinfo=None), close=30000))
        s.add(_exec_log("l1", "executed", 101))
        s.commit()
        report = check_soak_acceptance(s, now=NOW, alerts_deliverable=True, thresholds=TH)
    assert _verdicts(report)["execution_fills"] is False


def test_duplicate_execution_log_is_diagnostic_only() -> None:
    with _session() as s:
        _seed_all_green(s)
        # Logs are audit diagnostics; the canonical broker fill identity is the
        # duplicate-submission authority.
        s.add(_exec_log("l4", "executed", 101))
        s.commit()
        report = check_soak_acceptance(s, now=NOW, alerts_deliverable=True, thresholds=TH)
    assert _verdicts(report)["execution_fills"] is True
    assert _verdicts(report)["duplicate_submissions"] is True


def test_fill_without_signal_strategy_provenance_fails() -> None:
    with _session() as s:
        _seed_all_green(s)
        signal = s.get(CanonicalSignal, 1)
        assert signal is not None
        signal.strategy_id = "different-strategy"
        s.commit()
        report = check_soak_acceptance(s, now=NOW, alerts_deliverable=True, thresholds=TH)
    assert _verdicts(report)["execution_fills"] is False


def test_fill_without_owned_account_provenance_fails() -> None:
    with _session() as s:
        _seed_all_green(s)
        order = s.get(Order, 1)
        assert order is not None
        order.account_id = 2
        s.commit()
        report = check_soak_acceptance(s, now=NOW, alerts_deliverable=True, thresholds=TH)
    assert _verdicts(report)["execution_fills"] is False


def test_fill_without_observation_identity_is_rejected_by_schema() -> None:
    with _session() as s:
        _seed_all_green(s)
        execution = s.get(Execution, 1)
        assert execution is not None
        execution.trade_id = None
        with pytest.raises(IntegrityError):
            s.commit()


def test_missing_alert_sink_fails() -> None:
    with _session() as s:
        _seed_all_green(s)
        report = check_soak_acceptance(s, now=NOW, alerts_deliverable=False, thresholds=TH)
    assert _verdicts(report)["alert_sink"] is False
    assert not report.passed


def test_signal_stall_fails() -> None:
    with _session() as s:
        _seed_all_green(s)
        sig = s.get(CanonicalSignal, 1)
        assert sig is not None
        sig.ts = (NOW - timedelta(seconds=TH.signal_max_age_s + 120)).replace(tzinfo=None)
        s.commit()
        report = check_soak_acceptance(s, now=NOW, alerts_deliverable=True, thresholds=TH)
    assert _verdicts(report)["signal_activity"] is False
    assert not report.passed


def test_negative_position_fails_consistency() -> None:
    with _session() as s:
        _seed_all_green(s)
        # Spot is long-only — a negative-quantity position is state corruption.
        s.add(
            Position(
                account_id=1,
                instr_id=1,
                qty=-1,
                avg_price=100,
                gross_notional=100,
                notional_currency="USD",
            )
        )
        s.commit()
        report = check_soak_acceptance(s, now=NOW, alerts_deliverable=True, thresholds=TH)
    assert _verdicts(report)["positions_consistency"] is False
    assert not report.passed


def test_stale_nav_fails_nav_recorded() -> None:
    with _session() as s:
        _seed_all_green(s)
        # daily_nav older than the grace window -> the RiskGuard cap baseline is stale.
        s.add(
            DailyNav(
                user_id="u1",
                account_id=1,
                date=(NOW - timedelta(days=TH.nav_max_age_days + 2)).date(),
                nav_ccy="USD",
                nav_value=9_000,
            )
        )
        s.query(DailyNav).filter(DailyNav.date == NOW.date()).delete()
        s.commit()
        report = check_soak_acceptance(s, now=NOW, alerts_deliverable=True, thresholds=TH)
    assert _verdicts(report)["nav_recorded"] is False
