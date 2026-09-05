"""Contract tests for required signal-identity migration 0063."""

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
    / "0063_required_signal_identity.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "required_signal_identity_migration",
        _MIGRATION_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_legacy_schema(engine: sa.Engine) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    canonical_signals = sa.Table(
        "canonical_signals",
        metadata,
        sa.Column("signal_id", sa.Integer, primary_key=True),
        sa.Column("external_signal_id", sa.String(128), nullable=True),
    )
    asset_scores = sa.Table(
        "asset_scores",
        metadata,
        sa.Column("score_id", sa.Integer, primary_key=True),
        sa.Column("external_signal_id", sa.String(64), nullable=True),
    )
    execution_decision_logs = sa.Table(
        "execution_decision_logs",
        metadata,
        sa.Column("decision_id", sa.Integer, primary_key=True),
        sa.Column("idempotency_key", sa.String(64), nullable=True),
    )
    metadata.create_all(engine)
    return {
        "canonical_signals": canonical_signals,
        "asset_scores": asset_scores,
        "execution_decision_logs": execution_decision_logs,
    }


def _run(engine: sa.Engine, operation: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, operation)()


def _nullable_columns(engine: sa.Engine) -> dict[str, bool]:
    inspector = sa.inspect(engine)
    return {
        f"{table}.{column}": next(
            item["nullable"] for item in inspector.get_columns(table) if item["name"] == column
        )
        for table, column in (
            ("canonical_signals", "external_signal_id"),
            ("asset_scores", "external_signal_id"),
            ("execution_decision_logs", "idempotency_key"),
        )
    }


def _column_length(engine: sa.Engine, table: str, column: str) -> int | None:
    column_type = next(
        item["type"] for item in sa.inspect(engine).get_columns(table) if item["name"] == column
    )
    length = getattr(column_type, "length", None)
    return int(length) if length is not None else None


def _check_constraint_names(engine: sa.Engine, table: str) -> set[str]:
    return {
        str(constraint["name"])
        for constraint in sa.inspect(engine).get_check_constraints(table)
        if constraint["name"] is not None
    }


def test_revision_follows_historical_price_rebuild() -> None:
    migration = _load_migration()

    assert migration.revision == "0063_required_signal_identity"
    assert migration.down_revision == "0062_historical_price_rebuild"


def test_upgrade_requires_each_identity_and_round_trips() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    tables = _create_legacy_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            tables["canonical_signals"].insert(),
            [{"signal_id": 1, "external_signal_id": "external-1"}],
        )
        connection.execute(
            tables["asset_scores"].insert(),
            [{"score_id": 1, "external_signal_id": "external-1"}],
        )
        connection.execute(
            tables["execution_decision_logs"].insert(),
            [{"decision_id": 1, "idempotency_key": "exec-1"}],
        )

    assert all(_nullable_columns(engine).values())
    assert _column_length(engine, "asset_scores", "external_signal_id") == 64
    _run(engine, "upgrade")
    assert not any(_nullable_columns(engine).values())
    assert _column_length(engine, "asset_scores", "external_signal_id") == 128
    assert "ck_canonical_signal_external_identity" in _check_constraint_names(
        engine,
        "canonical_signals",
    )
    assert "ck_asset_score_external_identity" in _check_constraint_names(
        engine,
        "asset_scores",
    )
    assert "ck_execution_decision_idempotency_key" in _check_constraint_names(
        engine,
        "execution_decision_logs",
    )

    with engine.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT external_signal_id FROM canonical_signals")
            ).scalar_one()
            == "external-1"
        )
        assert (
            connection.execute(sa.text("SELECT external_signal_id FROM asset_scores")).scalar_one()
            == "external-1"
        )
        assert (
            connection.execute(
                sa.text("SELECT idempotency_key FROM execution_decision_logs")
            ).scalar_one()
            == "exec-1"
        )

    _run(engine, "downgrade")
    assert all(_nullable_columns(engine).values())
    assert _column_length(engine, "asset_scores", "external_signal_id") == 64
    assert _check_constraint_names(engine, "canonical_signals") == set()
    assert _check_constraint_names(engine, "asset_scores") == set()
    assert _check_constraint_names(engine, "execution_decision_logs") == set()


def test_upgrade_refuses_unattributed_historical_rows() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    tables = _create_legacy_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            tables["canonical_signals"].insert(),
            [{"signal_id": 1, "external_signal_id": None}],
        )
        connection.execute(
            tables["asset_scores"].insert(),
            [{"score_id": 1, "external_signal_id": "   "}],
        )
        connection.execute(
            tables["execution_decision_logs"].insert(),
            [{"decision_id": 1, "idempotency_key": ""}],
        )

    with pytest.raises(
        RuntimeError,
        match=(
            r"canonical_signals\.external_signal_id=1, "
            r"asset_scores\.external_signal_id=1, "
            r"execution_decision_logs\.idempotency_key=1"
        ),
    ):
        _run(engine, "upgrade")

    assert all(_nullable_columns(engine).values())


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO canonical_signals (signal_id, external_signal_id) VALUES (1, '   ')",
        "INSERT INTO asset_scores (score_id, external_signal_id) VALUES (1, '   ')",
        ("INSERT INTO execution_decision_logs (decision_id, idempotency_key) VALUES (1, '   ')"),
    ],
)
def test_upgrade_constraints_refuse_blank_future_identity(
    statement: str,
) -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _create_legacy_schema(engine)
    _run(engine, "upgrade")

    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(sa.text(statement))
