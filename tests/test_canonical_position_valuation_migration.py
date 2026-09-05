"""Contract tests for canonical position valuation migration 0056."""

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
    _ROOT / "scripts" / "db" / "alembic" / "versions" / "0056_canonical_position_valuation.py"
)
_VALUATION_COLUMNS = {
    "quantity_unit",
    "contract_multiplier",
    "gross_notional",
    "notional_currency",
}


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "canonical_position_valuation_migration",
        _MIGRATION_PATH,
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


def test_revision_follows_dormant_schema_retirement() -> None:
    migration = _load_migration()

    assert migration.revision == "0056_position_valuation"
    assert migration.down_revision == "0055_retire_dormant_schema"


def test_empty_legacy_position_schema_round_trips() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    head = inspect(engine)
    assert "options_positions" not in head.get_table_names()
    assert {column["name"] for column in head.get_columns("positions")} >= _VALUATION_COLUMNS

    _run_revision(engine, "downgrade")

    legacy = inspect(engine)
    assert "options_positions" in legacy.get_table_names()
    assert _VALUATION_COLUMNS.isdisjoint(
        {column["name"] for column in legacy.get_columns("positions")}
    )

    _run_revision(engine, "upgrade")

    restored = inspect(engine)
    assert "options_positions" not in restored.get_table_names()
    assert {column["name"] for column in restored.get_columns("positions")} >= _VALUATION_COLUMNS


def test_upgrade_refuses_to_guess_existing_position_semantics() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _run_revision(engine, "downgrade")

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO options_positions (
                    position_id,
                    user_id,
                    account_id,
                    strategy_id,
                    spread_type,
                    underlying_symbol,
                    expiry_date,
                    leg1_strike,
                    leg1_option_type,
                    leg1_side,
                    leg1_quantity,
                    leg1_premium,
                    leg2_strike,
                    leg2_option_type,
                    leg2_side,
                    leg2_quantity,
                    leg2_premium,
                    net_debit_credit
                )
                VALUES (
                    'legacy-position',
                    'legacy-user',
                    1,
                    'legacy-strategy',
                    'bull_call',
                    'BTC-USD',
                    '2026-08-01',
                    100,
                    'CALL',
                    'BUY',
                    1,
                    10,
                    110,
                    'CALL',
                    'SELL',
                    1,
                    5,
                    5
                )
                """
            )
        )

    migration = _load_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        with pytest.raises(RuntimeError, match="options_positions=1"):
            migration.upgrade()

    assert "options_positions" in inspect(engine).get_table_names()
