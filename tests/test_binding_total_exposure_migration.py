"""Migration contracts for binding-owned gross-exposure ceilings."""

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
    / "0095_binding_total_exposure.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("binding_total_exposure", _MIGRATION_PATH)
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


def test_revision_follows_backend_control_plane() -> None:
    migration = _load_migration()

    assert migration.revision == "0095_binding_total_exposure"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0094_backend_control_plane"


def test_round_trip_adds_validated_default_exposure_ceiling() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _previous_schema(engine)
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO user_strategy_bindings (binding_id) VALUES (1)"))

    _run(engine, "upgrade")
    with engine.begin() as connection:
        assert (
            connection.scalar(
                sa.text(
                    "SELECT max_total_exposure_pct FROM user_strategy_bindings WHERE binding_id = 1"
                )
            )
            == 0.5
        )
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                sa.text(
                    "UPDATE user_strategy_bindings "
                    "SET max_total_exposure_pct = 1.01 WHERE binding_id = 1"
                )
            )

    _run(engine, "downgrade")
    assert "max_total_exposure_pct" not in {
        column["name"] for column in sa.inspect(engine).get_columns("user_strategy_bindings")
    }


def test_downgrade_refuses_to_discard_non_default_policy() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _previous_schema(engine)
    _run(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO user_strategy_bindings "
                "(binding_id, max_total_exposure_pct) VALUES (1, 1.0)"
            )
        )

    with pytest.raises(RuntimeError, match="gross-exposure policy"):
        _run(engine, "downgrade")
