"""Migration contract for account-scoped daily NAV observations."""

from __future__ import annotations

import importlib.util
from contextlib import AbstractContextManager
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0061_account_scoped_daily_nav.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "account_scoped_daily_nav_migration",
        _MIGRATION_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _BatchRecorder(AbstractContextManager["_BatchRecorder"]):
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __enter__(self) -> _BatchRecorder:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return record


class _OpRecorder:
    def __init__(self, bind: Any) -> None:
        self.bind = bind
        self.batch = _BatchRecorder()

    def get_bind(self) -> Any:
        return self.bind

    def batch_alter_table(self, table: str) -> _BatchRecorder:
        assert table == "daily_nav"
        return self.batch

    def execute(self, statement: Any) -> None:
        self.bind.execute(sa.text(str(statement)))


def test_revision_follows_paper_account_capital() -> None:
    migration = _load_migration()

    assert migration.revision == "0061_account_scoped_daily_nav"
    assert migration.down_revision == "0060_paper_account_capital"


def test_upgrade_refuses_unattributed_legacy_rows() -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE daily_nav ("
                "nav_id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, date DATE NOT NULL)"
            )
        )
        connection.execute(
            sa.text("INSERT INTO daily_nav (nav_id, user_id, date) VALUES (1, 'u1', '2026-07-25')")
        )
        migration.op = _OpRecorder(connection)

        with pytest.raises(RuntimeError, match="have no broker account attribution"):
            migration.upgrade()


def test_upgrade_replaces_user_date_uniqueness_with_owned_account_scope() -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE daily_nav ("
                "nav_id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, date DATE NOT NULL)"
            )
        )
        recorder = _OpRecorder(connection)
        migration.op = recorder

        migration.upgrade()

    calls = recorder.batch.calls
    assert ("drop_constraint", ("uq_daily_nav",), {"type_": "unique"}) in calls
    assert any(
        name == "add_column"
        and args
        and isinstance(args[0], sa.Column)
        and args[0].name == "account_id"
        and not args[0].nullable
        for name, args, _ in calls
    )
    assert (
        "create_unique_constraint",
        ("uq_daily_nav_account_date", ["account_id", "date"]),
        {},
    ) in calls
    assert any(
        name == "create_foreign_key"
        and args[:4]
        == (
            "fk_daily_nav_account_owner",
            "linked_broker_accounts",
            ["account_id", "user_id"],
            ["account_id", "user_id"],
        )
        for name, args, _ in calls
    )


def test_upgrade_and_downgrade_round_trip_on_empty_legacy_table() -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table(
        "users",
        metadata,
        sa.Column("user_id", sa.String(50), primary_key=True),
    )
    sa.Table(
        "linked_broker_accounts",
        metadata,
        sa.Column("account_id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.String(50), nullable=False),
        sa.UniqueConstraint(
            "account_id",
            "user_id",
            name="uq_linked_broker_account_owner",
        ),
    )
    sa.Table(
        "daily_nav",
        metadata,
        sa.Column("nav_id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.String(50), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("nav_ccy", sa.String(10), nullable=False),
        sa.Column("nav_value", sa.Numeric(20, 2), nullable=False),
        sa.Column("ret_1d", sa.Numeric(10, 6)),
        sa.Column("drawdown", sa.Numeric(10, 6)),
        sa.Column("benchmark", sa.String(50)),
        sa.Column("bench_ret_1d", sa.Numeric(10, 6)),
        sa.UniqueConstraint("user_id", "date", name="uq_daily_nav"),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        inspector = sa.inspect(connection)
        assert inspector.get_columns("daily_nav")[-1]["name"] == "account_id"
        assert inspector.get_columns("daily_nav")[-1]["nullable"] is False
        assert {
            unique["name"]: tuple(unique["column_names"])
            for unique in inspector.get_unique_constraints("daily_nav")
        } == {"uq_daily_nav_account_date": ("account_id", "date")}
        assert {foreign_key["name"] for foreign_key in inspector.get_foreign_keys("daily_nav")} >= {
            "fk_daily_nav_account",
            "fk_daily_nav_account_owner",
        }

        migration.downgrade()
        inspector = sa.inspect(connection)
        assert "account_id" not in {column["name"] for column in inspector.get_columns("daily_nav")}
        assert {
            unique["name"]: tuple(unique["column_names"])
            for unique in inspector.get_unique_constraints("daily_nav")
        } == {"uq_daily_nav": ("user_id", "date")}
