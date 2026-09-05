"""Contract tests for durable strategy runtime migration 0075."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

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
    / "0075_durable_strategy_runtime.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "durable_strategy_runtime_migration",
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
        "strategies",
        metadata,
        sa.Column("strategy_id", sa.String(50), primary_key=True),
    )
    sa.Table(
        "prices",
        metadata,
        sa.Column("price_id", sa.BigInteger, primary_key=True),
    )
    sa.Table(
        "outbox_events",
        metadata,
        sa.Column("event_id", sa.String(50), primary_key=True),
        sa.Column("topic", sa.String(100), nullable=False),
    )
    metadata.create_all(engine)


def test_revision_follows_order_intent_idempotency() -> None:
    migration = _load_migration()

    assert migration.revision == "0075_durable_strategy_runtime"
    assert migration.down_revision == "0074_order_intent_idempotency"


def test_upgrade_creates_versioned_state_and_append_only_decision_contract() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _create_previous_schema(engine)

    _run(engine, "upgrade")

    inspector = sa.inspect(engine)
    assert {"strategy_runtime_states", "strategy_decisions"} <= set(inspector.get_table_names())
    state_columns = {
        column["name"]: column for column in inspector.get_columns("strategy_runtime_states")
    }
    assert {
        "worker_id",
        "strategy_id",
        "strategy_version",
        "symbols",
        "source",
        "timeframe",
        "consolidation_minutes",
        "config_fingerprint",
        "state_schema_version",
        "state_payload",
        "last_source_symbol",
        "last_source_price_id",
        "last_source_ts",
        "last_source_content_revision",
        "generation",
    } <= state_columns.keys()
    assert state_columns["state_schema_version"]["nullable"] is False
    assert state_columns["state_payload"]["nullable"] is False
    assert {index["name"] for index in inspector.get_indexes("strategy_runtime_states")} >= {
        "ix_strategy_runtime_identity",
        "ix_strategy_runtime_states_strategy_id",
    }

    decision_columns = {
        column["name"]: column for column in inspector.get_columns("strategy_decisions")
    }
    assert {
        "decision_key",
        "worker_id",
        "strategy_bar_ts",
        "source_price_id",
        "source_content_revision",
        "state_schema_version",
        "signal_envelopes",
    } <= decision_columns.keys()
    assert decision_columns["signal_envelopes"]["nullable"] is False
    assert {
        constraint["name"] for constraint in inspector.get_unique_constraints("strategy_decisions")
    } >= {"uq_strategy_decision_bar_revision"}

    _run(engine, "downgrade")
    assert "strategy_runtime_states" not in sa.inspect(engine).get_table_names()
    assert "strategy_decisions" not in sa.inspect(engine).get_table_names()


def test_downgrade_refuses_to_delete_runtime_history() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _create_previous_schema(engine)
    _run(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO strategies VALUES ('strategy-v1')"))
        connection.execute(sa.text("INSERT INTO prices VALUES (1)"))
        connection.execute(
            sa.text(
                """
                INSERT INTO strategy_runtime_states (
                    worker_id, strategy_id, strategy_version, symbols, source,
                    timeframe, consolidation_minutes, config_fingerprint,
                    state_schema_version, state_payload, last_source_symbol,
                    last_source_price_id, last_source_ts,
                    last_source_content_revision, generation
                )
                VALUES (
                    'worker', 'strategy-v1', '1.0.0', '["BTCUSDC"]',
                    'coinbase_live', '1m', 1, :fingerprint, 1,
                    '{"schema_version": 1}', 'BTCUSDC', 1,
                    '2026-07-26 12:00:00', 1, 1
                )
                """
            ),
            {"fingerprint": "a" * 64},
        )

    with pytest.raises(RuntimeError, match="Cannot remove durable strategy runtime history"):
        _run(engine, "downgrade")


class _RecordingOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


def test_postgresql_access_is_topic_scoped_and_command_specific() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration._enable_postgresql_runtime_access()

    sql = "\n".join(recorder.statements)
    assert "ALTER TABLE strategy_runtime_states ENABLE ROW LEVEL SECURITY" in sql
    assert "ALTER TABLE strategy_decisions ENABLE ROW LEVEL SECURITY" in sql
    assert "ALTER TABLE outbox_events ENABLE ROW LEVEL SECURITY" in sql
    assert (
        "CREATE POLICY outbox_events_indicator_select ON outbox_events "
        "FOR SELECT TO vm_indicator USING (topic = 'signals.submit')"
    ) in recorder.statements
    assert (
        "CREATE POLICY outbox_events_indicator_insert ON outbox_events "
        "FOR INSERT TO vm_indicator WITH CHECK (topic = 'signals.submit')"
    ) in recorder.statements
    assert (
        "CREATE POLICY outbox_events_indicator_update ON outbox_events "
        "FOR UPDATE TO vm_indicator USING (topic = 'signals.submit') "
        "WITH CHECK (topic = 'signals.submit')"
    ) in recorder.statements
    assert not any(
        "outbox_events_execution_update" in statement for statement in recorder.statements
    )
    assert " FOR ALL " not in sql
