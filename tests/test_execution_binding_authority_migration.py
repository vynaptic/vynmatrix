"""Contract tests for execution binding-authority migration 0080."""

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
    / "0080_execution_binding_read.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "execution_binding_authority_migration",
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


def test_revision_follows_feedback_concurrency_fences() -> None:
    migration = _load_migration()

    assert migration.revision == "0080_execution_binding_read"
    assert migration.down_revision == "0079_feedback_concurrency_fences"


def test_execution_binding_read_has_explicit_grant_and_rls_policy() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration._grant_postgresql_execution_access()

    assert recorder.statements == [
        "GRANT SELECT ON TABLE public.user_strategy_bindings TO vm_execution",
        ("DROP POLICY IF EXISTS user_strategy_bindings_execution_select ON user_strategy_bindings"),
        (
            "CREATE POLICY user_strategy_bindings_execution_select "
            "ON user_strategy_bindings FOR SELECT TO vm_execution USING (true)"
        ),
    ]
