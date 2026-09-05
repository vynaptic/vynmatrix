"""Contract tests for durable paper-fill projection checkpoint migration 0083."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from lib_application.db.models import PendingOrder

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0083_paper_fill_projection_checkpoint.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "paper_fill_projection_checkpoint_migration",
        _MIGRATION_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_schema(engine: sa.Engine) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "executions",
        metadata,
        sa.Column("exec_id", sa.BigInteger(), primary_key=True),
    )
    sa.Table(
        "pending_orders",
        metadata,
        sa.Column("order_id", sa.String(100), primary_key=True),
    )
    metadata.create_all(engine)


def _run(engine: sa.Engine, operation: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, operation)()


def test_revision_follows_execution_replay_read() -> None:
    migration = _load_migration()

    assert migration.revision == "0083_paper_fill_projection"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0082_execution_replay_read"


def test_model_declares_exact_execution_checkpoint() -> None:
    column = PendingOrder.__table__.c.pending_projection_exec_id
    foreign_keys = {foreign_key.target_fullname for foreign_key in column.foreign_keys}
    indexes = {index.name for index in PendingOrder.__table__.indexes}

    assert column.nullable is True
    assert foreign_keys == {"executions.exec_id"}
    assert "ix_pending_orders_pending_projection_exec_id" in indexes


def test_upgrade_round_trips_checkpoint_foreign_key_and_index() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _legacy_schema(engine)

    _run(engine, "upgrade")

    inspector = sa.inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("pending_orders")}
    foreign_keys = inspector.get_foreign_keys("pending_orders")
    indexes = {index["name"] for index in inspector.get_indexes("pending_orders")}
    assert "pending_projection_exec_id" in columns
    assert {
        (
            foreign_key["name"],
            tuple(foreign_key["constrained_columns"]),
            foreign_key["referred_table"],
            tuple(foreign_key["referred_columns"]),
        )
        for foreign_key in foreign_keys
    } == {
        (
            "fk_pending_order_projection_execution",
            ("pending_projection_exec_id",),
            "executions",
            ("exec_id",),
        )
    }
    assert "ix_pending_orders_pending_projection_exec_id" in indexes

    _run(engine, "downgrade")
    assert "pending_projection_exec_id" not in {
        column["name"] for column in sa.inspect(engine).get_columns("pending_orders")
    }


def test_checkpoint_rejects_unknown_execution() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)

    @sa.event.listens_for(engine, "connect")
    def _enable_foreign_keys(
        dbapi_connection: object,
        _connection_record: object,
    ) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    _legacy_schema(engine)
    _run(engine, "upgrade")

    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO pending_orders (order_id, pending_projection_exec_id)
                VALUES ('paper-order-1', 999)
                """
            )
        )
