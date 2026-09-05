"""Contract tests for indicator source-lock migration 0081."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0081_indicator_source_lock.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "indicator_source_lock_migration",
        _MIGRATION_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


def test_revision_follows_execution_binding_read() -> None:
    migration = _load_migration()

    assert migration.revision == "0081_indicator_source_lock"
    assert migration.down_revision == "0080_execution_binding_read"


def test_indicator_source_lock_is_security_definer_and_execute_only() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration._create_postgresql_indicator_source_lock()

    sql = "\n".join(recorder.statements)
    assert "SECURITY DEFINER" in sql
    assert "SET search_path = pg_catalog, public" in sql
    assert "p_content_revision bigint" in sql
    assert "FOR SHARE" in sql
    assert "REVOKE ALL ON FUNCTION" in sql
    assert "FROM PUBLIC" in sql
    assert "GRANT EXECUTE ON FUNCTION" in sql
    assert "TO vm_indicator" in sql
