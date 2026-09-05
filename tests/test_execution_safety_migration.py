"""Contract tests for execution-safety migration 0077."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0077_execution_safety.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "execution_safety_migration",
        _MIGRATION_PATH,
    )
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


def _create_previous_schema(engine: sa.Engine) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "instruments",
        metadata,
        sa.Column("instr_id", sa.BigInteger, primary_key=True),
        sa.Column("canonical", sa.String(50), nullable=False),
    )
    sa.Table(
        "instrument_aliases",
        metadata,
        sa.Column("instr_id", sa.BigInteger, nullable=False),
        sa.Column("alias", sa.String(50), nullable=False),
    )
    sa.Table(
        "user_strategy_bindings",
        metadata,
        sa.Column("binding_id", sa.BigInteger, primary_key=True),
        sa.Column("broker_account_id", sa.BigInteger, nullable=False),
        sa.Column("strategy_id", sa.String(50)),
        sa.Column("instruments_allowed", sa.JSON),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("autopilot", sa.Boolean, nullable=False),
        sa.Column("asset_score_threshold", sa.Numeric(10, 4), nullable=False),
        sa.Column("sector_score_threshold", sa.Numeric(10, 4)),
        sa.Column("market_score_threshold", sa.Numeric(10, 4)),
        sa.Column("max_position_pct", sa.Numeric(10, 4), nullable=False),
        sa.Column("max_daily_loss_pct", sa.Numeric(10, 4), nullable=False),
        sa.Column("max_open_positions", sa.Integer, nullable=False),
        sa.Column("execution_modes_allowed", sa.JSON, nullable=False),
        sa.Column("preferred_mode", sa.String(30)),
        sa.Column("asset_classes_allowed", sa.JSON, nullable=False),
        sa.Column("allowed_brokers", sa.JSON),
    )
    sa.Table(
        "outbox_events",
        metadata,
        sa.Column("event_id", sa.String(50), primary_key=True),
        sa.Column("topic", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
    )
    metadata.create_all(engine)


def test_revision_follows_durable_paper_orders() -> None:
    migration = _load_migration()

    assert migration.revision == "0077_execution_safety"
    assert migration.down_revision == "0076_durable_paper_orders"


class _RecordingOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


def test_postgresql_binding_trigger_serializes_account_authority_changes() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration._create_postgresql_binding_trigger()

    function_sql = recorder.statements[1]
    assert "pg_advisory_xact_lock" in function_sql
    assert "hashtext('vm_binding_account')" in function_sql
    assert "OLD.broker_account_id < NEW.broker_account_id" in function_sql
    assert "another active strategy owns an overlapping account/instrument scope" in function_sql


def test_upgrade_canonicalizes_authority_and_enforces_scalar_safety() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _create_previous_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO instruments (instr_id, canonical)
                VALUES (1, 'BTC/USDC')
                """
            )
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO instrument_aliases (instr_id, alias)
                VALUES (1, 'BTC-USDC')
                """
            )
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO user_strategy_bindings (
                    binding_id, broker_account_id, strategy_id,
                    instruments_allowed, is_active, autopilot,
                    asset_score_threshold, sector_score_threshold,
                    market_score_threshold, max_position_pct,
                    max_daily_loss_pct, max_open_positions,
                    execution_modes_allowed, preferred_mode,
                    asset_classes_allowed, allowed_brokers
                )
                VALUES (
                    1, 101, 'swing_high_low_pmo_v1',
                    '["BTC-USDC"]', 1, 1,
                    0.6, NULL, NULL, 0.1, 0.05, 10,
                    '["spot"]', 'spot', '["crypto"]', NULL
                )
                """
            )
        )

    _run(engine, "upgrade")

    columns = {
        column["name"]: column
        for column in sa.inspect(engine).get_columns("user_strategy_bindings")
    }
    assert columns["entries_enabled"]["nullable"] is False
    assert columns["exits_enabled"]["nullable"] is False
    with engine.begin() as connection:
        row = connection.execute(
            sa.text(
                """
                SELECT instruments_allowed, entries_enabled, exits_enabled
                  FROM user_strategy_bindings
                 WHERE binding_id = 1
                """
            )
        ).one()
        assert row.instruments_allowed == '["BTC/USDC"]'
        assert row.entries_enabled == 1
        assert row.exits_enabled == 1
        with pytest.raises(IntegrityError):
            connection.execute(
                sa.text(
                    """
                    UPDATE user_strategy_bindings
                       SET is_active = 0,
                           entries_enabled = 0,
                           exits_enabled = 1
                     WHERE binding_id = 1
                    """
                )
            )

    _run(engine, "downgrade")
    downgraded_columns = {
        column["name"] for column in sa.inspect(engine).get_columns("user_strategy_bindings")
    }
    assert "entries_enabled" not in downgraded_columns
    assert "exits_enabled" not in downgraded_columns
