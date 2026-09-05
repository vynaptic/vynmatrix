"""Contract tests for canonical execution-attribution migration 0057."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from lib_application.db.models import Base

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_PATH = (
    _ROOT / "scripts" / "db" / "alembic" / "versions" / "0057_canonical_execution_attribution.py"
)
_EXACT_FILL_MIGRATION_PATH = (
    _ROOT / "scripts" / "db" / "alembic" / "versions" / "0071_exact_broker_fill_contract.py"
)
_ATTRIBUTION_COLUMNS = {
    "strategy_id",
    "canonical_signal_id",
    "side",
    "execution_mode",
    "broker_environment",
}


def _load_migration(path: Path = _MIGRATION_PATH) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "canonical_execution_attribution_migration",
        path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_revision(
    engine: sa.Engine,
    operation: str,
    *,
    path: Path = _MIGRATION_PATH,
) -> None:
    migration = _load_migration(path)
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, operation)()


def test_revision_follows_canonical_position_valuation() -> None:
    migration = _load_migration()

    assert migration.revision == "0057_execution_attribution"
    assert migration.down_revision == "0056_position_valuation"


def test_empty_attribution_schema_round_trips() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    head = inspect(engine)
    assert {column["name"] for column in head.get_columns("order_intents")} >= _ATTRIBUTION_COLUMNS
    head_execution_columns = {column["name"]: column for column in head.get_columns("executions")}
    assert head_execution_columns["instr_id"]["nullable"] is False
    assert head_execution_columns["fee_amount"]["nullable"] is False

    # ``Base`` represents the current 0071 schema. Restore the 0057 execution
    # constraints before exercising 0057's own downgrade; an old revision must
    # not be tested against constraints introduced six migrations later.
    _run_revision(engine, "downgrade", path=_EXACT_FILL_MIGRATION_PATH)
    _run_revision(engine, "downgrade")
    legacy = inspect(engine)
    assert _ATTRIBUTION_COLUMNS.isdisjoint(
        {column["name"] for column in legacy.get_columns("order_intents")}
    )
    legacy_execution_columns = {
        column["name"]: column for column in legacy.get_columns("executions")
    }
    assert legacy_execution_columns["instr_id"]["nullable"] is True
    assert legacy_execution_columns["fee_amount"]["nullable"] is True

    _run_revision(engine, "upgrade")
    restored = inspect(engine)
    columns = {column["name"]: column for column in restored.get_columns("order_intents")}
    assert columns.keys() >= _ATTRIBUTION_COLUMNS
    assert columns["strategy_id"]["nullable"] is False
    assert columns["side"]["nullable"] is False
    assert columns["execution_mode"]["nullable"] is False
    assert columns["broker_environment"]["nullable"] is False
    restored_execution_columns = {
        column["name"]: column for column in restored.get_columns("executions")
    }
    assert restored_execution_columns["instr_id"]["nullable"] is False
    assert restored_execution_columns["fee_amount"]["nullable"] is False


class _Recorder:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


def test_postgresql_backfill_is_relational_and_fails_closed() -> None:
    migration = _load_migration()
    recorder = _Recorder()
    migration.op = recorder

    migration._backfill_postgresql()

    sql = "\n".join(recorder.statements)
    assert "pending.canonical_order_id = canonical_order.order_id" in sql
    assert "signal.external_signal_id = pending.signal_id" in sql
    assert "economic fills lack complete lineage" in sql
    assert "execution.fee_amount IS NULL" in sql
    assert "nullif(trim(execution.fee_ccy), '') IS NULL" in sql
    assert "order intents have ambiguous lineage" in sql
    assert "intent.payload" not in sql
