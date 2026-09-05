"""SQLAlchemy implementation of :class:`ISignalPerformanceRepository`.

Mirrors the inline DB code that previously lived in
``apps/feedback_loop_engine/feedback_loop_engine/engine.py``
(``_update_consecutive_wrong_tracker`` + ``_has_pending_optimization``).

Sprint F (Plan §6.9) — extracts the persistence concerns into the port
adapter so the engine can be unit-tested with an in-memory stub and so
``feedback_loop_engine/engine.py`` no longer needs lazy
``from lib_application.db.models import StrategyConsecutiveWrongTracker``
imports inside its methods.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from lib_common.logging import get_logger
from lib_common.time_utils import now_utc
from lib_strategy.ports.signal_performance_port import (
    ConsecutiveWrongTracker,
    ISignalPerformanceRepository,
    PendingSuggestion,
)

logger = get_logger(__name__)

if TYPE_CHECKING:
    from lib_application.db.models import (
        StrategyConsecutiveWrongTracker,
        StrategyParameterFeedback,
    )


class SQLAlchemySignalPerformanceRepository(ISignalPerformanceRepository):
    """SQLAlchemy adapter against the ``StrategyConsecutiveWrongTracker``
    and ``StrategyParameterFeedback`` tables in ``lib_application.db.models``.

    The constructor takes either a session-factory callable or a bound
    SQLAlchemy ``Engine``. Both forms are supported because the
    feedback engine currently owns its own ``Engine`` instance and would
    prefer not to introduce a session_factory just to use this port.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
        engine: Engine | None = None,
    ) -> None:
        if session_factory is None and engine is None:
            msg = "Either session_factory or engine must be provided"
            raise ValueError(msg)
        self._session_factory = session_factory
        self._engine = engine

    def _session(self) -> AbstractContextManager[Session]:
        if self._session_factory is not None:
            return self._session_factory()
        session: Session = Session(self._engine)
        return session

    def update_consecutive_wrong_tracker(
        self,
        *,
        strategy_id: str,
        strat_ver_id: int | None,
        instr_id: int,
        signal_id: int,
        signal_ts: datetime,
        is_correct: bool,
        wrong_threshold: int,
        horizon: str = "1d",
    ) -> ConsecutiveWrongTracker:
        # Lazy-import the ORM model so this module can still be imported
        # in environments where the production schema isn't installed
        # (e.g., libs-only test rigs).
        from lib_application.db.models import StrategyConsecutiveWrongTracker  # noqa: PLC0415

        with self._session() as session:
            bind = session.get_bind()
            if bind.dialect.name == "postgresql":
                scope = f"{strategy_id}:{instr_id}:{horizon}"
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
                    {"scope": scope},
                )

            stmt = (
                select(StrategyConsecutiveWrongTracker)
                .where(
                    StrategyConsecutiveWrongTracker.strategy_id == strategy_id,
                    StrategyConsecutiveWrongTracker.instr_id == instr_id,
                    StrategyConsecutiveWrongTracker.horizon == horizon,
                )
                .with_for_update()
            )
            tracker = session.execute(stmt).scalars().first()

            now = now_utc()
            threshold_reached = not is_correct and wrong_threshold <= 1

            if tracker is None:
                tracker = StrategyConsecutiveWrongTracker(
                    strategy_id=strategy_id,
                    strat_ver_id=strat_ver_id,
                    instr_id=instr_id,
                    horizon=horizon,
                    consecutive_wrong_count=0 if is_correct else 1,
                    last_signal_id=signal_id,
                    last_signal_ts=signal_ts,
                    last_evaluation_ts=now,
                    wrong_threshold=wrong_threshold,
                    threshold_reached=threshold_reached,
                    threshold_reached_at=now if threshold_reached else None,
                    updated_at=now,
                )
                if is_correct:
                    tracker.last_reset_ts = now
                    tracker.reset_reason = "correct_prediction"
                session.add(tracker)
            elif tracker.last_signal_id == signal_id:
                # A crash after committing the tracker but before persisting the
                # evaluation causes the same signal to be replayed. Returning
                # the current snapshot keeps that retry from incrementing the
                # sequence twice.
                return self._tracker_snapshot(tracker)
            elif self._precedes_tracker(
                tracker,
                signal_id=signal_id,
                signal_ts=signal_ts,
            ):
                # Late price data can make an older signal evaluable only after
                # the tracker has advanced. It remains valid performance
                # evidence, but must never rewind/reset the live sequence.
                logger.warning(
                    "Ignoring out-of-order tracker update for %s/%s/%s signal=%s",
                    strategy_id,
                    instr_id,
                    horizon,
                    signal_id,
                )
                return self._tracker_snapshot(tracker)
            elif tracker.strat_ver_id != strat_ver_id:
                tracker.strat_ver_id = strat_ver_id
                tracker.consecutive_wrong_count = 0 if is_correct else 1
                tracker.wrong_threshold = wrong_threshold
                tracker.threshold_reached = threshold_reached
                tracker.threshold_reached_at = now if threshold_reached else None
                tracker.feedback_id = None
                tracker.last_reset_ts = now
                tracker.reset_reason = "strategy_version_changed"
            else:
                if is_correct:
                    tracker.consecutive_wrong_count = 0
                    tracker.threshold_reached = False
                    tracker.threshold_reached_at = None
                    tracker.last_reset_ts = now
                    tracker.reset_reason = "correct_prediction"
                else:
                    tracker.consecutive_wrong_count += 1
                    if tracker.consecutive_wrong_count >= tracker.wrong_threshold:
                        tracker.threshold_reached = True
                        tracker.threshold_reached_at = now

                tracker.wrong_threshold = wrong_threshold

            tracker.last_signal_id = signal_id
            tracker.last_signal_ts = signal_ts
            tracker.last_evaluation_ts = now
            tracker.updated_at = now

            session.commit()

            return self._tracker_snapshot(tracker)

    @staticmethod
    def _precedes_tracker(
        tracker: StrategyConsecutiveWrongTracker,
        *,
        signal_id: int,
        signal_ts: datetime,
    ) -> bool:
        """Return whether a signal is older than the durable tracker cursor."""

        if tracker.last_signal_ts is None:
            return tracker.last_signal_id is not None and signal_id < tracker.last_signal_id
        current_ts = signal_ts
        durable_ts = tracker.last_signal_ts
        if current_ts.tzinfo is None:
            current_ts = current_ts.replace(tzinfo=UTC)
        else:
            current_ts = current_ts.astimezone(UTC)
        if durable_ts.tzinfo is None:
            durable_ts = durable_ts.replace(tzinfo=UTC)
        else:
            durable_ts = durable_ts.astimezone(UTC)
        durable_id = tracker.last_signal_id if tracker.last_signal_id is not None else -1
        return (current_ts, signal_id) < (durable_ts, durable_id)

    @staticmethod
    def _tracker_snapshot(
        tracker: StrategyConsecutiveWrongTracker,
    ) -> ConsecutiveWrongTracker:
        return ConsecutiveWrongTracker(
            strategy_id=tracker.strategy_id,
            strat_ver_id=tracker.strat_ver_id,
            instr_id=tracker.instr_id,
            horizon=tracker.horizon,
            consecutive_wrong_count=tracker.consecutive_wrong_count,
            wrong_threshold=tracker.wrong_threshold,
            threshold_reached=tracker.threshold_reached,
            threshold_reached_at=tracker.threshold_reached_at,
            feedback_id=tracker.feedback_id,
            last_signal_id=tracker.last_signal_id,
            last_signal_ts=tracker.last_signal_ts,
            last_evaluation_ts=tracker.last_evaluation_ts,
        )

    def has_pending_optimization(
        self,
        strategy_id: str,
        instr_id: int | None = None,
        horizon: str | None = None,
        *,
        max_age_days: int = 14,
    ) -> bool:
        from lib_application.db.models import StrategyParameterFeedback  # noqa: PLC0415

        with self._session() as session:
            # Expire stale pending suggestions so an un-reviewed one cannot block
            # re-optimization forever (FB-3); uses the 'expired' status the schema
            # already permits.
            cutoff = now_utc() - timedelta(days=max_age_days)
            for row in session.execute(
                select(StrategyParameterFeedback).where(
                    StrategyParameterFeedback.strategy_id == strategy_id,
                    StrategyParameterFeedback.status == "pending",
                    StrategyParameterFeedback.created_at < cutoff,
                )
            ).scalars():
                row.status = "expired"

            # A fresh pending suggestion blocks re-optimization only for the SAME
            # instrument — strategy-wide gating starved every other instrument.
            stmt = select(StrategyParameterFeedback.feedback_id).where(
                StrategyParameterFeedback.strategy_id == strategy_id,
                StrategyParameterFeedback.status == "pending",
            )
            if instr_id is not None:
                stmt = stmt.where(StrategyParameterFeedback.instr_id == instr_id)
            if horizon is not None:
                stmt = stmt.where(StrategyParameterFeedback.horizon == horizon)
            result = session.execute(stmt).scalars().first()
            session.commit()
            return result is not None

    def link_feedback_to_tracker(
        self,
        *,
        strategy_id: str,
        instr_id: int,
        horizon: str,
        feedback_id: int,
    ) -> bool:
        from lib_application.db.models import (  # noqa: PLC0415
            StrategyConsecutiveWrongTracker,
            StrategyParameterFeedback,
        )

        with self._session() as session:
            feedback = session.execute(
                select(StrategyParameterFeedback)
                .where(
                    StrategyParameterFeedback.feedback_id == feedback_id,
                    StrategyParameterFeedback.status == "pending",
                    StrategyParameterFeedback.strategy_id == strategy_id,
                    StrategyParameterFeedback.instr_id == instr_id,
                    StrategyParameterFeedback.horizon == horizon,
                )
                .with_for_update()
            ).scalar_one_or_none()
            tracker = session.execute(
                select(StrategyConsecutiveWrongTracker)
                .where(
                    StrategyConsecutiveWrongTracker.strategy_id == strategy_id,
                    StrategyConsecutiveWrongTracker.instr_id == instr_id,
                    StrategyConsecutiveWrongTracker.horizon == horizon,
                    StrategyConsecutiveWrongTracker.threshold_reached.is_(True),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if (
                feedback is None
                or tracker is None
                or feedback.strat_ver_id != tracker.strat_ver_id
                or tracker.feedback_id not in (None, feedback_id)
            ):
                session.rollback()
                return False
            tracker.feedback_id = feedback_id
            session.commit()
            return True

    def list_unlinked_reached_trackers(
        self,
        *,
        limit: int = 100,
    ) -> list[ConsecutiveWrongTracker]:
        from lib_application.db.models import StrategyConsecutiveWrongTracker  # noqa: PLC0415

        with self._session() as session:
            rows = (
                session.execute(
                    select(StrategyConsecutiveWrongTracker)
                    .where(
                        StrategyConsecutiveWrongTracker.threshold_reached.is_(True),
                        StrategyConsecutiveWrongTracker.feedback_id.is_(None),
                    )
                    .order_by(
                        StrategyConsecutiveWrongTracker.threshold_reached_at,
                        StrategyConsecutiveWrongTracker.tracker_id,
                    )
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [
                ConsecutiveWrongTracker(
                    strategy_id=row.strategy_id,
                    strat_ver_id=row.strat_ver_id,
                    instr_id=row.instr_id,
                    horizon=row.horizon,
                    consecutive_wrong_count=row.consecutive_wrong_count,
                    wrong_threshold=row.wrong_threshold,
                    threshold_reached=row.threshold_reached,
                    threshold_reached_at=row.threshold_reached_at,
                    feedback_id=row.feedback_id,
                    last_signal_id=row.last_signal_id,
                    last_signal_ts=row.last_signal_ts,
                    last_evaluation_ts=row.last_evaluation_ts,
                )
                for row in rows
            ]

    def list_pending_suggestions(
        self,
        strategy_id: str | None = None,
    ) -> list[PendingSuggestion]:
        from lib_application.db.models import StrategyParameterFeedback  # noqa: PLC0415

        with self._session() as session:
            stmt = select(StrategyParameterFeedback).where(
                StrategyParameterFeedback.status == "pending"
            )
            if strategy_id:
                stmt = stmt.where(StrategyParameterFeedback.strategy_id == strategy_id)

            rows = session.execute(stmt).scalars().all()
            return [self._to_pending_suggestion(row) for row in rows]

    def get_pending_suggestion(self, feedback_id: int) -> PendingSuggestion | None:
        from lib_application.db.models import StrategyParameterFeedback  # noqa: PLC0415

        if feedback_id <= 0:
            return None
        with self._session() as session:
            row = session.execute(
                select(StrategyParameterFeedback).where(
                    StrategyParameterFeedback.feedback_id == feedback_id,
                    StrategyParameterFeedback.status == "pending",
                )
            ).scalar_one_or_none()
            return self._to_pending_suggestion(row) if row is not None else None

    @staticmethod
    def _to_pending_suggestion(
        row: StrategyParameterFeedback,
    ) -> PendingSuggestion:
        return PendingSuggestion(
            feedback_id=int(row.feedback_id),
            strategy_id=str(row.strategy_id),
            strat_ver_id=int(row.strat_ver_id) if row.strat_ver_id is not None else None,
            instr_id=int(row.instr_id) if row.instr_id is not None else None,
            horizon=str(row.horizon),
            trigger_reason=str(row.trigger_reason),
            consecutive_wrong=int(row.consecutive_wrong_signals or 0),
            current_params=dict(row.current_params or {}),
            suggested_params=dict(row.suggested_params or {}),
            explanation=row.explanation,
            created_at=row.created_at,
        )

    def approve_suggestion(
        self,
        *,
        feedback_id: int,
        reviewer_user_id: str,
    ) -> bool:
        from lib_application.services.strategy_feedback import (  # noqa: PLC0415
            begin_feedback_transition,
        )

        with self._session() as session:
            record = begin_feedback_transition(
                session,
                feedback_id=feedback_id,
                current_status="pending",
                target_status="approved",
            )
            if record is None:
                logger.error(
                    "Feedback is not pending for an active exact strategy version: %s",
                    feedback_id,
                )
                return False

            record.reviewed_by = reviewer_user_id
            record.reviewed_at = now_utc()
            session.commit()
            logger.info("Approved feedback %s by user %s", feedback_id, reviewer_user_id)
            return True

    def reject_suggestion(
        self,
        *,
        feedback_id: int,
        reviewer_user_id: str,
        reason: str | None = None,
    ) -> bool:
        from lib_application.services.strategy_feedback import (  # noqa: PLC0415
            begin_feedback_transition,
        )

        with self._session() as session:
            record = begin_feedback_transition(
                session,
                feedback_id=feedback_id,
                current_status="pending",
                target_status="rejected",
            )
            if record is None:
                logger.error(
                    "Feedback is not pending for an active exact strategy version: %s",
                    feedback_id,
                )
                return False

            record.reviewed_by = reviewer_user_id
            record.reviewed_at = now_utc()
            record.review_notes = reason
            session.commit()
            logger.info("Rejected feedback %s by user %s", feedback_id, reviewer_user_id)
            return True
