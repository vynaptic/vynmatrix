from __future__ import annotations

from datetime import UTC, datetime, timedelta

from execution_engine.deduplication import ExecutionDeduplicator
from lib_application.db.models import Base, ExecutionDecisionLog
from lib_application.db.session import create_engine_for_env, get_session_factory


def _session_local():
    engine = create_engine_for_env(env="test")
    Base.metadata.create_all(engine)
    return get_session_factory(engine=engine, expire_on_commit=False)


def test_claim_execution_blocks_fresh_in_flight_claim_after_restart() -> None:
    session_local = _session_local()
    key = "exec:test-in-flight"

    worker_a = ExecutionDeduplicator(session_factory=session_local, ttl_seconds=300)
    worker_b_after_restart = ExecutionDeduplicator(session_factory=session_local, ttl_seconds=300)

    assert worker_a.claim_execution(
        key,
        signal_id="signal-1",
        user_id="user-1",
        symbol="BTC/USD",
        action="long",
    )
    assert not worker_b_after_restart.claim_execution(
        key,
        signal_id="signal-1",
        user_id="user-1",
        symbol="BTC/USD",
        action="long",
    )

    with session_local() as session:
        row = (
            session.query(ExecutionDecisionLog)
            .filter(ExecutionDecisionLog.idempotency_key == key)
            .one()
        )
        assert row.status == "executing"


def test_claim_execution_does_not_reclaim_stale_in_flight_claim_by_default() -> None:
    session_local = _session_local()
    key = "exec:test-stale"
    stale_ts = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(minutes=10)

    with session_local() as session:
        session.add(
            ExecutionDecisionLog(
                idempotency_key=key,
                user_id="user-1",
                signal_id="signal-stale",
                action="long",
                status="executing",
                ts=stale_ts,
            )
        )
        session.commit()

    reclaimer = ExecutionDeduplicator(session_factory=session_local, ttl_seconds=60)
    assert not reclaimer.claim_execution(
        key,
        signal_id="signal-redelivered",
        user_id="user-1",
        symbol="BTC/USD",
        action="long",
    )

    with session_local() as session:
        row = (
            session.query(ExecutionDecisionLog)
            .filter(ExecutionDecisionLog.idempotency_key == key)
            .one()
        )
        assert row.status == "executing"
        assert row.signal_id == "signal-stale"
        assert row.ts == stale_ts


def test_claim_execution_reclaims_stale_in_flight_claim_when_operator_enabled(
    monkeypatch,
) -> None:
    session_local = _session_local()
    key = "exec:test-stale-operator"
    stale_ts = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(minutes=10)

    with session_local() as session:
        session.add(
            ExecutionDecisionLog(
                idempotency_key=key,
                user_id="user-1",
                signal_id="signal-stale",
                action="long",
                status="executing",
                ts=stale_ts,
            )
        )
        session.commit()

    reclaimer = ExecutionDeduplicator(
        session_factory=session_local,
        ttl_seconds=60,
        allow_stale_executing_reclaim=True,
    )
    monkeypatch.setenv("EXECUTION_DEDUP_ALLOW_STALE_EXECUTING_RECLAIM", "false")
    assert reclaimer.claim_execution(
        key,
        signal_id="signal-redelivered",
        user_id="user-1",
        symbol="BTC/USD",
        action="long",
    )

    reclaimer.mark_executed(
        key,
        signal_id="signal-redelivered",
        user_id="user-1",
        symbol="BTC/USD",
        action="long",
        result_id="order-1",
        success=True,
    )

    with session_local() as session:
        linked = (
            session.query(ExecutionDecisionLog)
            .filter(ExecutionDecisionLog.idempotency_key == key)
            .one()
        )
        assert linked.status == "executed"
        assert linked.execution_id == "order-1"

    late_redelivery = ExecutionDeduplicator(session_factory=session_local, ttl_seconds=60)
    assert not late_redelivery.claim_execution(
        key,
        signal_id="signal-redelivered",
        user_id="user-1",
        symbol="BTC/USD",
        action="long",
    )


def test_mark_executed_blocked_records_rejected_not_failed() -> None:
    # N2: a deterministic policy/risk block is recorded as decision status
    # "rejected", a real broker/infra failure as "failed" — so the decision-log
    # error-rate is not polluted by platform decisions (mirrors execution_logs M1).
    session_local = _session_local()
    dedup = ExecutionDeduplicator(session_factory=session_local, ttl_seconds=60)
    dedup.mark_executed(
        "exec:blocked",
        signal_id="sig-b",
        user_id="u1",
        symbol="BTC/USD",
        action="short",
        success=False,
        blocked=True,
    )
    dedup.mark_executed(
        "exec:failed",
        signal_id="sig-f",
        user_id="u1",
        symbol="BTC/USD",
        action="long",
        success=False,
        blocked=False,
    )
    with session_local() as session:
        statuses = {r.idempotency_key: r.status for r in session.query(ExecutionDecisionLog).all()}
    assert statuses["exec:blocked"] == "rejected"
    assert statuses["exec:failed"] == "failed"


def test_failed_claim_requires_explicit_pending_retry_transition() -> None:
    session_local = _session_local()
    key = "exec:test-retry"
    now = datetime.now(tz=UTC).replace(tzinfo=None)

    with session_local() as session:
        session.add(
            ExecutionDecisionLog(
                idempotency_key=key,
                user_id="user-1",
                signal_id="signal-1",
                action="long",
                status="failed",
                ts=now,
            )
        )
        session.commit()

    deduplicator = ExecutionDeduplicator(session_factory=session_local, ttl_seconds=60)
    assert not deduplicator.claim_execution(
        key,
        signal_id="signal-1",
        user_id="user-1",
        symbol="BTC/USD",
        action="long",
    )

    with session_local() as session:
        row = (
            session.query(ExecutionDecisionLog)
            .filter(ExecutionDecisionLog.idempotency_key == key)
            .one()
        )
        row.status = "pending"
        session.commit()

    assert deduplicator.claim_execution(
        key,
        signal_id="signal-1",
        user_id="user-1",
        symbol="BTC/USD",
        action="long",
    )
