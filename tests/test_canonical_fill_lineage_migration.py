"""Contract tests for canonical fill-lineage migration 0064."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0064_canonical_fill_lineage.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "canonical_fill_lineage_migration",
        _MIGRATION_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_legacy_schema(engine: sa.Engine) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    order_intents = sa.Table(
        "order_intents",
        metadata,
        sa.Column("intent_id", sa.Integer, primary_key=True),
        sa.Column("canonical_signal_id", sa.BigInteger, nullable=True),
    )
    executions = sa.Table(
        "executions",
        metadata,
        sa.Column("exec_id", sa.Integer, primary_key=True),
        sa.Column("trade_id", sa.String(255), nullable=True),
    )
    metadata.create_all(engine)
    return {"order_intents": order_intents, "executions": executions}


def _run(engine: sa.Engine, operation: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, operation)()


def _nullable(engine: sa.Engine, table: str, column: str) -> bool:
    return bool(
        next(
            item["nullable"]
            for item in sa.inspect(engine).get_columns(table)
            if item["name"] == column
        )
    )


def _check_constraint_names(engine: sa.Engine, table: str) -> set[str]:
    return {
        str(constraint["name"])
        for constraint in sa.inspect(engine).get_check_constraints(table)
        if constraint["name"] is not None
    }


def test_revision_follows_required_signal_identity() -> None:
    migration = _load_migration()

    assert migration.revision == "0064_fill_lineage"
    assert migration.down_revision == "0063_required_signal_identity"


def test_upgrade_enforces_and_downgrade_restores_nullable_lineage() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    tables = _create_legacy_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            tables["order_intents"].insert(),
            [{"intent_id": 1, "canonical_signal_id": 91}],
        )
        connection.execute(
            tables["executions"].insert(),
            [{"exec_id": 1, "trade_id": "broker-fill-1"}],
        )

    _run(engine, "upgrade")
    assert not _nullable(engine, "order_intents", "canonical_signal_id")
    assert not _nullable(engine, "executions", "trade_id")
    assert "ck_execution_trade_identity" in _check_constraint_names(engine, "executions")

    _run(engine, "downgrade")
    assert _nullable(engine, "order_intents", "canonical_signal_id")
    assert _nullable(engine, "executions", "trade_id")
    assert "ck_execution_trade_identity" not in _check_constraint_names(engine, "executions")


@pytest.mark.parametrize(
    ("intent_signal_id", "trade_id", "expected"),
    [
        (None, "broker-fill-1", "order_intents.canonical_signal_id=1"),
        (91, None, "executions.trade_id=1"),
        (91, "   ", "executions.trade_id=1"),
    ],
)
def test_upgrade_refuses_unattributed_historical_rows(
    intent_signal_id: int | None,
    trade_id: str | None,
    expected: str,
) -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    tables = _create_legacy_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            tables["order_intents"].insert(),
            [{"intent_id": 1, "canonical_signal_id": intent_signal_id}],
        )
        connection.execute(
            tables["executions"].insert(),
            [{"exec_id": 1, "trade_id": trade_id}],
        )

    with pytest.raises(RuntimeError, match=expected):
        _run(engine, "upgrade")


def test_upgrade_refuses_blank_future_trade_identity() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _create_legacy_schema(engine)
    _run(engine, "upgrade")

    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO executions (exec_id, trade_id)
                VALUES (1, '   ')
                """
            )
        )
