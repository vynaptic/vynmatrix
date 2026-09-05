"""Forward migration tests for point-in-time factor-risk observations."""

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
    / "0093_factor_risk_exposure.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("factor_risk_exposure", _MIGRATION_PATH)
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
        "equity_observations",
        metadata,
        sa.Column("observation_id", sa.String(64), primary_key=True),
        sa.Column("observation_kind", sa.String(30), nullable=False),
        sa.CheckConstraint(
            _load_migration()._PREVIOUS_OBSERVATION_KIND_CHECK,
            name="ck_equity_observation_kind",
        ),
    )
    metadata.create_all(engine)


def _observation_kind_check(engine: sa.Engine) -> str:
    return next(
        constraint["sqltext"]
        for constraint in sa.inspect(engine).get_check_constraints("equity_observations")
        if constraint["name"] == "ck_equity_observation_kind"
    )


def test_revision_follows_panel_owner_fence() -> None:
    migration = _load_migration()

    assert migration.revision == "0093_factor_risk_exposure"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0092_panel_owner_fence"


def test_empty_schema_round_trip_adds_and_removes_only_factor_risk_kind() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _previous_schema(engine)

    _run(engine, "upgrade")
    assert "factor_risk_exposure" in _observation_kind_check(engine)

    _run(engine, "downgrade")
    check = _observation_kind_check(engine)
    assert "factor_risk_exposure" not in check
    assert "macro_release" in check


def test_downgrade_refuses_to_discard_immutable_factor_risk_evidence() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _previous_schema(engine)
    _run(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO equity_observations (observation_id, observation_kind) "
                "VALUES (:observation_id, 'factor_risk_exposure')"
            ),
            {"observation_id": "a" * 64},
        )

    with pytest.raises(RuntimeError, match="immutable evidence exists"):
        _run(engine, "downgrade")
