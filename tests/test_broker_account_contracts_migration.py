"""Contract tests for broker routing and credential invariants."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from lib_application.db.models.brokers import BrokerCredential, BrokerEnvironment

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0069_broker_account_contracts.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("broker_account_contracts", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_schema(engine: sa.Engine) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "broker_environments",
        metadata,
        sa.Column("broker_env_id", sa.Integer(), primary_key=True),
        sa.Column("broker_id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(20), nullable=False),
        sa.Column("region", sa.String(50)),
        sa.Column("base_urls", sa.JSON(), nullable=False),
        sa.Column("rate_limits", sa.JSON(), nullable=False),
    )
    sa.Table(
        "broker_credentials",
        metadata,
        sa.Column("cred_id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("secret_ref", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
    )
    metadata.create_all(engine)


def _run(engine: sa.Engine, operation: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, operation)()


def test_revision_follows_typed_broker_instrument_identity() -> None:
    migration = _load_migration()

    assert migration.revision == "0069_broker_account_contracts"
    assert migration.down_revision == "0068_typed_broker_instrument"


def test_model_declares_route_and_active_credential_uniqueness() -> None:
    environment_constraints = {
        constraint.name for constraint in BrokerEnvironment.__table__.constraints
    }
    credential_constraints = {
        constraint.name for constraint in BrokerCredential.__table__.constraints
    }
    credential_indexes = {index.name: index for index in BrokerCredential.__table__.indexes}

    assert "uq_broker_environment_route" in environment_constraints
    assert "ck_broker_environment_region" in environment_constraints
    assert BrokerEnvironment.__table__.c.region.nullable is False
    assert "uq_broker_credentials_secret_ref" in credential_constraints
    assert "ck_broker_credentials_secret_ref" in credential_constraints
    active_index = credential_indexes["uq_broker_credentials_active_account"]
    assert active_index.unique is True
    assert str(active_index.dialect_options["postgresql"]["where"]) == "status = 'active'"
    assert str(active_index.dialect_options["sqlite"]["where"]) == "status = 'active'"


def test_migration_round_trip_enforces_routes_and_active_credentials() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _legacy_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO broker_environments (
                    broker_env_id, broker_id, environment, region, base_urls, rate_limits
                ) VALUES (1, 3, 'live', 'india', '{}', '{}')
                """
            )
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO broker_credentials (cred_id, account_id, secret_ref, status)
                VALUES (1, 42, 'users/u/broker-accounts/42', 'active')
                """
            )
        )

    _run(engine, "upgrade")
    inspector = sa.inspect(engine)
    environment_uniques = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("broker_environments")
    }
    credential_uniques = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("broker_credentials")
    }
    credential_indexes = {
        index["name"]: index for index in inspector.get_indexes("broker_credentials")
    }

    assert ("broker_id", "environment", "region") in environment_uniques
    assert ("secret_ref",) in credential_uniques
    assert credential_indexes["uq_broker_credentials_active_account"]["unique"] == 1
    assert (
        next(
            column
            for column in inspector.get_columns("broker_environments")
            if column["name"] == "region"
        )["nullable"]
        is False
    )

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO broker_credentials (cred_id, account_id, secret_ref, status)
                VALUES (2, 42, 'users/u/broker-accounts/42/retired', 'disabled')
                """
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                sa.text(
                    """
                    INSERT INTO broker_credentials (cred_id, account_id, secret_ref, status)
                    VALUES (3, 42, 'users/u/broker-accounts/42/new', 'active')
                    """
                )
            )

    _run(engine, "downgrade")
    downgraded = sa.inspect(engine)
    assert all(
        index["name"] != "uq_broker_credentials_active_account"
        for index in downgraded.get_indexes("broker_credentials")
    )
    assert (
        next(
            column
            for column in downgraded.get_columns("broker_environments")
            if column["name"] == "region"
        )["nullable"]
        is True
    )


@pytest.mark.parametrize(
    ("table", "values", "error"),
    [
        (
            "broker_environments",
            "(1, 3, 'live', NULL, '{}', '{}')",
            "null or blank region",
        ),
        (
            "broker_credentials",
            "(1, 42, '', 'active')",
            "blank secret_ref",
        ),
    ],
)
def test_upgrade_rejects_invalid_existing_rows(
    table: str,
    values: str,
    error: str,
) -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _legacy_schema(engine)
    columns = {
        "broker_environments": (
            "broker_env_id, broker_id, environment, region, base_urls, rate_limits"
        ),
        "broker_credentials": "cred_id, account_id, secret_ref, status",
    }
    with engine.begin() as connection:
        connection.execute(sa.text(f"INSERT INTO {table} ({columns[table]}) VALUES {values}"))

    with pytest.raises(RuntimeError, match=error):
        _run(engine, "upgrade")


class _GrantRecorder:
    def __init__(self) -> None:
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        self.statements: list[str] = []

    def get_bind(self) -> Any:
        return self.bind

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


def test_backend_rotation_grant_is_tenant_scoped() -> None:
    migration = _load_migration()
    recorder = _GrantRecorder()
    migration.op = recorder

    migration._grant_backend_rotation()

    sql = "\n".join(recorder.statements)
    assert "GRANT UPDATE ON TABLE public.broker_credentials TO vm_backend" in sql
    assert "FOR UPDATE TO vm_backend" in sql
    assert "current_setting('app.current_tenant', true)" in sql
    assert "WITH CHECK" in sql
