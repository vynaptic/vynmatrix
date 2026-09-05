"""Migration contracts for owner-fenced synchronized panel execution."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from lib_application.db.models import Base

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0092_panel_runtime_owner_fence.py"
)
_TABLES = (
    "strategy_panel_input_revisions",
    "strategy_panel_decisions",
    "strategy_runtime_states",
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("panel_runtime_owner_fence", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine: sa.Engine, operation: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, operation)()


def test_revision_follows_dated_security_symbols() -> None:
    migration = _load_migration()

    assert migration.revision == "0092_panel_owner_fence"
    assert migration.down_revision == "0091_dated_security_symbols"


def test_empty_schema_round_trip_matches_model_columns_and_nullability() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    _run(engine, "downgrade")
    downgraded = sa.inspect(engine)
    assert "entitlement_owner_user_id" not in {
        column["name"] for column in downgraded.get_columns("strategy_panel_input_revisions")
    }
    assert "activation_cutoff" not in {
        column["name"] for column in downgraded.get_columns("strategy_panel_decisions")
    }

    _run(engine, "upgrade")
    inspector = sa.inspect(engine)
    for table_name in _TABLES:
        migrated = {
            column["name"]: column["nullable"] for column in inspector.get_columns(table_name)
        }
        modeled = {
            column.name: column.nullable for column in Base.metadata.tables[table_name].columns
        }
        assert migrated == modeled, f"model/migration column drift for {table_name}"


def test_model_and_migration_enforce_paper_forward_owner_binding() -> None:
    migration = _load_migration()
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    decision_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in Base.metadata.tables["strategy_panel_decisions"].constraints
        if isinstance(constraint, sa.CheckConstraint)
    }

    assert (
        "data_use_scope = 'paper_forward'" in decision_checks["ck_strategy_panel_runtime_binding"]
    )
    assert "data_use_scope = 'paper_forward'" in source
    assert "'skipped', 'expired'" in source
    assert migration._is_postgresql is not None
