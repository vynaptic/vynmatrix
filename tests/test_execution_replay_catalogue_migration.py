"""Contract tests for execution replay catalogue migration 0082."""

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
    / "0082_execution_replay_read.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "execution_replay_catalogue_migration",
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


def test_revision_follows_indicator_source_lock() -> None:
    migration = _load_migration()

    assert migration.revision == "0082_execution_replay_read"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0081_indicator_source_lock"


def test_execution_replay_grants_only_strategy_version_read() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    recorder.get_bind = lambda: _Bind()  # type: ignore[attr-defined]
    migration.upgrade()

    assert recorder.statements == ["GRANT SELECT ON TABLE public.strategy_versions TO vm_execution"]
