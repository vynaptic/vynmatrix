"""Contract tests for the Saxo broker-catalogue migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from lib_application.db.models import Base
from lib_application.db.models.brokers import Broker, BrokerEnvironment, LinkedBrokerAccount
from lib_application.db.models.identity import User

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0066_saxo_broker_catalogue.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("saxo_broker_catalogue", _MIGRATION_PATH)
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


def test_revision_follows_strategy_metadata_retirement() -> None:
    migration = _load_migration()

    assert migration.revision == "0066_saxo_broker_catalogue"
    assert migration.down_revision == "0065_retire_strategy_metadata"


def test_saxo_catalogue_round_trip_has_exact_environment_hosts() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    _run(engine, "upgrade")
    with engine.begin() as connection:
        broker = connection.execute(sa.select(Broker).where(Broker.code == "saxo")).scalar_one()
        environments = list(
            connection.execute(
                sa.select(BrokerEnvironment)
                .where(BrokerEnvironment.broker_id == broker)
                .order_by(BrokerEnvironment.environment)
            ).all()
        )

    assert len(environments) == 2
    assert environments[0].environment == "live"
    assert environments[0].base_urls["rest"] == "https://gateway.saxobank.com/openapi"
    assert environments[1].environment == "paper"
    assert environments[1].base_urls["rest"] == "https://gateway.saxobank.com/sim/openapi"

    _run(engine, "downgrade")
    with engine.begin() as connection:
        remaining = connection.execute(
            sa.select(sa.func.count()).select_from(Broker).where(Broker.code == "saxo")
        ).scalar_one()
    assert remaining == 0


def test_saxo_catalogue_downgrade_refuses_linked_accounts() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _run(engine, "upgrade")
    with engine.begin() as connection:
        broker_id = connection.execute(
            sa.select(Broker.broker_id).where(Broker.code == "saxo")
        ).scalar_one()
        connection.execute(
            sa.insert(User).values(
                user_id="saxo-user",
                email="saxo@example.com",
                base_ccy="EUR",
            )
        )
        connection.execute(
            sa.insert(LinkedBrokerAccount).values(
                user_id="saxo-user",
                broker_id=broker_id,
                environment="live",
                display_name="Saxo live",
                base_ccy="EUR",
                status="connected",
            )
        )

    with pytest.raises(RuntimeError, match="linked account"):
        _run(engine, "downgrade")
