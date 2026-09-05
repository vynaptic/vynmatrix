"""Migration contracts for binding-owned entry cash buffers."""

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
    / "0097_binding_entry_cash_buffer.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("binding_entry_cash_buffer", _MIGRATION_PATH)
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


def _previous_schema(engine: sa.Engine) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "user_strategy_bindings",
        metadata,
        sa.Column("binding_id", sa.Integer, primary_key=True),
    )
    metadata.create_all(engine)


def test_revision_follows_model_prior_identity() -> None:
    migration = _load_migration()

    assert migration.revision == "0097_binding_entry_cash_buffer"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0096_model_prior_identity"


def test_round_trip_adds_optional_validated_cash_buffer() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _previous_schema(engine)
    _run(engine, "upgrade")

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO user_strategy_bindings "
                "(binding_id, entry_cash_buffer_bps) VALUES (1, 25.5)"
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                sa.text(
                    "INSERT INTO user_strategy_bindings "
                    "(binding_id, entry_cash_buffer_bps) VALUES (2, 0)"
                )
            )

    with pytest.raises(RuntimeError, match="entry cash buffers"):
        _run(engine, "downgrade")


def test_downgrade_removes_unconfigured_column() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _previous_schema(engine)
    _run(engine, "upgrade")
    _run(engine, "downgrade")

    assert "entry_cash_buffer_bps" not in {
        column["name"] for column in sa.inspect(engine).get_columns("user_strategy_bindings")
    }
