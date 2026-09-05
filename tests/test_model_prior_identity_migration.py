"""Migration contracts for exact prior model-rebalance leg identity."""

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
    / "0096_model_prior_identity.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("model_prior_identity", _MIGRATION_PATH)
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


def _engine() -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    sa.event.listen(
        engine, "connect", lambda connection, _record: connection.execute("PRAGMA foreign_keys=ON")
    )
    return engine


def _schema(engine: sa.Engine, *, exact: bool) -> None:
    metadata = sa.MetaData()
    columns = (
        sa.Column("rebalance_id", sa.String(64), primary_key=True),
        sa.Column("sequence", sa.Integer, primary_key=True),
        sa.Column("leg_id", sa.String(128), nullable=False),
        sa.Column("instr_id", sa.Integer, nullable=False),
        sa.Column("factor_snapshot_id", sa.String(64), nullable=False),
        sa.Column("prior_model_rebalance_id", sa.String(64)),
        sa.Column("prior_model_leg_id", sa.String(128)),
    )
    constraints: list[sa.SchemaItem] = [
        sa.UniqueConstraint(
            "rebalance_id",
            "leg_id",
            name="uq_model_rebalance_leg_id",
        )
    ]
    if exact:
        constraints.extend(
            (
                sa.UniqueConstraint(
                    "rebalance_id",
                    "leg_id",
                    "instr_id",
                    "factor_snapshot_id",
                    name="uq_model_rebalance_leg_prior_identity",
                ),
                sa.ForeignKeyConstraint(
                    (
                        "prior_model_rebalance_id",
                        "prior_model_leg_id",
                        "instr_id",
                        "factor_snapshot_id",
                    ),
                    (
                        "model_rebalance_legs.rebalance_id",
                        "model_rebalance_legs.leg_id",
                        "model_rebalance_legs.instr_id",
                        "model_rebalance_legs.factor_snapshot_id",
                    ),
                    name="fk_model_rebalance_leg_prior_identity",
                    ondelete="RESTRICT",
                ),
            )
        )
    else:
        constraints.append(
            sa.ForeignKeyConstraint(
                ("prior_model_rebalance_id", "prior_model_leg_id"),
                (
                    "model_rebalance_legs.rebalance_id",
                    "model_rebalance_legs.leg_id",
                ),
                name="fk_model_rebalance_leg_prior_leg",
                ondelete="RESTRICT",
            )
        )
    sa.Table("model_rebalance_legs", metadata, *columns, *constraints)
    metadata.create_all(engine)


def _constraint_names(engine: sa.Engine) -> tuple[set[str], set[str]]:
    inspector = sa.inspect(engine)
    uniques = {
        str(item["name"])
        for item in inspector.get_unique_constraints("model_rebalance_legs")
        if item.get("name")
    }
    foreign_keys = {
        str(item["name"])
        for item in inspector.get_foreign_keys("model_rebalance_legs")
        if item.get("name")
    }
    return uniques, foreign_keys


def _insert_leg(
    connection: sa.Connection,
    *,
    rebalance_id: str,
    sequence: int,
    leg_id: str,
    instr_id: int,
    factor_snapshot_id: str,
    prior_rebalance_id: str | None = None,
    prior_leg_id: str | None = None,
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO model_rebalance_legs "
            "(rebalance_id, sequence, leg_id, instr_id, factor_snapshot_id, "
            "prior_model_rebalance_id, prior_model_leg_id) "
            "VALUES (:rebalance_id, :sequence, :leg_id, :instr_id, "
            ":factor_snapshot_id, :prior_rebalance_id, :prior_leg_id)"
        ),
        {
            "rebalance_id": rebalance_id,
            "sequence": sequence,
            "leg_id": leg_id,
            "instr_id": instr_id,
            "factor_snapshot_id": factor_snapshot_id,
            "prior_rebalance_id": prior_rebalance_id,
            "prior_leg_id": prior_leg_id,
        },
    )


def test_revision_follows_binding_total_exposure() -> None:
    migration = _load_migration()

    assert migration.revision == "0096_model_prior_identity"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0095_binding_total_exposure"


def test_upgrade_converges_legacy_prior_reference_and_downgrade_retains_it() -> None:
    engine = _engine()
    _schema(engine, exact=False)

    _run(engine, "upgrade")
    uniques, foreign_keys = _constraint_names(engine)
    assert "uq_model_rebalance_leg_prior_identity" in uniques
    assert "fk_model_rebalance_leg_prior_identity" in foreign_keys
    assert "fk_model_rebalance_leg_prior_leg" not in foreign_keys

    _run(engine, "downgrade")
    assert _constraint_names(engine) == (uniques, foreign_keys)


def test_upgrade_is_noop_for_fresh_exact_schema() -> None:
    engine = _engine()
    _schema(engine, exact=True)
    before = _constraint_names(engine)

    _run(engine, "upgrade")

    assert _constraint_names(engine) == before


def test_upgrade_refuses_mismatched_legacy_history() -> None:
    engine = _engine()
    _schema(engine, exact=False)
    with engine.begin() as connection:
        _insert_leg(
            connection,
            rebalance_id="a" * 64,
            sequence=0,
            leg_id="prior",
            instr_id=1,
            factor_snapshot_id="b" * 64,
        )
        _insert_leg(
            connection,
            rebalance_id="c" * 64,
            sequence=0,
            leg_id="child",
            instr_id=2,
            factor_snapshot_id="d" * 64,
            prior_rebalance_id="a" * 64,
            prior_leg_id="prior",
        )

    with pytest.raises(RuntimeError, match="mismatched history"):
        _run(engine, "upgrade")
