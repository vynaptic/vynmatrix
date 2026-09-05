"""Contract tests for the least-privilege feedback catalogue gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_PATH = (
    _ROOT / "scripts" / "db" / "alembic" / "versions" / "0073_feedback_catalogue_gate.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "feedback_catalogue_gate_migration",
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


def test_revision_follows_feedback_runtime_integrity() -> None:
    migration = _load_migration()

    assert migration.revision == "0073_feedback_catalogue_gate"
    assert migration.down_revision == "0072_feedback_runtime_integrity"


def test_postgresql_gate_locks_catalogue_without_catalogue_update_grants() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration._create_feedback_eligibility_gate()

    sql = "\n".join(recorder.statements)
    assert "CREATE OR REPLACE FUNCTION" in sql
    assert "SECURITY DEFINER" in sql
    assert "SET search_path = pg_catalog, public" in sql
    assert "FROM public.strategies" in sql
    assert "FROM public.strategy_versions" in sql
    assert sql.count("FOR KEY SHARE") == 2
    assert "REVOKE ALL ON FUNCTION" in sql
    assert "FROM PUBLIC" in sql
    assert "GRANT EXECUTE ON FUNCTION" in sql
    assert " TO vm_feedback" in sql
    assert "GRANT UPDATE" not in sql
