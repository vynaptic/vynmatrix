"""
Signal execution deduplication service.

Prevents duplicate executions of the same signal for a given user.
Uses database-backed persistence for distributed deployment support.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from lib_common.logging import get_logger
from lib_common.metrics import counter
from lib_strategy.signals.utils import compute_execution_dedup_key

logger = get_logger(__name__)

# Default TTL for deduplication entries (1 hour)
DEFAULT_TTL_SECONDS = 3600

# G13: a consumer that dies mid-execution leaves a stale "executing" dedup claim
# that is NOT auto-reclaimed by default (so we never risk a double-fill). The
# cost is a possibly-dropped retry that needs operator review — surface it as a
# metric so the blocked-key condition can be alerted on instead of only living in
# a warning log. None when prometheus-client is not installed.
_DEDUP_STALE_EXECUTING_SKIPPED = counter(
    "vm_execution_dedup_stale_executing_skipped_total",
    "Stale 'executing' dedup claims skipped (not auto-reclaimed); a blocked key "
    "may have dropped a retry and needs operator review.",
)


@dataclass
class ExecutionRecord:
    """Record of a signal execution."""

    execution_key: str
    signal_id: str
    user_id: str
    symbol: str
    action: str
    result_id: str | None
    executed_at: datetime
    expires_at: datetime
    success: bool
    # Transient failure that should stay re-deliverable (DB status "pending")
    # rather than a blocking "failed" row. Only meaningful when success is False.
    # See execution_result.is_retryable_failure.
    retryable: bool = False
    # A deterministic policy/risk block (execution_mode == "blocked"), recorded as
    # decision status "rejected" rather than "failed" — it is a platform decision,
    # not a broker/infra failure (N2). Only meaningful when success is False.
    blocked: bool = False


class ExecutionDeduplicator:
    """
    Deduplication service for signal executions.

    Prevents the same signal from being executed multiple times for one linked
    broker account while allowing an intentional route to another account.
    Supports both in-memory (for testing) and database-backed modes.

    Usage:
        dedup = ExecutionDeduplicator(session_factory=get_session)

        # Check before execution
        key = dedup.get_execution_key(
            external_signal_id,
            user_id,
            broker_account_id,
            symbol,
            action,
        )
        if dedup.has_executed(key):
            return  # Skip duplicate

        # Execute...
        result = await engine.handle_signal(...)

        # Mark as executed
        dedup.mark_executed(key, result.signal_id, success=result.success)
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractContextManager[Any]] | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        use_database: bool | None = None,
        allow_stale_executing_reclaim: bool = False,
        use_memory_only: bool = False,
    ) -> None:
        """
        Initialize deduplicator.

        Args:
            session_factory: Factory for database sessions (for DB mode)
            ttl_seconds: Time-to-live for deduplication entries
            use_database: If True, use database; if False, use in-memory;
                         if None, auto-detect based on session_factory
        """
        self._session_factory = session_factory
        self._ttl_seconds = ttl_seconds

        if use_database is None:
            use_database = session_factory is not None

        self._use_database = use_database
        self._memory_cache: dict[str, ExecutionRecord] = {}
        # ``claim_execution`` runs on worker threads (``asyncio.to_thread``) —
        # the event loop no longer serializes within-process claims, so the
        # memory-cache check-then-claim must be atomic under this lock.
        # Reentrant so the composite claim path can hold it across the nested
        # ``_check_memory`` / ``_store_memory`` calls.
        self._memory_lock = threading.RLock()

        if ttl_seconds <= 0:
            msg = "ttl_seconds must be positive"
            raise ValueError(msg)
        self._allow_stale_executing_reclaim = allow_stale_executing_reclaim
        self._use_memory_only = use_memory_only

    def get_execution_key(
        self,
        external_signal_id: str,
        user_id: str,
        broker_account_id: int,
        symbol: str,
        action: str,
    ) -> str:
        """
        Generate a deterministic execution key.

        The key uniquely identifies a signal execution attempt for a user and
        linked broker account.

        Callers must pass the canonical ``external_signal_id`` so redelivery
        dedups to the same key (SC-1).
        Derivation is shared with the scoring engine via
        ``compute_execution_dedup_key`` so both sides agree byte-for-byte.

        Args:
            external_signal_id: Stable canonical signal identity
            user_id: User identifier
            broker_account_id: Linked broker account selected by the binding
            symbol: Trading symbol
            action: Signal action (long/short/close)

        Returns:
            Deterministic execution key
        """
        return compute_execution_dedup_key(
            external_signal_id,
            user_id,
            broker_account_id,
            symbol,
            action,
        )

    def has_executed(self, key: str) -> bool:
        """
        Check if a signal has already been executed.

        Uses write-through strategy: memory is always checked first (authoritative
        within this process), then DB is checked for cross-process records.

        Args:
            key: Execution key from get_execution_key()

        Returns:
            True if already executed and not expired
        """
        # Always check memory first — it's authoritative within this process
        # and catches records from DB-store fallback scenarios.
        if self._check_memory(key):
            return True
        if self._use_database:
            return self._check_database(key)
        return False

    def claim_execution(
        self,
        key: str,
        *,
        signal_id: str,
        user_id: str,
        symbol: str,
        action: str,
    ) -> bool:
        """Atomically claim an execution before broker-visible side effects.

        Returns ``True`` only for the worker that owns the claim. Existing
        terminal rows, fresh in-flight claims, or local memory claims return
        ``False`` so callers can skip duplicate submissions.
        """
        record = ExecutionRecord(
            execution_key=key,
            signal_id=signal_id,
            user_id=user_id,
            symbol=symbol,
            action=action,
            result_id=None,
            executed_at=datetime.now(tz=UTC),
            expires_at=datetime.now(tz=UTC) + timedelta(seconds=self._ttl_seconds),
            success=False,
        )
        if self._use_database:
            # The database row (FOR UPDATE + unique idempotency_key) is the
            # claim arbiter across threads and processes; the memory check is
            # only a fast path, so it does not need to be atomic with the claim.
            if self._check_memory(key):
                return False
            claimed = self._claim_database(
                key,
                signal_id=signal_id,
                user_id=user_id,
                action=action,
            )
            if claimed:
                self._store_memory(record)
            return claimed

        # Memory-only mode: the cache itself is the arbiter, so check-then-claim
        # must be one atomic step now that claims run on worker threads.
        with self._memory_lock:
            if self._check_memory(key):
                return False
            self._store_memory(record)
            return True

    def _check_memory(self, key: str) -> bool:
        """Check in-memory cache for execution record."""
        with self._memory_lock:
            record = self._memory_cache.get(key)
            if record is None:
                return False

            # Check expiration
            if datetime.now(tz=UTC) > record.expires_at:
                self._memory_cache.pop(key, None)
                return False

            return True

    def _check_database(self, key: str) -> bool:
        """Check database for execution record."""
        if self._session_factory is None:
            return False

        try:
            from lib_application.db.models import ExecutionDecisionLog  # noqa: PLC0415
        except ImportError:
            logger.warning("ExecutionDecisionLog model not available, falling back to memory")
            return self._check_memory(key)

        try:
            with self._session_factory() as session:
                cutoff = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(
                    seconds=self._ttl_seconds
                )
                record = (
                    session.query(ExecutionDecisionLog)
                    .filter(
                        ExecutionDecisionLog.idempotency_key == key,
                        ExecutionDecisionLog.ts > cutoff,
                    )
                    .first()
                )
                if record is None:
                    return False

                status = str(record.status or "").lower()
                return status in {"executing", "executed", "failed"}
        except (SQLAlchemyError, OSError):
            # DB dedup failure is a hard error: silent fallback to memory-only
            # is dangerous because cross-process dedup stops working, potentially
            # leading to duplicate executions.  Raise so the caller knows the
            # dedup check is unreliable. Programming errors propagate.
            logger.exception(
                "DB dedup check failed — raising instead of silent fallback. "
                "Set EXECUTION_DEDUP_USE_MEMORY_ONLY=true to opt-in to memory-only mode."
            )
            if self._use_memory_only:
                logger.warning("EXECUTION_DEDUP_USE_MEMORY_ONLY=true; falling back to memory")
                self._use_database = False
                return self._check_memory(key)
            raise

    def _claim_database(
        self,
        key: str,
        *,
        signal_id: str,
        user_id: str,
        action: str,
    ) -> bool:
        """Claim an ExecutionDecisionLog row using the DB as arbiter."""
        if self._session_factory is None:
            return False

        try:
            from lib_application.db.models import ExecutionDecisionLog  # noqa: PLC0415
        except ImportError:
            logger.warning("ExecutionDecisionLog model not available; using memory claim")
            self._use_database = False
            return True

        now = datetime.now(tz=UTC).replace(tzinfo=None)
        stale_cutoff = now - timedelta(seconds=self._ttl_seconds)

        try:
            with self._session_factory() as session:
                query = session.query(ExecutionDecisionLog).filter(
                    ExecutionDecisionLog.idempotency_key == key
                )
                if session.bind is not None and session.bind.dialect.name != "sqlite":
                    query = query.with_for_update()
                row = query.first()

                if row is None:
                    return self._insert_claim_row(
                        session,
                        ExecutionDecisionLog,
                        key=key,
                        signal_id=signal_id,
                        user_id=user_id,
                        action=action,
                        now=now,
                    )

                status = str(getattr(row, "status", "") or "").lower()
                row_ts = getattr(row, "ts", None)
                # ExecutionDecisionLog.ts is tz-aware (DateTime(timezone=True), migration
                # 0029), so Postgres reads it back tz-aware while stale_cutoff is naive UTC.
                # Normalize the row value to naive-UTC before comparing (SQLite already
                # reads it back naive). Without this the comparison raised "can't compare
                # offset-naive and offset-aware datetimes" and 500'd /execute-command.
                if row_ts is not None and row_ts.tzinfo is not None:
                    row_ts = row_ts.astimezone(UTC).replace(tzinfo=None)
                is_stale = bool(row_ts is not None and row_ts < stale_cutoff)
                if status == "pending" or (
                    status == "executing" and is_stale and self._allow_stale_executing_reclaim
                ):
                    row.status = "executing"
                    row.signal_id = signal_id
                    row.action = action
                    row.ts = now
                    session.commit()
                    return True
                if status == "executing" and is_stale:
                    if _DEDUP_STALE_EXECUTING_SKIPPED is not None:
                        _DEDUP_STALE_EXECUTING_SKIPPED.inc()
                    logger.warning(
                        "Stale executing claim for key=%s not reclaimed automatically; "
                        "transition it to pending or set "
                        "EXECUTION_DEDUP_ALLOW_STALE_EXECUTING_RECLAIM=true after "
                        "operator review.",
                        key,
                    )

                session.rollback()
                return False
        except (SQLAlchemyError, OSError):
            logger.exception(
                "DB execution claim failed — raising instead of risking duplicate execution"
            )
            if self._use_memory_only:
                logger.warning("EXECUTION_DEDUP_USE_MEMORY_ONLY=true; using memory claim")
                self._use_database = False
                return True
            raise

    @staticmethod
    def _insert_claim_row(
        session: Any,
        model: Any,
        *,
        key: str,
        signal_id: str,
        user_id: str,
        action: str,
        now: datetime,
    ) -> bool:
        """Insert a fresh "executing" claim row.

        Returns False if a concurrent worker won the insert race — the
        not-exists branch cannot be guarded by ``SELECT ... FOR UPDATE``, so the
        UNIQUE(idempotency_key) constraint is the arbiter. The loser rolls back
        and skips instead of double-executing.
        """
        session.add(
            model(
                idempotency_key=key,
                user_id=user_id,
                signal_id=signal_id,
                action=action,
                status="executing",
                ts=now,
            )
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            logger.info("Lost dedup claim race for key=%s (concurrent insert)", key)
            return False
        return True

    def mark_executed(
        self,
        key: str,
        signal_id: str,
        user_id: str,
        symbol: str,
        action: str,
        result_id: str | None = None,
        success: bool = True,
        retryable: bool = False,
        blocked: bool = False,
    ) -> None:
        """
        Mark a signal as executed.

        Args:
            key: Execution key from get_execution_key()
            signal_id: Original signal ID
            user_id: User who executed
            symbol: Trading symbol
            action: Signal action
            result_id: Execution result ID (if available)
            success: Whether execution succeeded
            retryable: When ``success`` is False, whether the failure is transient
                and the command should remain re-deliverable (recorded as
                "pending") rather than a blocking "failed" row.
            blocked: When ``success`` is False, whether this was a deterministic
                policy/risk block (recorded as "rejected", not "failed").
        """
        now = datetime.now(tz=UTC)
        expires_at = now.replace(tzinfo=None)  # Remove timezone for SQLite compatibility
        expires_at = datetime.fromtimestamp(now.timestamp() + self._ttl_seconds, tz=UTC)

        record = ExecutionRecord(
            execution_key=key,
            signal_id=signal_id,
            user_id=user_id,
            symbol=symbol,
            action=action,
            result_id=result_id,
            executed_at=now,
            expires_at=expires_at,
            success=success,
            retryable=retryable,
            blocked=blocked,
        )

        # The original in-memory claim remains authoritative while the durable
        # result is written. A retryable pre-submission failure must then evict
        # that claim so redelivery in this same process can reclaim it; terminal
        # outcomes remain cached for the normal TTL fast path.
        if self._use_database:
            self._store_database(record)
        if retryable and not success:
            with self._memory_lock:
                self._memory_cache.pop(key, None)
        else:
            self._store_memory(record)

    def _store_memory(self, record: ExecutionRecord) -> None:
        """Store execution record in memory."""
        with self._memory_lock:
            self._memory_cache[record.execution_key] = record

            # Cleanup expired entries periodically
            if len(self._memory_cache) > 1000:  # noqa: PLR2004
                self._cleanup_memory()

    def _cleanup_memory(self) -> None:
        """Remove expired entries from memory cache."""
        with self._memory_lock:
            now = datetime.now(tz=UTC)
            expired_keys = [k for k, v in self._memory_cache.items() if now > v.expires_at]
            for key in expired_keys:
                del self._memory_cache[key]

    def _store_database(self, record: ExecutionRecord) -> None:
        """Best-effort persist to DB for cross-process dedup.

        The original claim remains in memory while this write runs, so if it
        fails the key is still dedup-safe within this process. Retryable
        outcomes are evicted only after this method returns. On failure,
        permanently switches to memory-only mode only when explicitly allowed.
        """
        if self._session_factory is None:
            self._use_database = False
            return

        try:
            from lib_application.db.models import ExecutionDecisionLog  # noqa: PLC0415
        except ImportError:
            logger.warning("ExecutionDecisionLog model not available; disabling DB dedup")
            self._use_database = False
            return

        try:
            with self._session_factory() as session:
                if record.success:
                    new_status = "executed"
                elif record.retryable:
                    # Transient failure: keep the claim re-deliverable so the
                    # outbox relay's retry can re-claim and re-execute, instead
                    # of a blocking "failed" row that silently drops the order.
                    new_status = "pending"
                elif record.blocked:
                    # Deterministic policy/risk block — a platform decision, not a
                    # broker/infra failure (N2). Recorded as "rejected" so the
                    # decision-log error-rate isn't polluted (mirrors the
                    # execution_logs "blocked" status from M1).
                    new_status = "rejected"
                else:
                    new_status = "failed"

                # Try to UPDATE an existing row first (scoring may have already
                # inserted a "pending" row with the same idempotency_key).
                existing = (
                    session.query(ExecutionDecisionLog)
                    .filter(ExecutionDecisionLog.idempotency_key == record.execution_key)
                    .first()
                )
                if existing:
                    existing.status = new_status
                    if record.result_id:
                        existing.execution_id = record.result_id
                    session.commit()
                    return

                # No existing row — insert a minimal one for cross-process dedup.
                # Provenance (L2): execution_decision_logs has TWO authoring paths.
                # The scoring dispatcher writes the rich, gating-decision row
                # (binding_id + should_execute) when it dispatches; this execution-
                # side insert only fires when the execution result arrives WITHOUT a
                # matching scoring row (e.g. the scoring pre-insert path is absent),
                # so binding_id / should_execute are intentionally NULL here — the
                # row records the execution outcome, not the gating decision.
                log_entry = ExecutionDecisionLog(
                    idempotency_key=record.execution_key,
                    user_id=record.user_id,
                    status=new_status,
                    signal_id=record.signal_id,
                    action=record.action,
                    execution_id=record.result_id,
                )
                session.add(log_entry)
                session.commit()
        except (SQLAlchemyError, OSError):
            # DB write failures are logged at error severity but do NOT
            # silently disable DB dedup.  Memory is already written
            # (write-through), so within-process dedup still works.  The DB
            # row will be missing for cross-process dedup — the relay worker
            # or reconciliation should detect and correct this. Programming
            # errors (AttributeError, KeyError) propagate.
            logger.exception(
                "DB dedup write failed for key=%s. Memory is still authoritative within "
                "this process, but cross-process dedup may miss this record. "
                "Set EXECUTION_DEDUP_USE_MEMORY_ONLY=true to suppress.",
                record.execution_key,
            )
            if self._use_memory_only:
                self._use_database = False

    def get_recent_executions(
        self,
        user_id: str,
        limit: int = 100,
    ) -> list[ExecutionRecord]:
        """
        Get recent executions for a user.

        Args:
            user_id: User identifier
            limit: Maximum records to return

        Returns:
            List of recent execution records
        """
        if self._use_database:
            return self._get_recent_from_database(user_id, limit)
        return self._get_recent_from_memory(user_id, limit)

    def _get_recent_from_memory(self, user_id: str, limit: int) -> list[ExecutionRecord]:
        """Get recent executions from memory cache."""
        with self._memory_lock:
            records = [r for r in self._memory_cache.values() if r.user_id == user_id]
        records.sort(key=lambda r: r.executed_at, reverse=True)
        return records[:limit]

    def _get_recent_from_database(self, user_id: str, limit: int) -> list[ExecutionRecord]:
        """Get recent executions from database."""
        if self._session_factory is None:
            return self._get_recent_from_memory(user_id, limit)

        try:
            from lib_application.db.models import ExecutionDecisionLog  # noqa: PLC0415
        except ImportError:
            return self._get_recent_from_memory(user_id, limit)

        try:
            with self._session_factory() as session:
                rows = (
                    session.query(ExecutionDecisionLog)
                    .filter(ExecutionDecisionLog.user_id == user_id)
                    .order_by(ExecutionDecisionLog.ts.desc())
                    .limit(limit)
                    .all()
                )

                # ExecutionDecisionLog has no symbol column; recent-execution
                # records therefore carry an empty symbol.
                return [
                    ExecutionRecord(
                        execution_key=row.idempotency_key or "",
                        signal_id=row.signal_id or "",
                        user_id=row.user_id or "",
                        symbol="",
                        action=row.action or row.direction or "",
                        result_id=row.execution_id,
                        executed_at=row.ts,
                        expires_at=datetime.now(tz=UTC),
                        success=row.status == "executed",
                    )
                    for row in rows
                ]
        except (SQLAlchemyError, OSError):
            # DB / connection-level failures fall back to memory cache.
            logger.exception("Error fetching recent executions from database")
            return self._get_recent_from_memory(user_id, limit)

    def clear_expired(self) -> int:
        """
        Clear expired entries.

        Returns:
            Number of entries cleared
        """
        if self._use_database:
            return self._clear_expired_database()

        with self._memory_lock:
            before = len(self._memory_cache)
            self._cleanup_memory()
            return before - len(self._memory_cache)

    def _clear_expired_database(self) -> int:
        """Clear expired entries from database."""
        if self._session_factory is None:
            return 0

        try:
            from datetime import timedelta  # noqa: PLC0415

            from lib_application.db.models import ExecutionDecisionLog  # noqa: PLC0415
        except ImportError:
            return 0

        try:
            with self._session_factory() as session:
                cutoff_naive = (
                    datetime.now(tz=UTC) - timedelta(seconds=self._ttl_seconds)
                ).replace(tzinfo=None)
                result = (
                    session.query(ExecutionDecisionLog)
                    .filter(ExecutionDecisionLog.ts < cutoff_naive)
                    .delete()
                )
                session.commit()
                return int(result)
        except (SQLAlchemyError, OSError):
            # DB / connection-level failures only — programming errors propagate.
            logger.exception("Error clearing expired entries from database")
            return 0
