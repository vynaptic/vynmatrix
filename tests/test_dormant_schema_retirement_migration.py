"""Contract tests for dormant decision-schema retirement migration 0055."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from lib_application.db.models import Base

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_PATH = (
    _ROOT / "scripts" / "db" / "alembic" / "versions" / "0055_retire_dormant_schema.py"
)
_RETIRED_TABLES = {
    "trade_signals",
    "decision_runs",
    "decision_run_members",
    "opportunities",
    "opportunity_methods",
    "opportunity_explanations",
    "user_opportunity_subscriptions",
    "opp_sub_execution_bindings",
    "strategy_coverage",
}


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "retire_dormant_schema_migration", _MIGRATION_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_revision(engine: sa.Engine, operation: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, operation)()


def test_revision_follows_observed_fx_head() -> None:
    migration = _load_migration()

    assert migration.revision == "0055_retire_dormant_schema"
    assert migration.down_revision == "0054_observed_fx_rates"


def test_empty_dormant_schema_round_trips_without_touching_active_tables() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    _run_revision(engine, "downgrade")

    restored = inspect(engine)
    assert set(restored.get_table_names()) >= _RETIRED_TABLES
    assert "opp_id" in {column["name"] for column in restored.get_columns("order_intents")}
    assert "user_budget_buckets" in restored.get_table_names()

    _run_revision(engine, "upgrade")

    retired = inspect(engine)
    assert _RETIRED_TABLES.isdisjoint(retired.get_table_names())
    assert "opp_id" not in {column["name"] for column in retired.get_columns("order_intents")}
    assert "user_budget_buckets" in retired.get_table_names()


def test_upgrade_refuses_to_discard_unexpected_historical_rows() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _run_revision(engine, "downgrade")

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO user_opportunity_subscriptions (
                    opp_sub_id,
                    user_id,
                    asset_class,
                    horizons,
                    instruments,
                    autopilot,
                    approval_mode,
                    created_at
                )
                VALUES (
                    1,
                    'historical-user',
                    'crypto',
                    '["intraday"]',
                    '{}',
                    false,
                    'instant',
                    CURRENT_TIMESTAMP
                )
                """
            )
        )

    migration = _load_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        with pytest.raises(
            RuntimeError,
            match="user_opportunity_subscriptions=1",
        ):
            migration.upgrade()

    inspector = inspect(engine)
    assert set(inspector.get_table_names()) >= _RETIRED_TABLES
    assert "opp_id" in {column["name"] for column in inspector.get_columns("order_intents")}
