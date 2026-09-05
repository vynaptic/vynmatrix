"""Contract tests for feedback runtime integrity migration 0072."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_PATH = (
    _ROOT / "scripts" / "db" / "alembic" / "versions" / "0072_feedback_runtime_integrity.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "feedback_runtime_integrity_migration",
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
        "strategy_parameter_feedback",
        metadata,
        sa.Column("feedback_id", sa.Integer, primary_key=True),
        sa.Column("strategy_id", sa.String(50), nullable=False),
        sa.Column("strat_ver_id", sa.Integer),
        sa.Column("instr_id", sa.Integer),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
    )
    sa.Table(
        "strategy_consecutive_wrong_tracker",
        metadata,
        sa.Column("tracker_id", sa.Integer, primary_key=True),
        sa.Column("strategy_id", sa.String(50), nullable=False),
        sa.Column("strat_ver_id", sa.Integer),
        sa.Column("instr_id", sa.Integer, nullable=False),
        sa.Column("horizon", sa.String(10), nullable=False, server_default="1d"),
        sa.Column("feedback_id", sa.Integer),
    )
    metadata.create_all(engine)


def _run(engine: sa.Engine, operation: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, operation)()


def test_revision_follows_exact_fill_contract() -> None:
    migration = _load_migration()

    assert migration.revision == "0072_feedback_runtime_integrity"
    assert migration.down_revision == "0071_exact_broker_fill"


def test_migration_round_trip_adds_horizon_scope() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _legacy_schema(engine)

    _run(engine, "upgrade")

    inspector = sa.inspect(engine)
    feedback_columns = {
        column["name"]: column for column in inspector.get_columns("strategy_parameter_feedback")
    }
    tracker_columns = {
        column["name"]: column
        for column in inspector.get_columns("strategy_consecutive_wrong_tracker")
    }
    constraints = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("strategy_parameter_feedback")
    }
    indexes = {
        index["name"]: index for index in inspector.get_indexes("strategy_parameter_feedback")
    }
    assert feedback_columns["horizon"]["nullable"] is False
    assert feedback_columns["horizon"]["default"] is None
    assert tracker_columns["horizon"]["default"] is None
    assert "ck_param_feedback_horizon" in constraints
    assert indexes["uq_pending_feedback_scope"]["unique"] == 1

    _run(engine, "downgrade")

    inspector = sa.inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("strategy_parameter_feedback")}
    assert "horizon" not in columns
    assert "uq_pending_feedback_scope" not in {
        index["name"] for index in inspector.get_indexes("strategy_parameter_feedback")
    }
    tracker_horizon = next(
        column
        for column in inspector.get_columns("strategy_consecutive_wrong_tracker")
        if column["name"] == "horizon"
    )
    assert tracker_horizon["default"] is not None


def test_upgrade_backfills_only_from_exact_linked_tracker() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _legacy_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO strategy_parameter_feedback
                    (feedback_id, strategy_id, strat_ver_id, instr_id, status)
                VALUES (7, 'ema_v1', 11, 101, 'pending')
                """
            )
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO strategy_consecutive_wrong_tracker
                    (tracker_id, strategy_id, strat_ver_id, instr_id, horizon, feedback_id)
                VALUES (9, 'ema_v1', 11, 101, '1h', 7)
                """
            )
        )

    _run(engine, "upgrade")

    with engine.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT horizon FROM strategy_parameter_feedback WHERE feedback_id = 7")
            ).scalar_one()
            == "1h"
        )


@pytest.mark.parametrize("tracker_count", [0, 2])
def test_upgrade_rejects_unresolved_or_ambiguous_feedback_lineage(
    tracker_count: int,
) -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _legacy_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO strategy_parameter_feedback
                    (feedback_id, strategy_id, strat_ver_id, instr_id, status)
                VALUES (7, 'ema_v1', 11, 101, 'pending')
                """
            )
        )
        for tracker_id in range(tracker_count):
            connection.execute(
                sa.text(
                    """
                    INSERT INTO strategy_consecutive_wrong_tracker
                        (tracker_id, strategy_id, strat_ver_id, instr_id, horizon, feedback_id)
                    VALUES (:tracker_id, 'ema_v1', 11, 101, :horizon, 7)
                    """
                ),
                {
                    "tracker_id": tracker_id + 1,
                    "horizon": "1h" if tracker_id == 0 else "4h",
                },
            )

    with pytest.raises(RuntimeError, match="lack exactly one identity-matched tracker"):
        _run(engine, "upgrade")
