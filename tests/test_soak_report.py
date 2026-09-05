"""Pipeline soak reconciliation report — invariant verdicts + robustness.

Seeds an in-memory schema and asserts the report's invariant sections flip
pass/fail correctly, that realized P&L takes the latest cumulative snapshot per
partition (not a sum), that missing executions are reconciled, and that a schema
behind head degrades gracefully (per-stage isolation) instead of crashing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lib_application.db.models import (
    AssetScore,
    Base,
    Broker,
    CanonicalSignal,
    Execution,
    ExecutionDecisionLog,
    ExecutionLog,
    ExecutionMetric,
    Instrument,
    LinkedBrokerAccount,
    Order,
    OrderIntent,
    OutboxEvent,
    ServiceHeartbeat,
    Strategy,
    User,
)
from lib_application.services.soak_report import build_soak_report

NOW = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)
SINCE = NOW - timedelta(hours=24)


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_clean(s: Session) -> None:
    s.add_all(
        [
            User(user_id="demo_user", email="demo_user@example.invalid", base_ccy="USD"),
            Broker(broker_id=1, code="coinbase", name="Coinbase", capabilities={}),
            Instrument(
                instr_id=1,
                asset_class="crypto",
                canonical="BTC-USDC",
                settlement_currency="USDC",
            ),
            Strategy(
                strategy_id="test_strategy_alpha_v1",
                strategy_name="EMA Cross Scalper",
            ),
        ]
    )
    s.flush()
    s.add(
        LinkedBrokerAccount(
            account_id=1,
            user_id="demo_user",
            broker_id=1,
            environment="paper",
            display_name="Coinbase paper",
            base_ccy="USD",
            paper_initial_equity=100_000,
            paper_initial_cash=100_000,
        )
    )
    # An actionable signal LINKED to an execution (canonical_signal_id) so the
    # reconciliation stage sees no orphan.
    s.add(
        CanonicalSignal(
            signal_id=1,
            strategy_id="test_strategy_alpha_v1",
            instr_id=1,
            action="long",
            external_signal_id="sig-1",
            ts=NOW.replace(tzinfo=None),
        )
    )
    s.add(
        AssetScore(
            instr_id=1,
            score_value=0.5,
            components=[],
            weights_applied={},
            external_signal_id="score-1",
            ts=NOW.replace(tzinfo=None),
        )
    )
    s.add(
        ExecutionLog(
            log_id="el-1",
            user_id="demo_user",
            account_id=1,
            strategy_id="test_strategy_alpha_v1",
            signal_type="long",
            execution_mode="spot",
            status="executed",
            created_at=NOW,
            canonical_signal_id=1,
        )
    )
    s.add(
        OrderIntent(
            intent_id=1,
            user_id="demo_user",
            account_id=1,
            strategy_id="test_strategy_alpha_v1",
            canonical_signal_id=1,
            side="BUY",
            execution_mode="spot",
            broker_environment="paper",
            method="SPOT",
            payload={"symbol": "BTC-USDC"},
            status="routed",
            created_at=NOW - timedelta(minutes=2),
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
            routed_at=NOW - timedelta(minutes=2),
        )
    )
    s.flush()
    s.add(
        Execution(
            exec_id=1,
            order_id=1,
            instr_id=1,
            fill_ts=(NOW - timedelta(minutes=1)).replace(tzinfo=None),
            qty=1,
            price=30_000,
            fee_ccy="USDC",
            fee_amount=3,
            venue="coinbase",
            trade_id="paper-order-1:fill-1",
        )
    )
    s.add(
        ServiceHeartbeat(
            service_name="feedback_loop_engine", last_success_at=NOW - timedelta(minutes=5)
        )
    )
    s.commit()


def _metric(
    metric_id: str, realized: float, created: datetime, mode: str = "spot"
) -> ExecutionMetric:
    return ExecutionMetric(
        metric_id=metric_id,
        user_id="demo_user",
        account_id=1,
        strategy_id="test_strategy_alpha_v1",
        symbol="BTCUSD",
        execution_mode=mode,
        broker="paper",
        realized_pnl=realized,
        orders_filled=1,
        created_at=created,
    )


def _section(report, name):  # type: ignore[no-untyped-def]
    return next(sec for sec in report.sections if sec.name == name)


def test_clean_soak_passes() -> None:
    with _session() as s:
        _seed_clean(s)
        report = build_soak_report(s, now=NOW, since=SINCE)
    assert report.passed
    assert not report.degraded
    assert _section(report, "schema").ok is True
    assert _section(report, "signals").ok is True
    assert _section(report, "scoring").ok is True
    assert _section(report, "executions").ok is True
    assert _section(report, "executions").rows["certified_fills"] == 1
    assert _section(report, "execution_logs").ok is None
    assert _section(report, "outbox").ok is True
    assert _section(report, "dedup").ok is True
    assert _section(report, "reconciliation").ok is True
    assert _section(report, "feedback_liveness").ok is True


def test_empty_window_fails_signals_and_executions() -> None:
    with _session() as s:
        report = build_soak_report(s, now=NOW, since=SINCE)
    assert not report.passed
    assert _section(report, "signals").ok is False
    assert _section(report, "executions").ok is False
    # No actionable signals -> no orphans -> reconciliation holds.
    assert _section(report, "reconciliation").ok is True


def test_execution_log_cannot_substitute_for_canonical_fill() -> None:
    with _session() as s:
        s.add(
            ExecutionLog(
                log_id="log-only",
                user_id="demo_user",
                account_id=1,
                strategy_id="test_strategy_alpha_v1",
                signal_type="long",
                execution_mode="spot",
                status="executed",
                created_at=NOW,
            )
        )
        s.add(
            ServiceHeartbeat(
                service_name="feedback_loop_engine",
                last_success_at=NOW,
            )
        )
        s.commit()
        report = build_soak_report(s, now=NOW, since=SINCE)
    assert _section(report, "execution_logs").rows["executed"] == 1
    assert _section(report, "executions").ok is False


def test_missing_fill_provenance_fails_execution_certification() -> None:
    with _session() as s:
        _seed_clean(s)
        signal = s.get(CanonicalSignal, 1)
        assert signal is not None
        signal.instr_id = 2
        s.commit()
        report = build_soak_report(s, now=NOW, since=SINCE)
    executions = _section(report, "executions")
    assert executions.ok is False
    assert executions.rows["provenance_failures"] == 1


def test_missing_fill_observation_identity_is_rejected_by_schema() -> None:
    with _session() as s:
        _seed_clean(s)
        execution = s.get(Execution, 1)
        assert execution is not None
        execution.trade_id = None
        with pytest.raises(IntegrityError):
            s.commit()


def test_duplicate_broker_fill_key_fails_on_drifted_schema() -> None:
    with _session() as s:
        _seed_clean(s)
        # Head enforces this identity in the database. Recreate only this table
        # without the constraint to prove the certification query still catches
        # a drifted/legacy database instead of trusting the intended schema.
        s.execute(text("CREATE TABLE executions_without_unique AS SELECT * FROM executions"))
        s.execute(text("DROP TABLE executions"))
        s.execute(text("ALTER TABLE executions_without_unique RENAME TO executions"))
        s.execute(
            text(
                """
                INSERT INTO executions (
                    exec_id, order_id, instr_id, fill_ts, qty, price,
                    fee_ccy, fee_amount, venue, trade_id
                )
                SELECT
                    2, order_id, instr_id, fill_ts, qty, price,
                    fee_ccy, fee_amount, venue, trade_id
                FROM executions
                WHERE exec_id = 1
                """
            )
        )
        s.commit()
        report = build_soak_report(s, now=NOW, since=SINCE)
    dedup = _section(report, "dedup")
    assert dedup.ok is False
    assert dedup.rows["duplicate_broker_fill_keys"] == 1


def test_negative_window_fails_fast() -> None:
    with _session() as s:
        report = build_soak_report(s, now=SINCE, since=NOW)  # now < since
    assert not report.passed
    assert _section(report, "window").ok is False
    assert len(report.sections) == 1


def test_dead_letter_in_window_fails_outbox() -> None:
    with _session() as s:
        _seed_clean(s)
        s.add(
            OutboxEvent(
                topic="execution.commands",
                event_type="cmd",
                payload={},
                status="dead_letter",
                created_at=NOW,
            )
        )
        s.commit()
        report = build_soak_report(s, now=NOW, since=SINCE)
    assert _section(report, "outbox").ok is False
    assert not report.passed


def test_missing_execution_is_reconciled() -> None:
    with _session() as s:
        _seed_clean(s)
        # A second actionable signal with NO execution_logs row -> orphan.
        s.add(
            CanonicalSignal(
                signal_id=2,
                strategy_id="test_strategy_alpha_v1",
                instr_id=1,
                action="long",
                external_signal_id="sig-2",
                ts=NOW.replace(tzinfo=None),
            )
        )
        s.commit()
        report = build_soak_report(s, now=NOW, since=SINCE)
    recon = _section(report, "reconciliation")
    assert recon.ok is False
    assert recon.rows["orphan_actionable_signals"] == 1
    assert "2" in str(recon.rows.get("orphan_signal_ids_sample", ""))
    assert not report.passed


def test_rejected_decision_is_handled_not_orphan() -> None:
    # A signal that was decided-and-rejected (policy/risk block) has no
    # execution_logs row but IS handled — it must NOT count as a missing-execution
    # orphan (the signal->decision edge held).
    with _session() as s:
        _seed_clean(s)
        s.add(
            CanonicalSignal(
                signal_id=3,
                strategy_id="test_strategy_alpha_v1",
                instr_id=1,
                action="short",
                external_signal_id="sig-3",
                run_id="run-3",
                ts=NOW.replace(tzinfo=None),
            )
        )
        s.add(
            ExecutionDecisionLog(
                idempotency_key="k-3",
                user_id="demo_user",
                signal_id="domain-signal-3",
                run_id="run-3",
                action="short",
                status="rejected",
                ts=NOW,
            )
        )
        s.commit()
        report = build_soak_report(s, now=NOW, since=SINCE)
    recon = _section(report, "reconciliation")
    assert recon.ok is True  # decided-and-rejected is handled, not an orphan
    assert recon.rows["orphan_actionable_signals"] == 0


def test_realized_pnl_takes_latest_per_partition_not_sum() -> None:
    with _session() as s:
        _seed_clean(s)
        # Cumulative snapshots for one partition: latest (8) is the truth, not 25.
        s.add(_metric("m1", 5.0, NOW - timedelta(minutes=3)))
        s.add(_metric("m2", 12.0, NOW - timedelta(minutes=2)))
        s.add(_metric("m3", 8.0, NOW - timedelta(minutes=1)))
        # A blocked row must be excluded entirely.
        s.add(_metric("mb", 999.0, NOW, mode="blocked"))
        s.commit()
        report = build_soak_report(s, now=NOW, since=SINCE)
    pnl = _section(report, "realized_pnl")
    assert pnl.rows["demo_user/test_strategy_alpha_v1/spot/realized_pnl"] == 8.0
    assert not any("blocked" in k for k in pnl.rows)


def test_stale_feedback_heartbeat_fails_when_required() -> None:
    with _session() as s:
        _seed_clean(s)
        hb = s.get(ServiceHeartbeat, "feedback_loop_engine")
        assert hb is not None
        hb.last_success_at = NOW - timedelta(hours=6)
        s.commit()
        report = build_soak_report(s, now=NOW, since=SINCE)
    assert _section(report, "feedback_liveness").ok is False


def test_feedback_optional_when_not_required() -> None:
    with _session() as s:
        _seed_clean(s)
        hb = s.get(ServiceHeartbeat, "feedback_loop_engine")
        s.delete(hb)
        s.commit()
        report = build_soak_report(s, now=NOW, since=SINCE, require_feedback=False)
    # Missing heartbeat is informational (UNKNOWN), not a failure.
    assert _section(report, "feedback_liveness").ok is None
    assert report.passed


def test_schema_behind_head_degrades_not_crashes() -> None:
    with _session() as s:
        _seed_clean(s)
        # Simulate a schema behind the application's required scoring contract.
        # Dropping the table avoids coupling this robustness test to SQLite's
        # version-specific ALTER COLUMN behavior and still proves stage isolation.
        s.execute(text("DROP TABLE asset_scores"))
        s.commit()
        report = build_soak_report(s, now=NOW, since=SINCE)
    assert _section(report, "schema").ok is False  # preflight flags drift
    assert _section(report, "scoring").error is not None  # stage isolated, not crashed
    assert report.degraded
    assert not report.passed
