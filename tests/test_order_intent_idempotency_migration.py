"""Contract tests for canonical order-intent idempotency migration 0074."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from lib_application.db.models import OrderIntent

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0074_order_intent_idempotency.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "order_intent_idempotency_migration",
        _MIGRATION_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_schema(engine: sa.Engine) -> sa.Table:
    metadata = sa.MetaData()
    intents = sa.Table(
        "order_intents",
        metadata,
        sa.Column("intent_id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    metadata.create_all(engine)
    return intents


def _run(engine: sa.Engine, operation: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, operation)()


def test_revision_follows_feedback_catalogue_gate() -> None:
    migration = _load_migration()

    assert migration.revision == "0074_order_intent_idempotency"
    assert migration.down_revision == "0073_feedback_catalogue_gate"


def test_model_declares_account_scoped_order_idempotency() -> None:
    constraints = {constraint.name for constraint in OrderIntent.__table__.constraints}

    assert OrderIntent.__table__.c.idempotency_key.nullable is True
    assert "uq_order_intent_account_idempotency" in constraints
    assert "ck_order_intent_idempotency_key" in constraints


def test_migration_backfills_payload_key_and_refuses_identity_loss() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    intents = _legacy_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            intents.insert(),
            [
                {
                    "intent_id": 1,
                    "account_id": 101,
                    "payload": {"idempotency_key": "dispatch-order-1"},
                },
                {
                    "intent_id": 2,
                    "account_id": 202,
                    "payload": {},
                },
            ],
        )

    _run(engine, "upgrade")

    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT intent_id, idempotency_key FROM order_intents ORDER BY intent_id")
        ).all() == [(1, "dispatch-order-1"), (2, None)]
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO order_intents
                    (intent_id, account_id, payload, idempotency_key)
                VALUES
                    (3, 101, '{}', 'dispatch-order-1')
                """
            )
        )
    with pytest.raises(RuntimeError, match="attributed intents"):
        _run(engine, "downgrade")

    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE order_intents SET idempotency_key = NULL"))
    _run(engine, "downgrade")
    assert "idempotency_key" not in {
        column["name"] for column in sa.inspect(engine).get_columns("order_intents")
    }
