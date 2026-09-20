"""Unit tests for the deployment record's open/close contract, on SQLite."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from dev_cli.core.deployment import record
from lib_application.db.models import Base, Deployment


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Deployment.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    with factory() as active:
        yield active
    engine.dispose()
    assert Base.metadata.tables["deployments"] is Deployment.__table__


def _open(session: Session, **overrides: object) -> int:
    fields = {
        "mode": record.UPGRADE,
        "image_tag": "sha-0123456789ab",
        "image_digest": "sha256:abc",
        "source_commit": "0123456789abcdef",
        "source_dirty": False,
        "alembic_head": "0108_deployment_record",
        "snapshot_path": Path("/tmp/snapshot.dump"),
        "snapshot_reason": None,
    }
    fields.update(overrides)
    return record.open_record(session, **fields)  # type: ignore[arg-type]


def test_a_run_is_recorded_as_failed_until_it_finishes(session: Session) -> None:
    deployment_id = _open(session)

    row = record.latest(session)

    assert row is not None
    assert row["deployment_id"] == deployment_id
    # An interrupted run leaves a visibly incomplete record, not no record.
    assert row["outcome"] == record.FAILED
    assert row["finished_at"] is None
    assert row["snapshot_path"] == "/tmp/snapshot.dump"


def test_closing_records_the_outcome_and_the_time(session: Session) -> None:
    deployment_id = _open(session)

    record.close_record(session, deployment_id, outcome=record.SUCCEEDED)

    row = record.latest(session)
    assert row is not None
    assert row["outcome"] == record.SUCCEEDED
    assert row["finished_at"] is not None
    assert row["finished_at"] >= row["started_at"]


def test_a_rollback_keeps_the_stage_that_failed(session: Session) -> None:
    deployment_id = _open(session)

    record.close_record(
        session, deployment_id, outcome=record.ROLLED_BACK, failure_stage="provision"
    )

    row = record.latest(session)
    assert row is not None
    assert (row["outcome"], row["failure_stage"]) == (record.ROLLED_BACK, "provision")


def test_the_latest_successful_record_is_the_rollback_target(session: Session) -> None:
    first = _open(session, image_tag="sha-aaaaaaaaaaaa")
    record.close_record(session, first, outcome=record.SUCCEEDED)
    second = _open(session, image_tag="sha-bbbbbbbbbbbb")
    record.close_record(session, second, outcome=record.ROLLED_BACK, failure_stage="verify")

    assert record.latest(session)["image_tag"] == "sha-bbbbbbbbbbbb"
    assert record.latest(session, outcome=record.SUCCEEDED)["image_tag"] == "sha-aaaaaaaaaaaa"


def test_a_deployment_without_a_snapshot_must_say_why(session: Session) -> None:
    with pytest.raises(ValueError, match="must record why"):
        _open(session, snapshot_path=None, snapshot_reason=None)

    deployment_id = _open(
        session, mode=record.INSTALL, snapshot_path=None, snapshot_reason="fresh install"
    )
    assert record.latest(session)["snapshot_reason"] == "fresh install"
    record.close_record(session, deployment_id, outcome=record.SUCCEEDED)


def test_an_unsupported_outcome_is_refused(session: Session) -> None:
    deployment_id = _open(session)

    with pytest.raises(ValueError, match="Unsupported deployment outcome"):
        record.close_record(session, deployment_id, outcome="probably-fine")


def test_a_database_without_the_table_is_detected(session: Session) -> None:
    assert record.table_exists(session) is True

    engine = create_engine("sqlite+pysqlite:///:memory:")
    with sessionmaker(bind=engine)() as empty:
        assert record.table_exists(empty) is False
    engine.dispose()
