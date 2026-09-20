"""Contract tests for the deployment record's schema and its read grant (0108)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0108_deployment_record.py"
)
#: Maintenance facts the owner UI has no reason to read.
_NEVER_READABLE = {"snapshot_path", "snapshot_reason", "failure_stage", "image_digest"}


class _RecordingOp:
    """Captures what the migration would do, without a database."""

    def __init__(self, dialect: str = "postgresql") -> None:
        self.dialect = dialect
        self.statements: list[str] = []
        self.tables: list[tuple[str, tuple[Any, ...]]] = []
        self.indexes: list[tuple[str, str, list[str]]] = []
        self.dropped: list[str] = []

    def get_bind(self) -> Any:
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))

    def create_table(self, name: str, *columns: Any) -> None:
        self.tables.append((name, columns))

    def create_index(self, name: str, table: str, columns: list[str], **_: Any) -> None:
        self.indexes.append((name, table, columns))

    def drop_index(self, name: str, **_: Any) -> None:
        self.dropped.append(name)

    def drop_table(self, name: str) -> None:
        self.dropped.append(name)

    @staticmethod
    def f(name: str) -> str:
        return name


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("deployment_record", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(direction: str, dialect: str = "postgresql") -> tuple[Any, _RecordingOp]:
    migration = _load()
    recorder = _RecordingOp(dialect)
    migration.op = recorder  # type: ignore[attr-defined]
    getattr(migration, direction)()
    return migration, recorder


def test_the_revision_follows_the_owner_ui_read_surface() -> None:
    migration = _load()

    assert migration.revision == "0108_deployment_record"
    # ``alembic_version.version_num`` is varchar(32).
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0107_backend_ui_read"


def test_the_table_records_what_was_deployed_and_how_it_ended() -> None:
    _, recorder = _run("upgrade")

    (name, columns) = recorder.tables[0]
    assert name == "deployments"
    declared = {column.name for column in columns if isinstance(column, sa.Column)}
    assert declared == {
        "deployment_id",
        "mode",
        "image_tag",
        "image_digest",
        "source_commit",
        "source_dirty",
        "alembic_head",
        "started_at",
        "finished_at",
        "outcome",
        "failure_stage",
        "snapshot_path",
        "snapshot_reason",
    }
    constraints = {
        constraint.name for constraint in columns if isinstance(constraint, sa.CheckConstraint)
    }
    assert constraints == {
        "ck_deployment_outcome",
        "ck_deployment_mode",
        "ck_deployment_finished_after_start",
        # A missing snapshot must always carry the reason it is missing.
        "ck_deployment_snapshot_accounted",
    }
    assert recorder.indexes == [("ix_deployments_started_at", "deployments", ["started_at"])]


def test_the_backend_gets_column_scoped_select_and_nothing_else() -> None:
    migration, recorder = _run("upgrade")

    grants = [statement for statement in recorder.statements if statement.startswith("GRANT")]
    assert grants == [
        f"GRANT SELECT ({', '.join(migration.READ_COLUMNS)}) ON TABLE public.deployments TO vm_backend"
    ]
    for statement in recorder.statements:
        for forbidden in ("INSERT", "UPDATE", "DELETE", "SEQUENCE", "ALL PRIVILEGES", "BYPASSRLS"):
            assert forbidden not in statement, statement
    assert not _NEVER_READABLE & set(migration.READ_COLUMNS)


def test_the_outcomes_and_modes_are_the_ones_the_tool_writes() -> None:
    from dev_cli.core.deployment import record

    migration = _load()

    assert set(migration.OUTCOMES) == {record.SUCCEEDED, record.ROLLED_BACK, record.FAILED}
    assert set(migration.MODES) == {record.INSTALL, record.UPGRADE}
    assert set(migration.READ_COLUMNS) <= set(record.PUBLIC_FIELDS)


def test_downgrade_removes_exactly_what_upgrade_added() -> None:
    migration, recorder = _run("downgrade")

    assert recorder.dropped == ["ix_deployments_started_at", "deployments"]
    assert recorder.statements == [
        f"REVOKE SELECT ({', '.join(migration.READ_COLUMNS)}) "
        "ON TABLE public.deployments FROM vm_backend"
    ]


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_the_table_exists_outside_postgresql_but_the_grant_does_not(direction: str) -> None:
    _, recorder = _run(direction, "sqlite")

    # SQLite carries the table for unit tests; roles and grants are PostgreSQL only.
    assert recorder.statements == []
    assert recorder.tables or recorder.dropped
