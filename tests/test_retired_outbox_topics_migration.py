"""Migration 0105 retires undelivered rows of the consumer-less topics idempotently."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from lib_application.db.models import Base, OutboxEvent

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "scripts/db/alembic/versions/0105_retire_observational_outbox_topics.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0105", _MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
            ]
        )
        session.commit()

    module = _load_migration()
    for _ in range(2):  # second run must change nothing further
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                module.upgrade()

    with Session(engine) as session:
        rows = {row.event_key: row for row in session.scalars(select(OutboxEvent)).all()}
    for key in ("k-pending", "k-failed", "k-inflight"):
        assert rows[key].status == "published"
        assert rows[key].claim_owner is None
        assert rows[key].delivery_metadata["publisher"] == "retired"
    assert rows["k-done"].status == "published"
    assert rows["k-done"].delivery_metadata is None
    assert rows["k-live"].status == "pending"
