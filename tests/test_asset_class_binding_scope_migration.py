"""Contract tests for migration 0110: asset classes in the binding overlap trigger.

The PostgreSQL behaviour, and its agreement with ``binding_control.BindingScope``,
is proved in ``test_owner_control_plane_postgres_integration.py``.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from lib_common.asset_classes import CANONICAL_ASSET_CLASS_VALUES

_VERSIONS = Path(__file__).resolve().parents[1] / "scripts" / "db" / "alembic" / "versions"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"migration_{name}", _VERSIONS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    """Captures SQL; ``pairs`` is what the downgrade guard's count query returns."""

    def __init__(self, *, pairs: int = 0) -> None:
        self.statements: list[str] = []
        self.queries: list[str] = []
        self._pairs = pairs

    def get_bind(self) -> Any:
        def execute(statement: Any) -> Any:
            self.queries.append(str(statement))
            return SimpleNamespace(scalar_one=lambda: self._pairs)

        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), execute=execute)

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


def _function(statements: list[str], name: str) -> str:
    matches = [s for s in statements if f"CREATE OR REPLACE FUNCTION {name}(" in s]
    assert len(matches) == 1, name
    return matches[0]


def _run(module: ModuleType, operation: str, op: Any) -> None:
    module.op = op
    getattr(module, operation)()


def test_revision_follows_backend_ui_readiness() -> None:
    migration = _load("0110_asset_class_binding_scope")

    assert migration.revision == "0110_asset_class_binding_scope"
    assert migration.down_revision == "0109_backend_ui_readiness"
    # alembic_version.version_num is varchar(32).
    assert len(migration.revision) <= 32


def test_downgrade_restores_the_0077_trigger_function_exactly() -> None:
    original = _RecordingOp()
    _run(_load("0077_execution_safety"), "_create_postgresql_binding_trigger", original)
    restored = _RecordingOp()
    _run(_load("0110_asset_class_binding_scope"), "downgrade", restored)

    expected = _function(original.statements, "vm_validate_strategy_binding")
    assert _function(restored.statements, "vm_validate_strategy_binding").strip() == (
        expected.strip()
    )
    assert restored.statements[-2:] == [
        "DROP FUNCTION IF EXISTS vm_binding_scopes_overlap(BIGINT[], TEXT[], BIGINT[], TEXT[])",
        "DROP FUNCTION IF EXISTS vm_binding_asset_classes(JSON)",
    ]


def test_upgrade_changes_only_the_overlap_predicate() -> None:
    original = _RecordingOp()
    _run(_load("0077_execution_safety"), "_create_postgresql_binding_trigger", original)
    upgraded = _RecordingOp()
    migration = _load("0110_asset_class_binding_scope")
    _run(migration, "upgrade", upgraded)

    function = _function(upgraded.statements, "vm_validate_strategy_binding")
    before = _function(original.statements, "vm_validate_strategy_binding")
    assert function.replace(migration._ASSET_CLASS_OVERLAP, "").strip() == (
        before.replace(migration._INSTRUMENT_ONLY_OVERLAP, "").strip()
    )
    assert "vm_binding_asset_classes(NEW.asset_classes_allowed)" in function
    assert "another active strategy owns an overlapping account/instrument scope" in function
    # The helpers exist before the trigger function that calls them.
    assert upgraded.statements.index(function) == len(upgraded.statements) - 1


def test_the_sql_taxonomy_is_the_canonical_asset_class_list() -> None:
    upgraded = _RecordingOp()
    _run(_load("0110_asset_class_binding_scope"), "upgrade", upgraded)

    helper = _function(upgraded.statements, "vm_binding_asset_classes")
    listed = re.search(r"NOT IN \(([^)]*)\)", helper)
    assert listed is not None
    assert tuple(re.findall(r"'([a-z]+)'", listed.group(1))) == CANONICAL_ASSET_CLASS_VALUES


def test_downgrade_refuses_while_bindings_rely_on_asset_class_separation() -> None:
    guarded = _RecordingOp(pairs=2)

    with pytest.raises(RuntimeError, match="2 pair\\(s\\) of active bindings"):
        _run(_load("0110_asset_class_binding_scope"), "downgrade", guarded)

    assert guarded.statements == []
    assert "vm_binding_instrument_ids(earlier.instruments_allowed) IS NULL" in guarded.queries[0]


@pytest.mark.parametrize("operation", ["upgrade", "downgrade"])
def test_other_dialects_are_untouched(operation: str) -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        _run(
            _load("0110_asset_class_binding_scope"),
            operation,
            Operations(MigrationContext.configure(connection)),
        )
        assert sa.inspect(connection).get_table_names() == []
