"""Contract tests for explicit paper-account capital migration 0060."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0060_paper_account_capital.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "paper_account_capital_migration", _MIGRATION_PATH
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


def test_revision_follows_fail_closed_onboarding() -> None:
    migration = _load_migration()

    assert migration.revision == "0060_paper_account_capital"
    assert migration.down_revision == "0059_fail_closed_onboarding"


def _create_accounts_table(engine: sa.Engine) -> sa.Table:
    metadata = sa.MetaData()
    accounts = sa.Table(
        "linked_broker_accounts",
        metadata,
        sa.Column("account_id", sa.Integer, primary_key=True),
        sa.Column("environment", sa.String(20), nullable=False),
    )
    metadata.create_all(engine)
    return accounts


def test_migration_refuses_to_guess_capital_for_existing_paper_account() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    accounts = _create_accounts_table(engine)
    with engine.begin() as connection:
        connection.execute(accounts.insert().values(account_id=1, environment="paper"))

    with pytest.raises(RuntimeError, match="remove and re-onboard"):
        _run(engine, "upgrade")


def test_migration_enforces_capital_by_environment() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    accounts = _create_accounts_table(engine)
    with engine.begin() as connection:
        connection.execute(accounts.insert().values(account_id=2, environment="live"))

    _run(engine, "upgrade")

    columns = {
        column["name"]: column
        for column in sa.inspect(engine).get_columns("linked_broker_accounts")
    }
    assert columns["paper_initial_equity"]["nullable"] is True
    assert columns["paper_initial_cash"]["nullable"] is True
    with engine.connect() as connection:
        assert connection.execute(
            sa.text(
                "SELECT account_id, paper_initial_equity, paper_initial_cash "
                "FROM linked_broker_accounts ORDER BY account_id"
            )
        ).all() == [(2, None, None)]

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO linked_broker_accounts "
                "(account_id, environment, paper_initial_equity, paper_initial_cash) "
                "VALUES (1, 'paper', 10000, 8000)"
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO linked_broker_accounts "
                "(account_id, environment, paper_initial_equity, paper_initial_cash) "
                "VALUES (3, 'paper', 10000, 12000)"
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO linked_broker_accounts "
                "(account_id, environment, paper_initial_equity, paper_initial_cash) "
                "VALUES (4, 'paper', NULL, NULL)"
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE linked_broker_accounts "
                "SET paper_initial_equity=10000, paper_initial_cash=8000 "
                "WHERE account_id=2"
            )
        )

    with pytest.raises(RuntimeError, match="legacy runtime invent financial state"):
        _run(engine, "downgrade")

    retained = {
        column["name"] for column in sa.inspect(engine).get_columns("linked_broker_accounts")
    }
    assert {"paper_initial_equity", "paper_initial_cash"} <= retained


def test_downgrade_removes_capital_columns_when_no_paper_accounts_exist() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    accounts = _create_accounts_table(engine)
    with engine.begin() as connection:
        connection.execute(accounts.insert().values(account_id=2, environment="live"))

    _run(engine, "upgrade")
    _run(engine, "downgrade")

    downgraded = {
        column["name"] for column in sa.inspect(engine).get_columns("linked_broker_accounts")
    }
    assert "paper_initial_equity" not in downgraded
    assert "paper_initial_cash" not in downgraded
