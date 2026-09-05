"""Contract tests for exact broker-fill migration 0071."""

from __future__ import annotations

import importlib.util
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from lib_application.db.models import Execution

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0071_exact_broker_fill_contract.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("exact_broker_fill_migration", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_schema(engine: sa.Engine) -> sa.Table:
    metadata = sa.MetaData()
    executions = sa.Table(
        "executions",
        metadata,
        sa.Column("exec_id", sa.Integer(), primary_key=True),
        sa.Column("fill_ts", sa.DateTime(), nullable=False),
        sa.Column("qty", sa.Numeric(20, 8), nullable=False),
        sa.Column("price", sa.Numeric(20, 8), nullable=False),
        sa.Column("fee_ccy", sa.String(10), nullable=True),
        sa.Column("fee_amount", sa.Numeric(20, 8), nullable=False),
        sa.Column("venue", sa.String(50), nullable=True),
        sa.Column("trade_id", sa.String(255), nullable=False),
        sa.CheckConstraint(
            "qty >= 0 AND price > 0 AND fee_amount >= 0",
            name="ck_execution_incremental_economics",
        ),
        sa.CheckConstraint(
            "qty > 0 OR fee_amount > 0",
            name="ck_execution_nonempty_delta",
        ),
        sa.CheckConstraint(
            "fee_amount = 0 OR nullif(trim(fee_ccy), '') IS NOT NULL",
            name="ck_execution_fee_currency",
        ),
    )
    metadata.create_all(engine)
    return executions


def _run(engine: sa.Engine, operation: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, operation)()


def _constraint_names(engine: sa.Engine) -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(engine).get_check_constraints("executions")
        if item["name"] is not None
    }


def _nullable(engine: sa.Engine, column_name: str) -> bool:
    return bool(
        next(
            column["nullable"]
            for column in sa.inspect(engine).get_columns("executions")
            if column["name"] == column_name
        )
    )


def _valid_row(exec_id: int = 1) -> dict[str, object]:
    return {
        "exec_id": exec_id,
        "fill_ts": datetime(2026, 7, 25, 10, 0),
        "qty": 1,
        "price": 100,
        "fee_ccy": "USD",
        "fee_amount": 0,
        "venue": "coinbase",
        "trade_id": f"trade-{exec_id}",
    }


def _insert_row(engine: sa.Engine, row: dict[str, object]) -> None:
    with engine.begin() as connection:
        executions = sa.Table("executions", sa.MetaData(), autoload_with=connection)
        connection.execute(executions.insert(), row)


def test_revision_follows_canonical_asset_taxonomy() -> None:
    migration = _load_migration()

    assert migration.revision == "0071_exact_broker_fill"
    assert migration.down_revision == "0070_canonical_asset_taxonomy"


def test_execution_model_declares_complete_exact_fill_contract() -> None:
    columns = Execution.__table__.c
    constraints = {constraint.name for constraint in Execution.__table__.constraints}

    assert columns.fee_ccy.nullable is False
    assert columns.venue.nullable is False
    assert "ck_execution_exact_fill_economics" in constraints
    assert "ck_execution_fee_currency" in constraints
    assert "ck_execution_venue_identity" in constraints
    assert "ck_execution_nonempty_delta" not in constraints


def test_migration_round_trip_enforces_exact_fill_rows() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    executions = _legacy_schema(engine)
    with engine.begin() as connection:
        connection.execute(executions.insert(), [_valid_row()])

    _run(engine, "upgrade")

    assert _nullable(engine, "fee_ccy") is False
    assert _nullable(engine, "venue") is False
    assert _constraint_names(engine) >= {
        "ck_execution_exact_fill_economics",
        "ck_execution_fee_currency",
        "ck_execution_venue_identity",
    }
    invalid = _valid_row(2)
    invalid["qty"] = 0
    with pytest.raises(sa.exc.IntegrityError):
        _insert_row(engine, invalid)

    _run(engine, "downgrade")

    assert _nullable(engine, "fee_ccy") is True
    assert _nullable(engine, "venue") is True
    assert "ck_execution_nonempty_delta" in _constraint_names(engine)


def test_upgrade_preserves_signed_maker_rebate() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _legacy_schema(engine)
    _run(engine, "upgrade")
    rebate = _valid_row()
    rebate["fee_amount"] = -0.25

    _insert_row(engine, rebate)

    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT fee_amount FROM executions")).scalar_one() == (
            Decimal("-0.25")
        )
    with pytest.raises(RuntimeError, match="maker rebates"):
        _run(engine, "downgrade")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("qty", 0),
        ("fee_ccy", None),
        ("fee_ccy", "   "),
        ("venue", None),
        ("venue", "   "),
        ("trade_id", "   "),
    ],
)
def test_upgrade_refuses_incomplete_historical_economics(
    field: str,
    value: object,
) -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    executions = _legacy_schema(engine)
    row = _valid_row()
    row[field] = value
    if field == "qty":
        # The predecessor permits a zero-quantity fee-only delta.
        row["fee_amount"] = 1
    with engine.begin() as connection:
        connection.execute(executions.insert(), [row])

    with pytest.raises(RuntimeError, match="incomplete execution rows"):
        _run(engine, "upgrade")
