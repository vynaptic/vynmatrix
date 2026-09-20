"""The deployment lifecycle against a real PostgreSQL, on a disposable database.

Requires DEPLOYMENT_LIFECYCLE_TEST_ALLOW_CREATE=1 plus explicit admin and target
URLs in DEPLOYMENT_LIFECYCLE_TEST_ADMIN_DATABASE_URL and
DEPLOYMENT_LIFECYCLE_TEST_DATABASE_URL. The target name must start with
``deployment_lifecycle_test``; the module creates and drops exactly that
database. Never run it against the normal development cluster.

What needs PostgreSQL, and is therefore here rather than in the unit tests: that
migrating from zero produces the record table with its constraints, that those
constraints actually refuse an unaccounted snapshot, and that a forced failure
after the migration is recoverable from the snapshot with the schema head and
the data exactly as they were.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from psycopg2 import sql
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from dev_cli.core.deployment import record
from lib_application.db.session import create_engine_for_env, dispose_engine, get_session_factory

pytestmark = pytest.mark.integration
_ROOT = Path(__file__).resolve().parents[1]
_PREFIX = "deployment_lifecycle_test"
_HEAD = "0108_deployment_record"


def _admin_sql(url: str, statement: sql.Composable) -> None:
    engine = create_engine_for_env(env="dev", db_url=url)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            driver = connection.connection.driver_connection
            assert driver is not None
            with driver.cursor() as cursor:
                cursor.execute(statement)
    finally:
        dispose_engine(engine)


@pytest.fixture
def scratch_url() -> Iterator[str]:
    if os.environ.get("DEPLOYMENT_LIFECYCLE_TEST_ALLOW_CREATE") != "1":
        pytest.skip("This module creates a database and must be authorized explicitly")
    admin = os.environ.get("DEPLOYMENT_LIFECYCLE_TEST_ADMIN_DATABASE_URL")
    target = os.environ.get("DEPLOYMENT_LIFECYCLE_TEST_DATABASE_URL")
    if not admin or not target:
        pytest.fail("Both explicit disposable-database URLs are required")
    name = str(make_url(target).database)
    if not name.startswith(_PREFIX):
        pytest.fail(f"Use an explicit {_PREFIX}-prefixed disposable target")
    _admin_sql(admin, sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
    _admin_sql(admin, sql.SQL("CREATE DATABASE {} ENCODING 'UTF8'").format(sql.Identifier(name)))
    try:
        yield target
    finally:
        _admin_sql(admin, sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def _upgrade(url: str) -> str:
    """Build the schema from zero exactly as a fresh install does."""
    from dev_cli.core.bootstrap import migrate

    engine = create_engine_for_env(env="dev", db_url=url)
    try:
        with engine.connect() as connection, connection.begin():
            return migrate(connection, _ROOT)
    finally:
        dispose_engine(engine)


@pytest.fixture
def migrated(scratch_url: str) -> Iterator[tuple[str, Session]]:
    head = _upgrade(scratch_url)
    assert head == _HEAD
    engine = create_engine_for_env(env="dev", db_url=scratch_url)
    try:
        with get_session_factory(engine=engine)() as session:
            yield scratch_url, session
    finally:
        dispose_engine(engine)


def test_migrating_from_zero_creates_the_record_table(migrated: tuple[str, Session]) -> None:
    _, session = migrated

    columns = set(
        session.scalars(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='deployments'"
            )
        )
    )
    constraints = set(
        session.scalars(
            text(
                "SELECT conname FROM pg_catalog.pg_constraint "
                "WHERE conrelid='public.deployments'::regclass AND contype='c'"
            )
        )
    )

    assert {"deployment_id", "image_tag", "source_commit", "outcome", "snapshot_path"} <= columns
    assert {
        "ck_deployment_outcome",
        "ck_deployment_mode",
        "ck_deployment_snapshot_accounted",
    } <= constraints
    assert session.scalar(text("SELECT version_num FROM alembic_version")) == _HEAD


def test_the_table_carries_no_row_security_and_one_read_grant(
    migrated: tuple[str, Session],
) -> None:
    _, session = migrated

    # The record describes the installation, not a user; like prices and
    # canonical signals in 0107 it is not row-secured.
    assert (
        session.scalar(
            text("SELECT relrowsecurity FROM pg_class WHERE oid='public.deployments'::regclass")
        )
        is False
    )
    privileges = set(
        session.scalars(
            text(
                "SELECT DISTINCT privilege_type FROM information_schema.column_privileges "
                "WHERE table_name='deployments' AND grantee='vm_backend'"
            )
        )
    )
    # The role only exists on a cluster where roles were provisioned.
    assert privileges <= {"SELECT"}


def test_postgresql_refuses_a_deployment_that_does_not_account_for_its_snapshot(
    migrated: tuple[str, Session],
) -> None:
    _, session = migrated

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO deployments (mode, image_tag, image_digest, source_commit, "
                "source_dirty, alembic_head, started_at, outcome) VALUES "
                "('upgrade', 'sha-a', 'sha256:a', 'abc', false, :head, now(), 'failed')"
            ),
            {"head": _HEAD},
        )
    session.rollback()

    with pytest.raises(IntegrityError):
        record.open_record(
            session,
            mode="teleport",
            image_tag="sha-a",
            image_digest="sha256:a",
            source_commit="abc",
            source_dirty=False,
            alembic_head=_HEAD,
            snapshot_path=None,
            snapshot_reason="none taken",
        )
    session.rollback()


@pytest.mark.skipif(
    shutil.which("pg_dump") is None or shutil.which("pg_restore") is None,
    reason="the PostgreSQL client tools are required for the snapshot round trip",
)
def test_a_forced_failure_after_the_migration_is_recoverable_from_the_snapshot(
    migrated: tuple[str, Session], tmp_path: Path
) -> None:
    url, session = migrated
    parsed = make_url(url)
    deployment_id = record.open_record(
        session,
        mode=record.UPGRADE,
        image_tag="sha-000000000001",
        image_digest="sha256:one",
        source_commit="0" * 40,
        source_dirty=False,
        alembic_head=_HEAD,
        snapshot_path=tmp_path / "snapshot.dump",
        snapshot_reason=None,
    )
    record.close_record(session, deployment_id, outcome=record.SUCCEEDED)
    session.execute(
        text(
            "INSERT INTO strategies (strategy_id, strategy_name, asset_class) VALUES (:id, :n, :a)"
        ),
        {"id": "history_v1", "n": "History", "a": "crypto"},
    )
    session.commit()

    environment = {**os.environ, "PGPASSWORD": str(parsed.password)}
    connection = [
        "--host",
        str(parsed.host),
        "--port",
        str(parsed.port or 5432),
        "--username",
        str(parsed.username),
        "--dbname",
        str(parsed.database),
    ]
    snapshot = tmp_path / "snapshot.dump"
    with snapshot.open("wb") as stream:
        assert (
            subprocess.run(
                ["pg_dump", "--format=custom", *connection],
                env=environment,
                stdout=stream,
                check=False,
            ).returncode
            == 0
        )
    assert snapshot.read_bytes()[:5] == b"PGDMP"

    # The forced failure: the upgrade got past the migration and destroyed data.
    session.execute(text("DELETE FROM strategies WHERE strategy_id = 'history_v1'"))
    session.execute(text("UPDATE alembic_version SET version_num = '0107_backend_ui_read'"))
    session.commit()
    failed = record.open_record(
        session,
        mode=record.UPGRADE,
        image_tag="sha-000000000002",
        image_digest="sha256:two",
        source_commit="2" * 40,
        source_dirty=False,
        alembic_head=_HEAD,
        snapshot_path=snapshot,
        snapshot_reason=None,
    )
    session.close()

    with snapshot.open("rb") as stream:
        restored = subprocess.run(
            [
                "pg_restore",
                "--exit-on-error",
                "--single-transaction",
                "--clean",
                "--if-exists",
                "--no-owner",
                *connection,
            ],
            env=environment,
            stdin=stream,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
    assert restored.returncode == 0, restored.stderr.decode("utf-8", "replace")[-2000:]

    engine = create_engine_for_env(env="dev", db_url=url)
    try:
        with get_session_factory(engine=engine)() as after:
            # The schema head, the history and the record are exactly as they were.
            assert after.scalar(text("SELECT version_num FROM alembic_version")) == _HEAD
            assert (
                after.scalar(
                    text("SELECT count(*) FROM strategies WHERE strategy_id = 'history_v1'")
                )
                == 1
            )
            assert record.latest(after, outcome=record.SUCCEEDED)["image_tag"] == "sha-000000000001"
            # The failed attempt is not in the restored snapshot; it is reopened
            # and closed as rolled back so the history stays honest.
            assert (
                after.scalar(
                    text("SELECT count(*) FROM deployments WHERE deployment_id = :id"),
                    {"id": failed},
                )
                == 0
            )
            reopened = record.open_record(
                after,
                mode=record.UPGRADE,
                image_tag="sha-000000000002",
                image_digest="sha256:two",
                source_commit="2" * 40,
                source_dirty=False,
                alembic_head=_HEAD,
                snapshot_path=snapshot,
                snapshot_reason=None,
            )
            record.close_record(
                after, reopened, outcome=record.ROLLED_BACK, failure_stage="provision"
            )
            latest = record.latest(after)
            assert (latest["outcome"], latest["failure_stage"]) == (
                record.ROLLED_BACK,
                "provision",
            )
    finally:
        dispose_engine(engine)


def test_an_unmigrated_database_is_detected_rather_than_assumed(scratch_url: str) -> None:
    engine = create_engine_for_env(env="dev", db_url=scratch_url)
    try:
        with get_session_factory(engine=engine)() as session:
            assert record.table_exists(session) is False
            with pytest.raises(ProgrammingError):
                session.execute(text("SELECT 1 FROM deployments"))
    finally:
        dispose_engine(engine)
