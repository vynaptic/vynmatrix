"""Fail-closed eligibility and state transitions for strategy feedback."""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lib_application.db.models import (
    Strategy,
    StrategyParameterFeedback,
    StrategyVersion,
)

_ALLOWED_TRANSITIONS = frozenset(
    {
        ("pending", "approved"),
        ("pending", "rejected"),
    }
)


def exact_strategy_version_is_active(
    session: Session,
    *,
    strategy_id: str,
    strat_ver_id: int | None,
) -> bool:
    """Lock and verify the exact catalogue strategy/version for feedback.

    PostgreSQL requires UPDATE privilege for row-locking SELECT statements.
    The feedback service intentionally has read-only catalogue privileges, so
    production uses the narrowly scoped SECURITY DEFINER function installed by
    migration 0073. SQLite test databases retain the direct-query path.
    """

    if strat_ver_id is None:
        return False
    if session.get_bind().dialect.name == "postgresql":
        return bool(
            session.execute(
                text(
                    "SELECT public.vm_feedback_exact_strategy_version_active("
                    ":strategy_id, :strat_ver_id)"
                ),
                {
                    "strategy_id": strategy_id,
                    "strat_ver_id": strat_ver_id,
                },
            ).scalar_one()
        )
    strategy_query = (
        select(Strategy.strategy_id)
        .where(Strategy.strategy_id == strategy_id, Strategy.is_active.is_(True))
        .with_for_update()
    )
    version_query = (
        select(StrategyVersion.strat_ver_id)
        .where(
            StrategyVersion.strat_ver_id == strat_ver_id,
            StrategyVersion.strategy_id == strategy_id,
            StrategyVersion.status == "active",
        )
        .with_for_update()
    )
    return (
        session.execute(strategy_query).scalar_one_or_none() is not None
        and session.execute(version_query).scalar_one_or_none() is not None
    )


def begin_feedback_transition(
    session: Session,
    *,
    feedback_id: int,
    current_status: str,
    target_status: str,
) -> StrategyParameterFeedback | None:
    """Lock and stage one allowed transition for an active exact version.

    The caller owns commit/rollback and any transition-specific metadata. A
    missing, retired, mismatched, or stale-state record returns ``None`` without
    mutation.
    """

    if (current_status, target_status) not in _ALLOWED_TRANSITIONS:
        message = f"Unsupported feedback transition: {current_status!r} -> {target_status!r}"
        raise ValueError(message)
    candidate = session.get(StrategyParameterFeedback, feedback_id)
    if candidate is None or not exact_strategy_version_is_active(
        session,
        strategy_id=candidate.strategy_id,
        strat_ver_id=candidate.strat_ver_id,
    ):
        return None
    record: StrategyParameterFeedback | None = session.execute(
        select(StrategyParameterFeedback)
        .where(
            StrategyParameterFeedback.feedback_id == feedback_id,
            StrategyParameterFeedback.status == current_status,
            StrategyParameterFeedback.strategy_id == candidate.strategy_id,
            StrategyParameterFeedback.strat_ver_id == candidate.strat_ver_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if record is None:
        return None
    record.status = target_status
    return record
