"""Commercial metadata retirement must preserve identity and refuse populated state."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from lib_application import db
from lib_application.db import models

_RETIRED = {"orgs", "plans", "user_roles", "user_plan_subscriptions"}
_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts/db/alembic/versions/0103_remove_commercial_tenancy.py"
)


def _migration(connection: sa.Connection) -> ModuleType:
    assert _PATH.is_file(), "The commercial tenancy retirement revision must exist"
    spec = importlib.util.spec_from_file_location("commercial_tenancy_retirement", _PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__dict__["op"] = Operations(MigrationContext.configure(connection))
    return module


@pytest.fixture
def legacy() -> Iterator[sa.Connection]:
    """An independent pre-retirement schema with durable historical references."""
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE orgs (org_id INTEGER PRIMARY KEY, name TEXT NOT NULL)",
            "CREATE TABLE plans (plan_id INTEGER PRIMARY KEY, code TEXT NOT NULL, features JSON)",
            "CREATE TABLE users (user_id VARCHAR(50) PRIMARY KEY, email TEXT NOT NULL, "
            "org_id INTEGER, CONSTRAINT users_org_id_fkey FOREIGN KEY(org_id) REFERENCES orgs)",
            "CREATE TABLE user_roles (user_id VARCHAR(50) REFERENCES users, role TEXT NOT NULL, "
            "PRIMARY KEY(user_id, role))",
            "CREATE TABLE user_plan_subscriptions (sub_id INTEGER PRIMARY KEY, "
            "user_id VARCHAR(50) REFERENCES users, plan_id INTEGER REFERENCES plans, status TEXT)",
            "CREATE TABLE linked_broker_accounts (account_id INTEGER PRIMARY KEY, "
            "user_id VARCHAR(50) NOT NULL REFERENCES users)",
            "CREATE TABLE execution_logs (execution_id TEXT PRIMARY KEY, "
            "account_id INTEGER NOT NULL REFERENCES linked_broker_accounts, payload TEXT)",
            "INSERT INTO users VALUES ('historical-id', 'historical@example.invalid', NULL)",
            "INSERT INTO linked_broker_accounts VALUES (901, 'historical-id')",
            "INSERT INTO execution_logs VALUES ('durable-fill-id', 901, 'preserved-history')",
        ):
            connection.exec_driver_sql(statement)
        yield connection
    engine.dispose()


def test_runtime_identity_retains_account_and_trading_authority_without_commercial_exports() -> (
    None
):
    assert _RETIRED.isdisjoint(models.Base.metadata.tables)
    assert "org_id" not in models.User.__table__.c
    for name in ("Organization", "Plan", "UserRole", "UserPlanSubscription"):
        assert not hasattr(models, name)
        assert not hasattr(db, name)
    relationships = set(sa.inspect(models.User).relationships.keys())
    assert {"organization", "roles", "subscriptions"}.isdisjoint(relationships)
    assert {"linked_accounts", "trading_policies", "budget_buckets", "strategy_configs"} <= (
        relationships
    )
    assert {"user_trading_policies", "risk_mandates", "linked_broker_accounts"} <= (
        models.Base.metadata.tables.keys()
    )


def test_empty_retirement_round_trips_and_preserves_historical_identity(
    legacy: sa.Connection,
) -> None:
    migration = _migration(legacy)
    assert migration.revision == "0103_remove_commercial_tenancy"
    assert migration.down_revision == "0102_owner_control_plane"
    for _ in range(2):
        migration.upgrade()
        inspector = sa.inspect(legacy)
        assert _RETIRED.isdisjoint(inspector.get_table_names())
        assert "org_id" not in {column["name"] for column in inspector.get_columns("users")}
        assert legacy.exec_driver_sql("SELECT * FROM users").tuples().all() == [
            ("historical-id", "historical@example.invalid")
        ]
        assert legacy.exec_driver_sql("SELECT * FROM linked_broker_accounts").tuples().all() == [
            (901, "historical-id")
        ]
        assert legacy.exec_driver_sql("SELECT * FROM execution_logs").tuples().all() == [
            ("durable-fill-id", 901, "preserved-history")
        ]
        migration.downgrade()
        inspector = sa.inspect(legacy)
        assert set(inspector.get_table_names()) >= _RETIRED
        org_id = next(
            column for column in inspector.get_columns("users") if column["name"] == "org_id"
        )
        assert org_id["nullable"] is True
        assert any(fk["referred_table"] == "orgs" for fk in inspector.get_foreign_keys("users"))
        assert legacy.exec_driver_sql("SELECT org_id FROM users").scalar_one() is None
        for table in _RETIRED:
            assert legacy.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() == 0


def test_user_owner_uniqueness_and_currency_checks_survive_the_round_trip() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.insert(models.User).values(
                    user_id="designated",
                    email="owner@example.invalid",
                    base_ccy="EUR",
                    is_deployment_owner=True,
                )
            )
            migration = _migration(connection)
            migration.downgrade()
            migration.upgrade()
            assert connection.exec_driver_sql(
                "SELECT user_id, base_ccy, is_deployment_owner FROM users"
            ).one() == ("designated", "EUR", True)
            for user_id, currency, owner in (("second", "EUR", True), ("invalid", "eur", False)):
                with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
                    connection.execute(
                        sa.insert(models.User).values(
                            user_id=user_id,
                            email=f"{user_id}@example.invalid",
                            base_ccy=currency,
                            is_deployment_owner=owner,
                        )
                    )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("statement", "blocked"),
    [
        ("INSERT INTO orgs VALUES (1, 'Legacy organization')", "orgs=1"),
        ("INSERT INTO plans VALUES (1, 'legacy-plan', '{}')", "plans=1"),
        ("INSERT INTO user_roles VALUES ('historical-id', 'trader')", "user_roles=1"),
        (
            "INSERT INTO user_plan_subscriptions VALUES (1, 'historical-id', NULL, 'canceled')",
            "user_plan_subscriptions=1",
        ),
        ("UPDATE users SET org_id = 7", "users.org_id=1"),
    ],
)
def test_every_populated_commercial_surface_blocks_before_ddl(
    legacy: sa.Connection, statement: str, blocked: str
) -> None:
    legacy.exec_driver_sql(statement)
    statements: list[str] = []

    def capture(
        _conn: sa.Connection,
        _cursor: Any,
        sql: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(sql.strip().upper())

    sa.event.listen(legacy, "before_cursor_execute", capture)
    try:
        with pytest.raises(RuntimeError, match=blocked):
            _migration(legacy).upgrade()
    finally:
        sa.event.remove(legacy, "before_cursor_execute", capture)
    assert not any(sql.startswith(("ALTER ", "DROP ", "CREATE ", "DELETE ")) for sql in statements)
    assert set(sa.inspect(legacy).get_table_names()) >= _RETIRED
    assert "org_id" in {column["name"] for column in sa.inspect(legacy).get_columns("users")}
    assert legacy.exec_driver_sql("SELECT * FROM execution_logs").one() == (
        "durable-fill-id",
        901,
        "preserved-history",
    )


@pytest.fixture
def postgres_connection() -> Iterator[sa.Connection]:
    """An explicit unconfigured head; all fixture rows and DDL roll back."""
    url = os.environ.get("OWNER_CONTROL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("OWNER_CONTROL_TEST_DATABASE_URL requires isolated PostgreSQL acceptance")
    engine = sa.create_engine(url, hide_parameters=True)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.fail("Commercial retirement acceptance requires PostgreSQL")
    config = Config()
    config.set_main_option("script_location", str(_PATH.parent.parent))
    head = ScriptDirectory.from_config(config).get_current_head()
    try:
        with engine.connect() as connection, connection.begin():
            assert MigrationContext.configure(connection).get_current_revision() == head
            assert (
                connection.scalar(
                    sa.text("SELECT count(*) FROM public.users WHERE is_deployment_owner")
                )
                == 0
            ), "Use an isolated unconfigured database for retirement acceptance"
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT pg_get_userbyid(relowner)=current_user FROM pg_class WHERE oid='public.users'::regclass"
                    )
                )
                is True
            ), "Use the migration identity that owns the schema"
            connection.execute(sa.text("SET LOCAL lock_timeout='2s'"))
            yield connection
            connection.rollback()
    finally:
        engine.dispose()


def _preserved_state(connection: sa.Connection) -> dict[str, str]:
    """Hash rows so a failed comparison never prints credentials or private payloads."""
    result = {}
    for model in (
        models.User,
        models.Broker,
        models.LinkedBrokerAccount,
        models.BrokerCredential,
        models.ManagedSecret,
        models.Strategy,
        models.StrategyVersion,
        models.ExecutionLog,
        models.OrderIntent,
        models.Order,
        models.Execution,
    ):
        table = models.Base.metadata.tables[model.__tablename__]
        rows = connection.execute(sa.select(table).order_by(*table.primary_key.columns)).mappings()
        payload = json.dumps([dict(row) for row in rows], sort_keys=True, default=str)
        result[table.name] = hashlib.sha256(payload.encode()).hexdigest()
    return result


def _preservation_rows(connection: sa.Connection) -> str:
    """Schema fixtures only: disabled release, owner/account links, no submitted order or fill."""
    suffix = uuid4().hex[:16]
    owner_id = f"retirement-owner:{suffix}"
    strategy_id = f"retirement-{suffix}"
    secret_ref = f"retirement-fixture/{suffix}"
    connection.execute(
        sa.insert(models.User).values(
            user_id=owner_id,
            email=f"{suffix}@retirement.invalid",
            base_ccy="EUR",
            is_deployment_owner=True,
        )
    )
    broker_id = connection.scalar(
        sa.insert(models.Broker)
        .values(
            code=strategy_id,
            name="Retirement schema fixture",
            capabilities={},
        )
        .returning(models.Broker.broker_id)
    )
    account_id = connection.scalar(
        sa.insert(models.LinkedBrokerAccount)
        .values(
            user_id=owner_id,
            broker_id=broker_id,
            environment="paper",
            config_key="retirement.paper",
            display_name="Retirement schema fixture",
            base_ccy="EUR",
            paper_initial_equity=1000,
            paper_initial_cash=900,
        )
        .returning(models.LinkedBrokerAccount.account_id)
    )
    connection.execute(
        sa.insert(models.BrokerCredential).values(
            account_id=account_id,
            secret_ref=secret_ref,
        )
    )
    connection.execute(
        sa.insert(models.ManagedSecret).values(
            account_id=account_id,
            secret_ref=secret_ref,
            ciphertext="opaque-schema-fixture",
        )
    )
    connection.execute(
        sa.insert(models.Strategy).values(
            strategy_id=strategy_id,
            strategy_name="Retirement fixture",
            asset_class="crypto",
            is_active=False,
        )
    )
    connection.execute(
        sa.insert(models.StrategyVersion).values(
            strategy_id=strategy_id,
            semver="1.0.0",
            param_schema={},
            default_params={},
            status="pulled",
        )
    )
    connection.execute(
        sa.insert(models.ExecutionLog).values(
            log_id=f"retirement-log:{suffix}",
            user_id=owner_id,
            account_id=account_id,
            strategy_id=strategy_id,
            signal_type="hold",
            execution_mode="paper",
            status="pending",
            execution_details={"fixture": "schema-preservation", "order_submitted": False},
        )
    )
    return owner_id


@pytest.mark.integration
def test_postgres_retirement_round_trip_preserves_current_owner_account_and_history(
    postgres_connection: sa.Connection,
) -> None:
    connection = postgres_connection
    owner_id = _preservation_rows(connection)
    before = _preserved_state(connection)
    migration = _migration(connection)
    for _ in range(2):
        migration.downgrade()
        assert _preserved_state(connection) == before
        assert (
            connection.scalar(sa.text("SELECT count(*) FROM public.users WHERE org_id IS NOT NULL"))
            == 0
        )
        for table in _RETIRED:
            assert connection.scalar(sa.text(f"SELECT count(*) FROM public.{table}")) == 0
        migration.upgrade()
        assert _RETIRED.isdisjoint(sa.inspect(connection).get_table_names())
        assert connection.scalar(sa.text("SELECT public.vm_deployment_owner_id()")) == owner_id
        assert _preserved_state(connection) == before
    with pytest.raises(sa.exc.IntegrityError) as conflict, connection.begin_nested():
        connection.execute(
            sa.insert(models.User).values(
                user_id=f"retirement-second:{uuid4().hex[:16]}",
                email=f"{uuid4().hex}@retirement.invalid",
                base_ccy="EUR",
                is_deployment_owner=True,
            )
        )
    assert (
        getattr(conflict.value.orig, "sqlstate", None)
        or getattr(conflict.value.orig, "pgcode", None)
    ) == "23505"


def _populate_commercial_surface(connection: sa.Connection, surface: str) -> None:
    user_id = f"retirement-guard:{uuid4().hex[:16]}"
    connection.execute(
        sa.insert(models.User).values(
            user_id=user_id,
            email=f"{user_id}@retirement.invalid",
            base_ccy="EUR",
        )
    )
    if surface in {"orgs", "users.org_id"}:
        org_id = connection.scalar(
            sa.text(
                "INSERT INTO public.orgs(name) VALUES ('Retained organization') RETURNING org_id"
            )
        )
        if surface == "users.org_id":
            connection.execute(
                sa.text("UPDATE public.users SET org_id=:org WHERE user_id=:user"),
                {"org": org_id, "user": user_id},
            )
    elif surface in {"plans", "user_plan_subscriptions"}:
        plan_id = connection.scalar(
            sa.text(
                "INSERT INTO public.plans(code,features) VALUES ('retained-plan','{}'::json) RETURNING plan_id"
            )
        )
        if surface == "user_plan_subscriptions":
            connection.execute(
                sa.text(
                    "INSERT INTO public.user_plan_subscriptions(user_id,plan_id,status) VALUES (:user,:plan,'canceled')"
                ),
                {"user": user_id, "plan": plan_id},
            )
    else:
        connection.execute(
            sa.text("INSERT INTO public.user_roles(user_id,role) VALUES (:user,'trader')"),
            {"user": user_id},
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "surface", ["orgs", "plans", "user_roles", "user_plan_subscriptions", "users.org_id"]
)
def test_postgres_retirement_refuses_fk_valid_populated_state_before_ddl(
    postgres_connection: sa.Connection,
    surface: str,
) -> None:
    connection = postgres_connection
    migration = _migration(connection)
    migration.downgrade()
    _populate_commercial_surface(connection, surface)
    before = _preserved_state(connection)
    statements: list[str] = []

    def capture(_conn: Any, _cursor: Any, sql: str, *_args: Any) -> None:
        statements.append(sql.strip().upper())

    sa.event.listen(connection, "before_cursor_execute", capture)
    try:
        with pytest.raises(RuntimeError, match=re.escape(f"{surface}=1")):
            migration.upgrade()
    finally:
        sa.event.remove(connection, "before_cursor_execute", capture)
    assert not any(sql.startswith(("ALTER ", "DROP ", "CREATE ", "DELETE ")) for sql in statements)
    assert set(sa.inspect(connection).get_table_names()) >= _RETIRED
    assert "org_id" in {column["name"] for column in sa.inspect(connection).get_columns("users")}
    assert _preserved_state(connection) == before
    query = (
        "SELECT count(*) FROM public.users WHERE org_id IS NOT NULL"
        if surface == "users.org_id"
        else f"SELECT count(*) FROM public.{surface}"
    )
    assert connection.scalar(sa.text(query)) == 1


@pytest.mark.integration
def test_postgres_recreated_commercial_tables_have_no_runtime_entitlements(
    postgres_connection: sa.Connection,
) -> None:
    connection = postgres_connection
    _migration(connection).downgrade()
    for role in (
        "vm_backend",
        "vm_scoring",
        "vm_execution",
        "vm_feedback",
        "vm_market_data",
        "vm_indicator",
    ):
        for table in _RETIRED:
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert (
                    connection.scalar(
                        sa.text("SELECT has_table_privilege(:role,:table,:privilege)"),
                        {"role": role, "table": f"public.{table}", "privilege": privilege},
                    )
                    is False
                )
    for table in ("user_roles", "user_plan_subscriptions"):
        assert (
            connection.scalar(
                sa.text("SELECT relrowsecurity FROM pg_class WHERE oid=CAST(:table AS regclass)"),
                {"table": f"public.{table}"},
            )
            is True
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM pg_policies WHERE schemaname='public' AND tablename=:table"
                ),
                {"table": table},
            )
            == 0
        )
