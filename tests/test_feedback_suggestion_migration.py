"""Contract tests for feedback suggestion-only migration 0053."""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_PATH = (
    _ROOT / "scripts" / "db" / "alembic" / "versions" / "0053_feedback_suggestion_only.py"
)


class _RecordingOp:
    def __init__(self, dialect: str) -> None:
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def get_bind(self) -> Any:
        return self.bind

    def _record(self, operation: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((operation, args, kwargs))

    def execute(self, *args: Any, **kwargs: Any) -> None:
        self._record("execute", *args, **kwargs)

    def drop_constraint(self, *args: Any, **kwargs: Any) -> None:
        self._record("drop_constraint", *args, **kwargs)

    def drop_column(self, *args: Any, **kwargs: Any) -> None:
        self._record("drop_column", *args, **kwargs)

    def add_column(self, *args: Any, **kwargs: Any) -> None:
        self._record("add_column", *args, **kwargs)

    def create_check_constraint(self, *args: Any, **kwargs: Any) -> None:
        self._record("create_check_constraint", *args, **kwargs)

    @contextmanager
    def batch_alter_table(self, table: str) -> Iterator[_RecordingOp]:
        self._record("batch_alter_table", table)
        yield self


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("feedback_suggestion_migration", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_follows_service_role_rls() -> None:
    migration = _load_migration()

    assert migration.revision == "0053_feedback_suggestion_only"
    assert migration.down_revision == "0052_service_role_rls"


def test_postgresql_upgrade_preserves_review_then_removes_apply_surface() -> None:
    migration = _load_migration()
    recorder = _RecordingOp("postgresql")
    migration.op = recorder

    migration.upgrade()

    operations = [operation for operation, _, _ in recorder.calls]
    sql = str(recorder.calls[0][1][0])
    assert operations == [
        "execute",
        "drop_constraint",
        "drop_column",
        "drop_column",
        "create_check_constraint",
    ]
    assert "status = 'approved'" in sql
    assert "Historical runtime apply status retired" in sql
    assert recorder.calls[2][1] == ("strategy_parameter_feedback", "config_file_path")
    assert recorder.calls[3][1] == ("strategy_parameter_feedback", "applied_at")
    assert recorder.calls[4][1] == (
        "ck_param_feedback_status",
        "strategy_parameter_feedback",
        "status IN ('pending', 'approved', 'rejected', 'expired')",
    )


def test_sqlite_upgrade_uses_portable_review_preservation() -> None:
    migration = _load_migration()
    recorder = _RecordingOp("sqlite")
    migration.op = recorder

    migration.upgrade()

    sql = str(recorder.calls[0][1][0])
    assert "char(10)" in sql
    assert "concat_ws" not in sql
    assert ("drop_column", ("config_file_path",), {}) in recorder.calls
    assert ("drop_column", ("applied_at",), {}) in recorder.calls
    assert (
        "create_check_constraint",
        (
            "ck_param_feedback_status",
            "status IN ('pending', 'approved', 'rejected', 'expired')",
        ),
        {},
    ) in recorder.calls


def test_postgresql_downgrade_restores_only_legacy_schema_shape() -> None:
    migration = _load_migration()
    recorder = _RecordingOp("postgresql")
    migration.op = recorder

    migration.downgrade()

    operations = [operation for operation, _, _ in recorder.calls]
    assert operations == [
        "drop_constraint",
        "add_column",
        "add_column",
        "create_check_constraint",
    ]
    restored_columns = [call[1][1] for call in recorder.calls if call[0] == "add_column"]
    assert [(column.name, column.nullable) for column in restored_columns] == [
        ("config_file_path", True),
        ("applied_at", True),
    ]
    assert recorder.calls[-1][1] == (
        "ck_param_feedback_status",
        "strategy_parameter_feedback",
        "status IN ('pending', 'approved', 'rejected', 'applied', 'expired')",
    )
