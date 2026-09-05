"""Database-backed outbox primitives shared by runtime services."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, and_, case, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from lib_common.time_utils import now_utc as _utcnow


def backlog_age_seconds(oldest_created_at: datetime | None, *, now: datetime) -> float:
    """Age of the oldest undelivered outbox row, tz-normalized, never negative."""
    if oldest_created_at is None:
        return 0.0
    if oldest_created_at.tzinfo is None or oldest_created_at.utcoffset() is None:
        oldest_created_at = oldest_created_at.replace(tzinfo=UTC)
    return max(0.0, (now - oldest_created_at.astimezone(UTC)).total_seconds())


@dataclass(frozen=True)
class OutboxBacklogSnapshot:
    """Backlog progress for one outbox partition.

    SINGLE source of truth for the delivery-readiness predicate shared by the
    scoring outbox relay and the indicator durable signal relay: a partition is
    current only while it has zero dead-lettered rows AND its oldest
    undelivered row is younger than the staleness SLO.
    """

    counts: dict[str, int]
    oldest_age_seconds: float

    @property
    def dead_letter_count(self) -> int:
        return int(self.counts.get("dead_letter", 0))

    def is_current(self, max_age_seconds: float) -> bool:
        return self.dead_letter_count == 0 and self.oldest_age_seconds <= max(
            1.0, float(max_age_seconds)
        )


@dataclass(frozen=True)
class OutboxRecord:
    """Serializable outbox record returned to workers."""

    event_id: str
    topic: str
    event_type: str
    schema_version: str
    aggregate_type: str | None
    aggregate_id: str | None
    event_key: str | None
    ordering_key: str | None
    payload: dict[str, Any]
    headers: dict[str, Any]
    delivery_metadata: dict[str, Any]
    status: str
    attempts: int
    max_attempts: int
    available_at: datetime
    claimed_at: datetime | None
    claim_owner: str | None
    published_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    failure_class: str | None = None
    redrive_generation: int = 0
    redrive_audit: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class OutboxRedriveResult:
    """Outcome of one fenced operator redrive request."""

    acquired: bool
    event_id: str
    generation: int
    outcome: str


SessionFactory = Callable[[], AbstractContextManager[Session]]


class OutboxStore:
    """Helper for enqueueing, claiming, and completing outbox messages."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def enqueue(
        self,
        *,
        topic: str,
        event_type: str,
        payload: dict[str, Any],
        schema_version: str = "v1",
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        event_key: str | None = None,
        ordering_key: str | None = None,
        headers: dict[str, Any] | None = None,
        available_at: datetime | None = None,
        max_attempts: int = 10,
    ) -> str:
        """Insert a pending outbox message or reuse an idempotent existing row.

        Opens its own session and commits. For atomicity with a business write,
        use :meth:`enqueue_on_session` inside the caller's transaction instead.
        """
        from lib_application.db.models import OutboxEvent  # noqa: PLC0415

        with self._session_factory() as session:
            try:
                event_id = self.enqueue_on_session(
                    session,
                    topic=topic,
                    event_type=event_type,
                    payload=payload,
                    schema_version=schema_version,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    event_key=event_key,
                    ordering_key=ordering_key,
                    headers=headers,
                    available_at=available_at,
                    max_attempts=max_attempts,
                )
                session.commit()
            except IntegrityError:
                session.rollback()
                if not event_key:
                    raise
                existing = session.scalars(
                    select(OutboxEvent).where(OutboxEvent.event_key == event_key)
                ).first()
                if existing is None:
                    raise
                return str(existing.event_id)
            return event_id

    def enqueue_on_session(
        self,
        session: Session,
        *,
        topic: str,
        event_type: str,
        payload: dict[str, Any],
        schema_version: str = "v1",
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        event_key: str | None = None,
        ordering_key: str | None = None,
        headers: dict[str, Any] | None = None,
        available_at: datetime | None = None,
        max_attempts: int = 10,
    ) -> str:
        """Enqueue within the caller's transaction — does NOT commit or flush.

        Adds a pending ``OutboxEvent`` to ``session`` so the business state
        change and the event are persisted atomically when the caller commits
        (transactional outbox). The id is generated client-side and returned
        immediately, so no flush is needed. ``event_key`` gives idempotency: an
        existing row with the same key is reused instead of inserting a
        duplicate. Commit and rollback are the caller's responsibility
        (typically a ``unit_of_work``).
        """
        from lib_application.db.models import OutboxEvent, generate_uuid  # noqa: PLC0415

        if event_key:
            existing = session.scalars(
                select(OutboxEvent).where(OutboxEvent.event_key == event_key)
            ).first()
            if existing is not None:
                return str(existing.event_id)

        event_id = generate_uuid()
        event = OutboxEvent(
            event_id=event_id,
            topic=topic,
            event_type=event_type,
            schema_version=schema_version,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_key=event_key,
            ordering_key=ordering_key,
            payload=payload,
            headers=dict(headers or {}),
            status="pending",
            attempts=0,
            max_attempts=max_attempts,
            available_at=available_at or _utcnow(),
        )
        session.add(event)
        return event_id

    def claim_batch(
        self,
        *,
        topics: Iterable[str],
        consumer: str,
        limit: int = 100,
        lease_seconds: int = 60,
        ordering_keys: Iterable[str] | None = None,
        preserve_ordering: bool = False,
    ) -> list[OutboxRecord]:
        """Claim a batch of deliverable outbox messages.

        When ``preserve_ordering`` is enabled, a row is claimable only if no
        older undelivered row exists for the same non-null ``ordering_key``.
        Backoff, active leases, and dead letters therefore stop that partition
        instead of allowing a later command to overtake it.
        """
        from lib_application.db.models import OutboxEvent  # noqa: PLC0415

        topic_list = [topic for topic in topics if topic]
        if not topic_list:
            return []
        ordering_key_list = [key for key in ordering_keys or [] if key]
        if preserve_ordering and not ordering_key_list:
            msg = "preserve_ordering requires at least one ordering_key"
            raise ValueError(msg)

        # ``DateTime`` columns are tz-naive in the unit-test SQLite schema.
        # Strip tz from filter values so SQLite string comparisons resolve
        # correctly; PostgreSQL comparison semantics are unchanged.
        now_aware = _utcnow()
        now = now_aware.replace(tzinfo=None)
        stale_cutoff = now - timedelta(seconds=max(1, lease_seconds))
        claimed: list[OutboxRecord] = []

        with self._session_factory() as session:
            # A row is eligible if it is queued (``pending``/``failed`` — both
            # have a cleared ``claimed_at``) **or** if it was claimed by a
            # worker that died before completing delivery (``in_progress``
            # with a stale lease). The ``available_at`` filter then enforces
            # the backoff window for previously-failed rows.
            stmt = (
                select(OutboxEvent)
                .where(OutboxEvent.topic.in_(topic_list))
                .where(OutboxEvent.available_at <= now)
                .where(
                    OutboxEvent.status.in_(["pending", "failed"])
                    | (
                        (OutboxEvent.status == "in_progress")
                        & (OutboxEvent.claimed_at < stale_cutoff)
                    )
                )
            )
            if ordering_key_list:
                stmt = stmt.where(OutboxEvent.ordering_key.in_(ordering_key_list))
            if preserve_ordering:
                prior = aliased(OutboxEvent)
                older_undelivered = (
                    select(prior.event_id)
                    .where(
                        prior.ordering_key == OutboxEvent.ordering_key,
                        prior.status != "published",
                        or_(
                            prior.created_at < OutboxEvent.created_at,
                            and_(
                                prior.created_at == OutboxEvent.created_at,
                                prior.event_id < OutboxEvent.event_id,
                            ),
                        ),
                    )
                    .exists()
                )
                stmt = stmt.where(~older_undelivered)
            stmt = stmt.order_by(
                OutboxEvent.available_at.asc(),
                OutboxEvent.created_at.asc(),
            ).limit(limit)
            if session.bind is not None and session.bind.dialect.name != "sqlite":
                stmt = stmt.with_for_update(skip_locked=True)

            rows = session.scalars(stmt).all()
            for row in rows:
                row.status = "in_progress"
                row.claim_owner = consumer
                row.claimed_at = now
                row.attempts = int(row.attempts or 0) + 1
                claimed.append(_to_record(row))
            session.commit()
        return claimed

    def mark_published(
        self,
        event_id: str,
        *,
        expected_claim_owner: str,
        expected_attempts: int,
        delivery_metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Publish only if the caller still owns the exact claim generation."""
        from lib_application.db.models import OutboxEvent  # noqa: PLC0415

        with self._session_factory() as session:
            now = _utcnow()
            result = session.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.event_id == event_id,
                    OutboxEvent.status == "in_progress",
                    OutboxEvent.claim_owner == expected_claim_owner,
                    OutboxEvent.attempts == expected_attempts,
                )
                .values(
                    status="published",
                    published_at=now,
                    claimed_at=None,
                    claim_owner=None,
                    last_error=None,
                    failure_class=None,
                    delivery_metadata=dict(delivery_metadata or {}),
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            updated = int(cast("CursorResult[Any]", result).rowcount or 0)
            session.commit()
            return updated == 1

    def mark_failed(
        self,
        event_id: str,
        *,
        expected_claim_owner: str,
        expected_attempts: int,
        error_message: str,
        retry_delay_seconds: int = 30,
        failure_class: str = "transient",
        terminal: bool = False,
    ) -> bool:
        """Release only if the caller still owns the exact claim generation."""
        from lib_application.db.models import OutboxEvent  # noqa: PLC0415

        normalized_failure_class = str(failure_class).strip().lower()
        if normalized_failure_class not in {"transient", "permanent"}:
            msg = "failure_class must be transient or permanent"
            raise ValueError(msg)
        terminal = terminal or normalized_failure_class == "permanent"
        with self._session_factory() as session:
            now = _utcnow()
            next_status: Any = (
                "dead_letter"
                if terminal
                else case(
                    (
                        OutboxEvent.attempts >= OutboxEvent.max_attempts,
                        "dead_letter",
                    ),
                    else_="failed",
                )
            )
            result = session.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.event_id == event_id,
                    OutboxEvent.status == "in_progress",
                    OutboxEvent.claim_owner == expected_claim_owner,
                    OutboxEvent.attempts == expected_attempts,
                )
                .values(
                    last_error=error_message,
                    claimed_at=None,
                    claim_owner=None,
                    available_at=now + timedelta(seconds=max(1, retry_delay_seconds)),
                    status=next_status,
                    failure_class=normalized_failure_class,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            updated = int(cast("CursorResult[Any]", result).rowcount or 0)
            session.commit()
            return updated == 1

    def list_dead_letters(
        self,
        *,
        topics: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[OutboxRecord]:
        """Inspect dead letters without granting delivery authority."""
        from lib_application.db.models import OutboxEvent  # noqa: PLC0415

        with self._session_factory() as session:
            stmt = select(OutboxEvent).where(OutboxEvent.status == "dead_letter")
            topic_list = [topic for topic in topics or [] if topic]
            if topic_list:
                stmt = stmt.where(OutboxEvent.topic.in_(topic_list))
            rows = session.scalars(
                stmt.order_by(OutboxEvent.updated_at.asc(), OutboxEvent.created_at.asc()).limit(
                    max(1, min(int(limit), 1000))
                )
            ).all()
            return [_to_record(row) for row in rows]

    def redrive_dead_letter(
        self,
        event_id: str,
        *,
        actor: str,
        reason: str,
        expected_generation: int,
    ) -> OutboxRedriveResult:
        """Fence and audit one execution-command dead-letter redrive."""
        from lib_application.db.models import OutboxEvent  # noqa: PLC0415

        normalized_event_id = str(event_id or "").strip()
        normalized_actor = str(actor or "").strip()
        normalized_reason = str(reason or "").strip()
        if not normalized_event_id or not normalized_actor or not normalized_reason:
            msg = "event_id, actor, and reason are required for redrive"
            raise ValueError(msg)
        if isinstance(expected_generation, bool) or expected_generation < 0:
            msg = "expected_generation must be a non-negative integer"
            raise ValueError(msg)

        with self._session_factory() as session:
            stmt = select(OutboxEvent).where(OutboxEvent.event_id == normalized_event_id)
            if session.bind is not None and session.bind.dialect.name != "sqlite":
                stmt = stmt.with_for_update()
            row = session.scalars(stmt).one_or_none()
            if row is None:
                msg = f"Outbox event {normalized_event_id} does not exist"
                raise LookupError(msg)
            if str(row.topic) not in {
                "execution.commands",
                "execution.rebalance.commands",
            }:
                msg = "Only execution command dead letters are eligible for redrive"
                raise ValueError(msg)

            observed_generation = int(row.redrive_generation or 0)
            audit = list(row.redrive_audit or [])
            outcome = "queued"
            acquired = True
            if str(row.status) != "dead_letter":
                outcome = "rejected_state"
                acquired = False
            elif observed_generation != expected_generation:
                outcome = "rejected_generation"
                acquired = False

            now = _utcnow()
            next_generation = observed_generation + 1 if acquired else observed_generation
            audit.append(
                {
                    "actor": normalized_actor,
                    "reason": normalized_reason,
                    "source_event_id": normalized_event_id,
                    "requested_generation": expected_generation,
                    "generation": next_generation,
                    "outcome": outcome,
                    "recorded_at": now.isoformat(),
                }
            )
            row.redrive_audit = audit
            row.updated_at = now
            if acquired:
                row.status = "pending"
                row.attempts = 0
                row.available_at = now
                row.claimed_at = None
                row.claim_owner = None
                row.published_at = None
                row.last_error = None
                row.failure_class = None
                row.redrive_generation = next_generation
            session.commit()
            return OutboxRedriveResult(
                acquired=acquired,
                event_id=normalized_event_id,
                generation=next_generation,
                outcome=outcome,
            )

    def list_pending(
        self,
        *,
        topics: Iterable[str] | None = None,
        limit: int = 100,
        ordering_keys: Iterable[str] | None = None,
        include_dead_letter: bool = False,
    ) -> list[OutboxRecord]:
        """List undelivered outbox messages without claiming them.

        Dead letters remain excluded for compatibility unless
        ``include_dead_letter`` is true. Operational readiness callers enable
        it so the reported oldest undelivered age cannot reset to zero merely
        because the head event exhausted its attempts.
        """
        from lib_application.db.models import OutboxEvent  # noqa: PLC0415

        with self._session_factory() as session:
            statuses = ["pending", "failed", "in_progress"]
            if include_dead_letter:
                statuses.append("dead_letter")
            stmt = select(OutboxEvent).where(OutboxEvent.status.in_(statuses))
            topic_list = [topic for topic in topics or [] if topic]
            if topic_list:
                stmt = stmt.where(OutboxEvent.topic.in_(topic_list))
            ordering_key_list = [key for key in ordering_keys or [] if key]
            if ordering_key_list:
                stmt = stmt.where(OutboxEvent.ordering_key.in_(ordering_key_list))
            stmt = stmt.order_by(OutboxEvent.created_at.asc()).limit(limit)
            return [_to_record(row) for row in session.scalars(stmt).all()]

    def backlog_counts(
        self,
        *,
        topics: Iterable[str] | None = None,
        ordering_keys: Iterable[str] | None = None,
    ) -> dict[str, int]:
        """Count UNDELIVERED outbox events by status (excludes ``published``).

        Powers the backlog gauge: a growing ``pending``/``failed`` count or any
        ``dead_letter`` means execution-command delivery is degraded while the
        relay process can still look alive.
        """
        from sqlalchemy import func  # noqa: PLC0415

        from lib_application.db.models import OutboxEvent  # noqa: PLC0415

        with self._session_factory() as session:
            stmt = select(OutboxEvent.status, func.count()).where(OutboxEvent.status != "published")
            topic_list = [topic for topic in topics or [] if topic]
            if topic_list:
                stmt = stmt.where(OutboxEvent.topic.in_(topic_list))
            ordering_key_list = [key for key in ordering_keys or [] if key]
            if ordering_key_list:
                stmt = stmt.where(OutboxEvent.ordering_key.in_(ordering_key_list))
            rows = session.execute(stmt.group_by(OutboxEvent.status)).all()
        return {str(status): int(count) for status, count in rows}


def _to_record(row: Any) -> OutboxRecord:
    return OutboxRecord(
        event_id=str(row.event_id),
        topic=str(row.topic),
        event_type=str(row.event_type),
        schema_version=str(row.schema_version),
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        event_key=row.event_key,
        ordering_key=row.ordering_key,
        payload=dict(row.payload or {}),
        headers=dict(row.headers or {}),
        delivery_metadata=dict(row.delivery_metadata or {}),
        status=str(row.status),
        attempts=int(row.attempts or 0),
        max_attempts=int(row.max_attempts or 0),
        available_at=row.available_at,
        claimed_at=row.claimed_at,
        claim_owner=row.claim_owner,
        published_at=row.published_at,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        failure_class=getattr(row, "failure_class", None),
        redrive_generation=int(getattr(row, "redrive_generation", 0) or 0),
        redrive_audit=list(getattr(row, "redrive_audit", None) or []),
    )


__all__ = ["OutboxRecord", "OutboxRedriveResult", "OutboxStore"]
