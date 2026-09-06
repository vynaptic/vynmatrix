"""Migrations 0105 and 0106 retire the consumer-less topics' rows idempotently."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from lib_application.db.models import Base, OutboxEvent

_VERSIONS = Path(__file__).resolve().parents[1] / "scripts/db/alembic/versions"
_MIGRATION = _VERSIONS / "0105_retire_observational_outbox_topics.py"
_DEAD_LETTER_MIGRATION = _VERSIONS / "0106_retire_dead_lettered_outbox_topics.py"


def _load_migration(path: Path = _MIGRATION):
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem[:4]}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_twice(engine, module) -> None:
    for _ in range(2):  # second run must change nothing further
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                module.upgrade()


def _row(event_key: str, topic: str, status: str) -> OutboxEvent:
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    return OutboxEvent(
        topic=topic,
        event_type="Legacy",
        schema_version="v1",
        payload={},
        headers={},
        event_key=event_key,
        status=status,
        attempts=1,
        max_attempts=10,
        available_at=now,
        created_at=now,
        updated_at=now,
    )


def test_upgrade_marks_only_undelivered_retired_rows_published_and_is_idempotent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                _row("k-pending", "signals.scored", "pending"),
                _row("k-failed", "feedback.ready", "failed"),
                _row("k-inflight", "execution.results", "in_progress"),
                _row("k-done", "signals.ingested", "published"),
                _row("k-live", "execution.commands", "pending"),
                _row("k-dead", "signals.scored", "dead_letter"),
            ]
        )
        session.commit()

    _upgrade_twice(engine, _load_migration())

    with Session(engine) as session:
        rows = {row.event_key: row for row in session.scalars(select(OutboxEvent)).all()}
    for key in ("k-pending", "k-failed", "k-inflight"):
        assert rows[key].status == "published"
        assert rows[key].claim_owner is None
        assert rows[key].delivery_metadata["publisher"] == "retired"
    assert rows["k-done"].status == "published"
    assert rows["k-done"].delivery_metadata is None
    assert rows["k-live"].status == "pending"
    assert rows["k-dead"].status == "dead_letter"  # left for 0106


def test_0106_retires_only_dead_letters_of_retired_topics_and_is_idempotent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        dead = [
            _row(f"dl-{topic}", topic, "dead_letter")
            for topic in (
                "signals.ingested",
                "signals.scored",
                "execution.results",
                "feedback.ready",
            )
        ]
        for row in dead:
            row.claim_owner = "scoring-outbox-relay"
            row.last_error = "no consumer"
            row.failure_class = "permanent"
        session.add_all(
            [
                *dead,
                _row("dl-live", "execution.commands", "dead_letter"),
                _row("k-done", "signals.scored", "published"),
            ]
        )
        session.commit()

    _upgrade_twice(engine, _load_migration(_DEAD_LETTER_MIGRATION))

    with Session(engine) as session:
        rows = {row.event_key: row for row in session.scalars(select(OutboxEvent)).all()}
    for topic in ("signals.ingested", "signals.scored", "execution.results", "feedback.ready"):
        row = rows[f"dl-{topic}"]
        assert row.status == "published"
        assert row.published_at is not None
        assert row.claim_owner is None
        assert row.claimed_at is None
        assert row.delivery_metadata == {
            "publisher": "retired",
            "revision": "0106_retire_topic_dead_letters",
        }
        assert row.last_error == "no consumer"  # failure evidence is kept
        assert row.failure_class == "permanent"
    assert rows["dl-live"].status == "dead_letter"  # execution dead letters stay actionable
    assert rows["k-done"].delivery_metadata is None
