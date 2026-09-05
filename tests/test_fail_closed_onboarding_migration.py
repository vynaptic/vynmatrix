"""Regression checks for fail-closed strategy-binding defaults."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import sqlalchemy as sa

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0059_fail_closed_onboarding_defaults.py"
)


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location(
        "fail_closed_onboarding_migration", _MIGRATION_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.alterations: list[tuple[str, str, dict[str, Any]]] = []
        self.executed: list[Any] = []

    def alter_column(self, table: str, column: str, **kwargs: Any) -> None:
        self.alterations.append((table, column, kwargs))

    def execute(self, statement: Any) -> None:
        self.executed.append(statement)


def test_revision_follows_relational_risk_ownership() -> None:
    migration = _load_migration()

    assert migration.revision == "0059_fail_closed_onboarding"
    assert migration.down_revision == "0058_risk_ownership"


def test_upgrade_changes_only_future_row_defaults() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration.upgrade()

    assert recorder.executed == []
    assert [(table, column) for table, column, _ in recorder.alterations] == [
        ("user_strategy_bindings", "is_active"),
        ("user_strategy_bindings", "autopilot"),
    ]
    for _, _, kwargs in recorder.alterations:
        assert isinstance(kwargs["existing_type"], sa.Boolean)
        assert kwargs["server_default"] == "false"
        assert kwargs["existing_nullable"] is False


def test_downgrade_keeps_fail_closed_defaults_without_rewriting_rows() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration.downgrade()

    assert recorder.executed == []
    assert [kwargs["server_default"] for _, _, kwargs in recorder.alterations] == [
        "false",
        "false",
    ]
