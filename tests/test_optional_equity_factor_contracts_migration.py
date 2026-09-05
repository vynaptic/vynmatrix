"""Forward-migration contracts for optional point-in-time equity factors."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from lib_strategy.equity_optional_factors import (
    OPTIONAL_FACTOR_SOURCE_CONTRACTS,
    optional_factor_source_registry_sha256,
)

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0090_optional_equity_factor_contracts.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "optional_equity_factor_contracts_migration",
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
        "users",
        metadata,
        sa.Column("user_id", sa.String(50), primary_key=True),
    )
    sa.Table(
        "equity_source_lineages",
        metadata,
        sa.Column("lineage_id", sa.String(64), primary_key=True),
    )
    sa.Table(
        "equity_observations",
        metadata,
        sa.Column("observation_id", sa.String(64), primary_key=True),
        sa.Column("observation_kind", sa.String(30), nullable=False),
        sa.CheckConstraint(
            "observation_kind IN ('price', 'corporate_action', 'membership', "
            "'filing', 'xbrl_fact', 'benchmark', 'calendar', 'earnings_event', "
            "'market_cap', 'security_identity')",
            name="ck_equity_observation_kind",
        ),
    )
    sa.Table(
        "equity_factor_snapshots",
        metadata,
        sa.Column("factor_snapshot_id", sa.String(64), primary_key=True),
        sa.Column("configuration_digest", sa.String(64), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "length(factor_snapshot_id) = 64 AND length(configuration_digest) = 64 "
            "AND length(content_sha256) = 64",
            name="ck_equity_factor_digests",
        ),
    )
    metadata.create_all(engine)


def test_revision_follows_account_execution_fence_and_pins_runtime_registry() -> None:
    migration = _load_migration()

    assert migration.revision == "0090_optional_factor_contracts"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0089_account_execution_fence"
    assert optional_factor_source_registry_sha256() == migration._REGISTRY_SHA256
    assert set(migration._OPTIONAL_KINDS) == {
        contract.observation_kind.value for contract in OPTIONAL_FACTOR_SOURCE_CONTRACTS
    }


def test_upgrade_adds_owner_and_registry_identity_and_backfills_existing_rows() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _create_previous_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO equity_factor_snapshots "
                "(factor_snapshot_id, configuration_digest, content_sha256) "
                "VALUES (:factor_snapshot_id, :configuration_digest, :content_sha256)"
            ),
            {
                "factor_snapshot_id": "a" * 64,
                "configuration_digest": "b" * 64,
                "content_sha256": "a" * 64,
            },
        )

    _run(engine, "upgrade")
    inspector = sa.inspect(engine)
    lineage_columns = {column["name"] for column in inspector.get_columns("equity_source_lineages")}
    factor_columns = {column["name"] for column in inspector.get_columns("equity_factor_snapshots")}
    assert "entitlement_owner_user_id" in lineage_columns
    assert "source_contract_registry_sha256" in factor_columns
    observation_check = next(
        constraint["sqltext"]
        for constraint in inspector.get_check_constraints("equity_observations")
        if constraint["name"] == "ck_equity_observation_kind"
    )
    assert all(
        contract.observation_kind.value in observation_check
        for contract in OPTIONAL_FACTOR_SOURCE_CONTRACTS
    )
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT source_contract_registry_sha256 FROM equity_factor_snapshots")
            )
            == optional_factor_source_registry_sha256()
        )


def test_empty_schema_round_trips_without_rewriting_historical_revisions() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _create_previous_schema(engine)

    _run(engine, "upgrade")
    _run(engine, "downgrade")

    inspector = sa.inspect(engine)
    assert "entitlement_owner_user_id" not in {
        column["name"] for column in inspector.get_columns("equity_source_lineages")
    }
    assert "source_contract_registry_sha256" not in {
        column["name"] for column in inspector.get_columns("equity_factor_snapshots")
    }
    observation_check = next(
        constraint["sqltext"]
        for constraint in inspector.get_check_constraints("equity_observations")
        if constraint["name"] == "ck_equity_observation_kind"
    )
    assert "analyst_estimate" not in observation_check
    assert "security_identity" in observation_check


def test_downgrade_refuses_to_discard_optional_immutable_evidence() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _create_previous_schema(engine)
    _run(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO equity_observations (observation_id, observation_kind) "
                "VALUES (:observation_id, 'news_event')"
            ),
            {"observation_id": "c" * 64},
        )

    with pytest.raises(RuntimeError, match="immutable evidence exists"):
        _run(engine, "downgrade")
