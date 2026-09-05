"""Transient execution failures must retry, not silently drop (C-2, M-16).

A non-raising ExecutionResult(success=False) for a transient infra failure must
(a) be classified retryable, (b) leave the dedup row "pending" (reclaimable) so a
relay retry can re-execute — not a blocking "failed" row. Terminal policy blocks
stay terminal. M-16: a lost insert race (concurrent claim) returns False, not raise.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from execution_engine.deduplication import ExecutionDeduplicator
from execution_engine.execution_result import (
    RETRYABLE_BLOCK_REASONS,
    ExecutionResult,
    is_retryable_failure,
)
from lib_application.db.models import Base, ExecutionDecisionLog
from lib_application.db.session import create_engine_for_env, get_session_factory


def _result(
    *, success: bool, execution_mode: str, block_reason: str | None = None
) -> ExecutionResult:
    return ExecutionResult(
        success=success,
        signal_id="sig-1",
        symbol="BTCUSD",
        execution_mode=execution_mode,
        broker="coinbase",
        orders_submitted=1,
        orders_filled=1 if success else 0,
        total_quantity=0.0,
        average_price=0.0,
        total_commission=0.0,
        block_reason=block_reason,
    )


def _session_local():
    engine = create_engine_for_env(env="test")
    Base.metadata.create_all(engine)
    return get_session_factory(engine=engine, expire_on_commit=False)


# ── C-2: classification ────────────────────────────────────────────────────
def test_is_retryable_failure_classification() -> None:
    # Transient infra failure (broker unavailable/order rejected) → retryable.
    assert is_retryable_failure(_result(success=False, execution_mode="spot")) is True
    # Deliberate policy/validation block with no reason → terminal.
    assert is_retryable_failure(_result(success=False, execution_mode="blocked")) is False
    # Duplicate skip → terminal.
    assert is_retryable_failure(_result(success=False, execution_mode="dedup")) is False
    # Any success → never retryable.
    assert is_retryable_failure(_result(success=True, execution_mode="spot")) is False


def test_blocked_retryable_iff_transient_reason() -> None:
    # RG-2: a "blocked" result is retryable ONLY when its block_reason is a known
    # transient infra failure — a momentarily-unavailable/stale dependency.
    for reason in RETRYABLE_BLOCK_REASONS:
        assert (
            is_retryable_failure(
                _result(success=False, execution_mode="blocked", block_reason=reason)
            )
            is True
        ), reason
    # A deterministic policy rejection (risk rule_code) stays terminal.
    assert (
        is_retryable_failure(
            _result(success=False, execution_mode="blocked", block_reason="max_daily_loss_pct")
        )
        is False
    )
    # An unrecognized / missing reason stays terminal (safe-by-default).
    assert (
        is_retryable_failure(
            _result(success=False, execution_mode="blocked", block_reason="some_new_gate")
        )
        is False
    )
    # A transient reason on a duplicate skip is still terminal (dedup is terminal).
    assert (
        is_retryable_failure(
            _result(success=False, execution_mode="dedup", block_reason="account_state_unavailable")
        )
        is False
    )


def test_transient_blocked_result_is_reclaimable_end_to_end() -> None:
    # The full chain: a transient blocked result → retryable=True → "pending" row →
    # a relay retry can re-claim it (RG-2 + cd5f45f pipeline).
    session_local = _session_local()
    key = "exec:blocked-transient"
    result = _result(success=False, execution_mode="blocked", block_reason="market_data_missing")
    writer = ExecutionDeduplicator(session_factory=session_local, ttl_seconds=300)
    assert writer.claim_execution(
        key, signal_id="sig-1", user_id="u1", symbol="BTCUSD", action="long"
    )
    writer.mark_executed(
        key,
        signal_id="sig-1",
        user_id="u1",
        symbol="BTCUSD",
        action="long",
        success=False,
        retryable=is_retryable_failure(result),
    )
    with session_local() as s:
        row = (
            s.query(ExecutionDecisionLog).filter(ExecutionDecisionLog.idempotency_key == key).one()
        )
        assert row.status == "pending"
    # The same long-lived worker must be able to reclaim immediately; a
    # retryable result cannot remain trapped behind its in-memory claim TTL.
    assert writer.claim_execution(
        key, signal_id="sig-1", user_id="u1", symbol="BTCUSD", action="long"
    )
    writer.mark_executed(
        key,
        signal_id="sig-1",
        user_id="u1",
        symbol="BTCUSD",
        action="long",
        success=False,
        retryable=True,
    )
    retry_worker = ExecutionDeduplicator(session_factory=session_local, ttl_seconds=300)
    assert retry_worker.claim_execution(
        key, signal_id="sig-1", user_id="u1", symbol="BTCUSD", action="long"
    )


def test_terminal_blocked_result_blocks_reclaim_end_to_end() -> None:
    # A deterministic policy block → retryable=False → "failed" row → no reclaim.
    session_local = _session_local()
    key = "exec:blocked-terminal"
    result = _result(success=False, execution_mode="blocked", block_reason="shorting_disabled")
    writer = ExecutionDeduplicator(session_factory=session_local, ttl_seconds=300)
    assert writer.claim_execution(
        key, signal_id="sig-1", user_id="u1", symbol="BTCUSD", action="short"
    )
    writer.mark_executed(
        key,
        signal_id="sig-1",
        user_id="u1",
        symbol="BTCUSD",
        action="short",
        success=False,
        retryable=is_retryable_failure(result),
    )
    with session_local() as s:
        row = (
            s.query(ExecutionDecisionLog).filter(ExecutionDecisionLog.idempotency_key == key).one()
        )
        assert row.status == "failed"
    retry_worker = ExecutionDeduplicator(session_factory=session_local, ttl_seconds=300)
    assert not retry_worker.claim_execution(
        key, signal_id="sig-1", user_id="u1", symbol="BTCUSD", action="short"
    )


# ── C-2: transient failure stays reclaimable ───────────────────────────────
def test_transient_failure_recorded_pending_and_reclaimable() -> None:
    session_local = _session_local()
    key = "exec:transient"

    writer = ExecutionDeduplicator(session_factory=session_local, ttl_seconds=300)
    assert writer.claim_execution(
        key, signal_id="sig-1", user_id="u1", symbol="BTCUSD", action="long"
    )
    writer.mark_executed(
        key,
        signal_id="sig-1",
        user_id="u1",
        symbol="BTCUSD",
        action="long",
        success=False,
        retryable=True,
    )

    with session_local() as s:
        row = (
            s.query(ExecutionDecisionLog).filter(ExecutionDecisionLog.idempotency_key == key).one()
        )
        assert row.status == "pending"

    # A fresh worker (relay retry) must be able to re-claim the pending row.
    retry_worker = ExecutionDeduplicator(session_factory=session_local, ttl_seconds=300)
    assert retry_worker.claim_execution(
        key, signal_id="sig-1", user_id="u1", symbol="BTCUSD", action="long"
    )


# ── C-2: terminal failure blocks redelivery ────────────────────────────────
def test_terminal_failure_recorded_failed_and_blocks() -> None:
    session_local = _session_local()
    key = "exec:terminal"

    writer = ExecutionDeduplicator(session_factory=session_local, ttl_seconds=300)
    assert writer.claim_execution(
        key, signal_id="sig-1", user_id="u1", symbol="BTCUSD", action="long"
    )
    writer.mark_executed(
        key,
        signal_id="sig-1",
        user_id="u1",
        symbol="BTCUSD",
        action="long",
        success=False,
        retryable=False,
    )

    with session_local() as s:
        row = (
            s.query(ExecutionDecisionLog).filter(ExecutionDecisionLog.idempotency_key == key).one()
        )
        assert row.status == "failed"

    retry_worker = ExecutionDeduplicator(session_factory=session_local, ttl_seconds=300)
    assert not retry_worker.claim_execution(
        key, signal_id="sig-1", user_id="u1", symbol="BTCUSD", action="long"
    )


# ── M-16: lost insert race returns False (does not raise) ──────────────────
class _RaceSession:
    """Session stub reproducing a lost insert race: the SELECT misses (None) but
    the INSERT commit raises IntegrityError because a concurrent worker inserted
    the same idempotency_key first."""

    def __init__(self) -> None:
        self.bind = None
        self.rolled_back = False

    def __enter__(self) -> _RaceSession:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def query(self, *a: object, **k: object) -> _RaceSession:
        return self

    def filter(self, *a: object, **k: object) -> _RaceSession:
        return self

    def with_for_update(self, *a: object, **k: object) -> _RaceSession:
        return self

    def first(self) -> None:
        return None

    def add(self, *a: object, **k: object) -> None:
        return None

    def commit(self) -> None:
        raise IntegrityError("INSERT", {}, Exception("duplicate idempotency_key"))

    def rollback(self) -> None:
        self.rolled_back = True


def test_claim_lost_insert_race_returns_false_not_raise() -> None:
    race_session = _RaceSession()
    dedup = ExecutionDeduplicator(session_factory=lambda: race_session, ttl_seconds=300)

    claimed = dedup.claim_execution(
        "exec:race", signal_id="sig-1", user_id="u1", symbol="BTCUSD", action="long"
    )

    assert claimed is False
    assert race_session.rolled_back is True
