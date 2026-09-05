from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from lib_application.db.models import Base, OutboxEvent
from lib_application.outbox import OutboxStore


def _store() -> tuple[OutboxStore, sessionmaker]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine, expire_on_commit=False)
    return OutboxStore(session_local), session_local


def test_outbox_enqueue_is_idempotent_by_event_key() -> None:
    store, session_local = _store()

    first_id = store.enqueue(
        topic="signals.scored",
        event_type="ScoredSignal",
        payload={"signal_id": "sig-1"},
        event_key="score:sig-1",
    )
    second_id = store.enqueue(
        topic="signals.scored",
        event_type="ScoredSignal",
        payload={"signal_id": "sig-1"},
        event_key="score:sig-1",
    )

    assert first_id == second_id
    with session_local() as session:
        assert session.query(OutboxEvent).count() == 1


def test_outbox_claim_publish_and_dead_letter() -> None:
    store, session_local = _store()
    event_id = store.enqueue(
        topic="execution.commands",
        event_type="ExecutionCommand",
        payload={"signal_id": "sig-2"},
        event_key="exec:sig-2",
        max_attempts=1,
    )

    claimed = store.claim_batch(topics=["execution.commands"], consumer="relay", limit=10)
    assert [record.event_id for record in claimed] == [event_id]
    assert claimed[0].status == "in_progress"
    assert claimed[0].attempts == 1

    store.mark_failed(
        event_id,
        expected_claim_owner=claimed[0].claim_owner or "",
        expected_attempts=claimed[0].attempts,
        error_message="boom",
        retry_delay_seconds=1,
    )
    with session_local() as session:
        row = session.get(OutboxEvent, event_id)
        assert row is not None
        assert row.status == "dead_letter"
        assert row.last_error == "boom"


def test_dead_letter_redrive_is_fenced_audited_and_preserves_identity() -> None:
    store, session_local = _store()
    payload = {
        "signal_id": "sig-redrive",
        "binding_id": 42,
        "economic_identity": "decision-42",
    }
    event_key = "exec:sig-redrive:42"
    event_id = store.enqueue(
        topic="execution.commands",
        event_type="ExecutionCommand",
        payload=payload,
        event_key=event_key,
        ordering_key="account:7:BTC-USDC",
        max_attempts=5,
    )
    claimed = store.claim_batch(
        topics=["execution.commands"],
        consumer="relay-a",
        limit=1,
    )[0]
    assert store.mark_failed(
        event_id,
        expected_claim_owner=claimed.claim_owner or "",
        expected_attempts=claimed.attempts,
        error_message="contract rejected",
        failure_class="permanent",
    )

    dead_letters = store.list_dead_letters(topics=["execution.commands"])
    assert [record.event_id for record in dead_letters] == [event_id]
    assert dead_letters[0].failure_class == "permanent"

    first = store.redrive_dead_letter(
        event_id,
        actor="operator@example.invalid",
        reason="binding contract corrected",
        expected_generation=0,
    )
    competing = store.redrive_dead_letter(
        event_id,
        actor="second-operator@example.invalid",
        reason="concurrent duplicate request",
        expected_generation=0,
    )

    assert first.acquired is True
    assert first.generation == 1
    assert first.outcome == "queued"
    assert competing.acquired is False
    assert competing.generation == 1
    with session_local() as session:
        row = session.get(OutboxEvent, event_id)
        assert row is not None
        assert row.event_id == event_id
        assert row.event_key == event_key
        assert row.payload == payload
        assert row.ordering_key == "account:7:BTC-USDC"
        assert row.status == "pending"
        assert row.attempts == 0
        assert row.failure_class is None
        assert row.redrive_generation == 1
        assert [entry["outcome"] for entry in row.redrive_audit] == [
            "queued",
            "rejected_state",
        ]

    requeued = store.claim_batch(
        topics=["execution.commands"],
        consumer="relay-b",
        limit=1,
    )
    assert [record.event_id for record in requeued] == [event_id]


def _seed_statuses(
    store: OutboxStore, session_local: sessionmaker, rows: list[tuple[str, str, str]]
) -> None:
    # rows: (event_key, topic, status). enqueue handles required defaults; then
    # set the target status directly for full control.
    for key, topic, status in rows:
        eid = store.enqueue(topic=topic, event_type="X", payload={}, event_key=key)
        with session_local() as session:
            row = session.get(OutboxEvent, eid)
            row.status = status
            session.commit()


def test_backlog_counts_excludes_published_and_filters_by_topic() -> None:
    store, session_local = _store()
    _seed_statuses(
        store,
        session_local,
        [
            ("k1", "execution.commands", "pending"),
            ("k2", "execution.commands", "failed"),
            ("k3", "execution.commands", "dead_letter"),
            ("k4", "execution.commands", "published"),  # excluded (delivered)
            ("k5", "signals.scored", "pending"),  # other topic
        ],
    )

    counts = store.backlog_counts(topics=["execution.commands"])
    assert counts == {"pending": 1, "failed": 1, "dead_letter": 1}


def test_backlog_counts_all_topics_when_unfiltered() -> None:
    store, session_local = _store()
    _seed_statuses(
        store,
        session_local,
        [
            ("k1", "execution.commands", "pending"),
            ("k2", "signals.scored", "pending"),
            ("k3", "signals.scored", "published"),  # excluded
        ],
    )

    counts = store.backlog_counts()
    assert counts == {"pending": 2}

    publish_id = store.enqueue(
        topic="execution.results",
        event_type="ExecutionResult",
        payload={"signal_id": "sig-3"},
        event_key="result:sig-3",
    )
    claimed = store.claim_batch(topics=["execution.results"], consumer="relay", limit=10)
    assert [record.event_id for record in claimed] == [publish_id]

    store.mark_published(
        publish_id,
        expected_claim_owner=claimed[0].claim_owner or "",
        expected_attempts=claimed[0].attempts,
        delivery_metadata={"backend": "noop"},
    )
    with session_local() as session:
        row = session.get(OutboxEvent, publish_id)
        assert row is not None
        assert row.status == "published"
        assert row.delivery_metadata == {"backend": "noop"}


def test_outbox_enqueue_recovers_from_event_key_integrity_race(monkeypatch) -> None:
    store, session_local = _store()
    original_commit = session_local.class_.commit
    state = {"raised": False}

    def _commit(self):  # type: ignore[no-untyped-def]
        if not state["raised"]:
            pending = next(iter(self.new))
            if isinstance(pending, OutboxEvent):
                existing = OutboxEvent(
                    topic=pending.topic,
                    event_type=pending.event_type,
                    schema_version=pending.schema_version,
                    aggregate_type=pending.aggregate_type,
                    aggregate_id=pending.aggregate_id,
                    event_key=pending.event_key,
                    ordering_key=pending.ordering_key,
                    payload=dict(pending.payload or {}),
                    headers=dict(pending.headers or {}),
                    status="pending",
                    attempts=0,
                    max_attempts=pending.max_attempts,
                    available_at=pending.available_at,
                )
                with session_local() as other:
                    other.add(existing)
                    original_commit(other)
                state["raised"] = True
                raise IntegrityError("insert", {}, Exception("duplicate event_key"))
        return original_commit(self)

    monkeypatch.setattr(session_local.class_, "commit", _commit)

    event_id = store.enqueue(
        topic="signals.scored",
        event_type="ScoredSignal",
        payload={"signal_id": "sig-race"},
        event_key="score:sig-race",
    )

    with session_local() as session:
        rows = session.query(OutboxEvent).filter_by(event_key="score:sig-race").all()
        assert len(rows) == 1
        assert str(rows[0].event_id) == event_id
