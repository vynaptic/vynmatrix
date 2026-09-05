"""Failure-mode tests for ``OutboxStore``.

These complement ``test_outbox_store.py`` (idempotency + happy-path
DLQ) by covering the recovery semantics that production relies on:

* lease expiry → re-claim by another consumer,
* dead-letter rows are excluded from subsequent claims,
* ``mark_failed`` schedules ``available_at`` into the future so the
  next claim respects the backoff window,
* ``mark_failed`` flips to ``dead_letter`` only once
  ``attempts >= max_attempts``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from lib_application.db.models import Base, OutboxEvent
from lib_application.outbox import OutboxStore


def _store() -> tuple[OutboxStore, sessionmaker]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine, expire_on_commit=False)
    return OutboxStore(session_local), session_local


def test_lease_expiry_allows_reclaim_by_another_consumer() -> None:
    """A claimed-but-not-completed row is re-claimable once its lease expires."""
    store, _ = _store()
    event_id = store.enqueue(
        topic="execution.commands",
        event_type="ExecutionCommand",
        payload={"signal_id": "sig-lease"},
        event_key="exec:sig-lease",
    )

    first = store.claim_batch(
        topics=["execution.commands"],
        consumer="worker-A",
        limit=1,
        lease_seconds=1,
    )
    assert [r.event_id for r in first] == [event_id]

    # Within the lease window the row is invisible to other consumers.
    locked = store.claim_batch(
        topics=["execution.commands"],
        consumer="worker-B",
        limit=1,
        lease_seconds=1,
    )
    assert locked == []

    # Sleep past the 1s lease and re-claim. The replacement consumer must
    # use a lease window <= the elapsed time so its ``stale_cutoff`` covers
    # the abandoned claim — production deployments share the same
    # ``lease_seconds`` across workers, so this models the case where one
    # worker died after acquiring the lease.
    time.sleep(1.2)
    second = store.claim_batch(
        topics=["execution.commands"],
        consumer="worker-B",
        limit=1,
        lease_seconds=1,
    )
    assert [r.event_id for r in second] == [event_id]
    # Attempts incremented on each claim so ``mark_failed`` can DLQ correctly.
    assert second[0].attempts == 2


def test_mark_failed_pushes_available_at_into_the_future() -> None:
    """Failed rows respect the backoff window before the next claim."""
    store, session_local = _store()
    event_id = store.enqueue(
        topic="execution.commands",
        event_type="ScoredSignal",
        payload={"signal_id": "sig-backoff"},
        event_key="score:sig-backoff",
        max_attempts=5,
    )

    claimed = store.claim_batch(
        topics=["execution.commands"], consumer="worker", limit=1, lease_seconds=60
    )
    assert claimed
    before = datetime.now(tz=UTC)
    store.mark_failed(
        event_id,
        expected_claim_owner=claimed[0].claim_owner or "",
        expected_attempts=claimed[0].attempts,
        error_message="503",
        retry_delay_seconds=120,
    )

    with session_local() as session:
        row = session.get(OutboxEvent, event_id)
        assert row is not None
        # Status is "failed" (not dead_letter) — attempts (1) < max_attempts (5).
        assert row.status == "failed"
        assert row.last_error == "503"
        # SQLite returns naive datetimes; normalise both sides to compare.
        # ``available_at`` is roughly ``before + 120s`` — the floor is what
        # matters: it must be far enough in the future to enforce backoff.
        available_at = row.available_at
        if available_at.tzinfo is None:
            available_at = available_at.replace(tzinfo=UTC)
        assert available_at >= before + timedelta(seconds=115)

    # A subsequent claim within the backoff window finds nothing.
    locked = store.claim_batch(
        topics=["execution.commands"], consumer="worker", limit=1, lease_seconds=60
    )
    assert locked == []


def test_dead_lettered_rows_are_not_reclaimed() -> None:
    """Once ``status == 'dead_letter'`` the row is invisible to ``claim_batch``."""
    store, session_local = _store()
    event_id = store.enqueue(
        topic="execution.commands",
        event_type="ExecutionCommand",
        payload={"signal_id": "sig-dlq-skip"},
        event_key="exec:sig-dlq-skip",
        max_attempts=1,  # First failure will DLQ immediately.
    )

    claimed = store.claim_batch(
        topics=["execution.commands"], consumer="worker", limit=1, lease_seconds=60
    )
    store.mark_failed(
        event_id,
        expected_claim_owner=claimed[0].claim_owner or "",
        expected_attempts=claimed[0].attempts,
        error_message="boom",
        retry_delay_seconds=0,
    )

    with session_local() as session:
        row = session.get(OutboxEvent, event_id)
        assert row is not None
        assert row.status == "dead_letter"

    # Sleep past the (zero) backoff so ``available_at`` does not hide the row.
    time.sleep(0.05)
    reclaimed = store.claim_batch(
        topics=["execution.commands"], consumer="worker", limit=10, lease_seconds=60
    )
    assert reclaimed == []


def test_mark_failed_promotes_to_dead_letter_only_at_max_attempts() -> None:
    """``mark_failed`` keeps status = 'failed' until attempts hits the cap."""
    store, session_local = _store()
    event_id = store.enqueue(
        topic="execution.commands",
        event_type="ScoredSignal",
        payload={"signal_id": "sig-attempts"},
        event_key="score:sig-attempts",
        max_attempts=3,
    )

    for expected_attempts, expected_status in [(1, "failed"), (2, "failed"), (3, "dead_letter")]:
        # Each claim increments ``attempts`` so subsequent ``mark_failed`` can
        # branch on it. Use a 0-second backoff so the row immediately becomes
        # claimable again on the next loop iteration.
        claimed = store.claim_batch(
            topics=["execution.commands"], consumer="worker", limit=1, lease_seconds=60
        )
        assert claimed
        assert claimed[0].attempts == expected_attempts
        store.mark_failed(
            event_id,
            expected_claim_owner=claimed[0].claim_owner or "",
            expected_attempts=claimed[0].attempts,
            error_message="boom",
            retry_delay_seconds=0,
        )

        with session_local() as session:
            row = session.get(OutboxEvent, event_id)
            assert row is not None
            assert int(row.attempts or 0) == expected_attempts
            assert row.status == expected_status

        # Sleep so ``available_at`` rolls past now (retry_delay_seconds=0
        # still bumps available_at by ~1s due to the floor in ``mark_failed``).
        time.sleep(1.05)


def test_list_pending_excludes_published_and_dead_letter_rows() -> None:
    """Operators inspecting the queue should see only in-flight work."""
    store, _ = _store()
    queued_id = store.enqueue(
        topic="execution.commands",
        event_type="ExecutionCommand",
        payload={"signal_id": "sig-queued"},
        event_key="exec:sig-queued",
    )
    dlq_id = store.enqueue(
        topic="execution.commands",
        event_type="ExecutionCommand",
        payload={"signal_id": "sig-dlq"},
        event_key="exec:sig-dlq",
        max_attempts=1,
    )
    published_id = store.enqueue(
        topic="execution.commands",
        event_type="ExecutionCommand",
        payload={"signal_id": "sig-published"},
        event_key="exec:sig-published",
    )

    # Move dlq_id into dead_letter.
    claimed = store.claim_batch(
        topics=["execution.commands"], consumer="worker", limit=10, lease_seconds=60
    )
    by_id = {record.event_id: record for record in claimed}
    store.mark_failed(
        dlq_id,
        expected_claim_owner=by_id[dlq_id].claim_owner or "",
        expected_attempts=by_id[dlq_id].attempts,
        error_message="boom",
        retry_delay_seconds=0,
    )
    # Move published_id into published.
    store.mark_published(
        published_id,
        expected_claim_owner=by_id[published_id].claim_owner or "",
        expected_attempts=by_id[published_id].attempts,
        delivery_metadata={"backend": "noop"},
    )

    pending = store.list_pending(topics=["execution.commands"], limit=100)
    pending_ids = {record.event_id for record in pending}

    assert queued_id in pending_ids
    assert dlq_id not in pending_ids
    assert published_id not in pending_ids

    operational = store.list_pending(
        topics=["execution.commands"],
        limit=100,
        include_dead_letter=True,
    )
    operational_ids = {record.event_id for record in operational}
    assert queued_id in operational_ids
    assert dlq_id in operational_ids
    assert published_id not in operational_ids


def test_stale_worker_cannot_complete_a_reclaimed_event() -> None:
    """A timed-out claim cannot overwrite the result of its replacement worker."""
    store, session_local = _store()
    event_id = store.enqueue(
        topic="execution.commands",
        event_type="ExecutionCommand",
        payload={"signal_id": "sig-fenced"},
        event_key="exec:sig-fenced",
    )
    first = store.claim_batch(
        topics=["execution.commands"], consumer="worker-A", limit=1, lease_seconds=60
    )[0]
    with session_local() as session:
        row = session.get(OutboxEvent, event_id)
        assert row is not None
        row.claimed_at = datetime.now(tz=UTC) - timedelta(seconds=120)
        session.commit()

    second = store.claim_batch(
        topics=["execution.commands"], consumer="worker-B", limit=1, lease_seconds=60
    )[0]
    assert second.attempts == first.attempts + 1

    assert (
        store.mark_failed(
            event_id,
            expected_claim_owner=first.claim_owner or "",
            expected_attempts=first.attempts,
            error_message="late failure from worker-A",
        )
        is False
    )
    assert (
        store.mark_published(
            event_id,
            expected_claim_owner=second.claim_owner or "",
            expected_attempts=second.attempts,
            delivery_metadata={"worker": "B"},
        )
        is True
    )
    with session_local() as session:
        row = session.get(OutboxEvent, event_id)
        assert row is not None
        assert row.status == "published"
        assert row.attempts == 2
        assert row.delivery_metadata == {"worker": "B"}


def test_preserved_partition_order_stops_at_dead_letter() -> None:
    """A dead-lettered LONG must block a newer CLOSE in the same partition."""
    store, session_local = _store()
    long_id = store.enqueue(
        topic="signals.submit",
        event_type="StrategySignalSubmitted",
        payload={"action": "LONG"},
        event_key="strategy:long",
        ordering_key="strategy-worker",
    )
    close_id = store.enqueue(
        topic="signals.submit",
        event_type="StrategySignalSubmitted",
        payload={"action": "CLOSE"},
        event_key="strategy:close",
        ordering_key="strategy-worker",
    )
    other_id = store.enqueue(
        topic="signals.submit",
        event_type="StrategySignalSubmitted",
        payload={"action": "LONG"},
        event_key="other:long",
        ordering_key="other-worker",
    )
    with session_local() as session:
        long_row = session.get(OutboxEvent, long_id)
        close_row = session.get(OutboxEvent, close_id)
        other_row = session.get(OutboxEvent, other_id)
        assert long_row is not None
        assert close_row is not None
        assert other_row is not None
        base = datetime(2026, 7, 26, 12, 0)
        long_row.created_at = base
        close_row.created_at = base + timedelta(seconds=1)
        other_row.created_at = base + timedelta(seconds=2)
        long_row.status = "dead_letter"
        session.commit()

    claimed = store.claim_batch(
        topics=["signals.submit"],
        consumer="strategy-relay",
        limit=10,
        ordering_keys=["strategy-worker", "other-worker"],
        preserve_ordering=True,
    )

    assert [record.event_id for record in claimed] == [other_id]
    with session_local() as session:
        blocked = session.get(OutboxEvent, close_id)
        assert blocked is not None
        assert blocked.status == "pending"


def test_preserved_partition_order_requires_explicit_partition() -> None:
    store, _session_local = _store()

    with pytest.raises(ValueError, match="ordering_key"):
        store.claim_batch(
            topics=["signals.submit"],
            consumer="strategy-relay",
            preserve_ordering=True,
        )
