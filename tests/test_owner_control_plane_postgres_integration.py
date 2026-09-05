"""Opt-in backend role acceptance against an isolated database migrated through 0102.

All fixture data and assertions roll back; this module never provisions databases,
roles, or migrations. OWNER_CONTROL_TEST_DATABASE_URL must identify an unconfigured
isolated test database using its migration identity.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from execution_engine.account_execution_serializer import AccountExecutionSerializer
from lib_application.db.models import (
    ApiAuditLog,
    Broker,
    BrokerCredential,
    ExecutionLog,
    LinkedBrokerAccount,
    Strategy,
    User,
    UserStrategyBinding,
)

pytestmark = pytest.mark.integration
_OWNER = "backend-owner:textual"
_PEER = "historical-owner:textual"


@pytest.fixture
def database() -> Iterator[tuple[sa.Connection, int, int]]:
    url = os.environ.get("OWNER_CONTROL_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "OWNER_CONTROL_TEST_DATABASE_URL requires explicit isolated PostgreSQL acceptance"
        )
    engine = sa.create_engine(url, hide_parameters=True)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.fail("Owner control-plane acceptance requires PostgreSQL")
    try:
        with engine.connect() as connection, connection.begin():
            assert (
                connection.scalar(
                    sa.text("SELECT count(*) FROM public.users WHERE is_deployment_owner")
                )
                == 0
            ), "Use an isolated unconfigured acceptance database"
            with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                session.add_all(
                    [
                        User(
                            user_id=_OWNER,
                            email="owner@control.invalid",
                            is_deployment_owner=True,
                            base_ccy="EUR",
                        ),
                        User(user_id=_PEER, email="history@control.invalid", base_ccy="EUR"),
                    ]
                )
                broker = Broker(code="owner-control-test", name="Owner control acceptance")
                session.add(broker)
                session.flush()
                accounts = [
                    LinkedBrokerAccount(
                        user_id=user_id,
                        broker_id=broker.broker_id,
                        environment="paper",
                        display_name=f"Account {user_id}",
                        base_ccy="EUR",
                        paper_initial_equity=10000,
                        paper_initial_cash=10000,
                    )
                    for user_id in (_OWNER, _PEER)
                ]
                session.add_all(accounts)
                session.flush()
                for account in accounts:
                    session.add(
                        BrokerCredential(
                            account_id=account.account_id,
                            secret_ref=f"acceptance/{account.account_id}",
                        )
                    )
                    session.add(
                        UserStrategyBinding(
                            user_id=account.user_id,
                            broker_account_id=account.account_id,
                        )
                    )
                    session.add(
                        ApiAuditLog(
                            user_id=account.user_id, action="acceptance", req={}, status="ok"
                        )
                    )
                session.add(Strategy(strategy_id="owner-control-test", strategy_name="Acceptance"))
                session.flush()
                owned_id, peer_id = [int(account.account_id) for account in accounts]
                session.commit()
            yield connection, owned_id, peer_id
            connection.rollback()
    finally:
        engine.dispose()


def _as_backend(connection: sa.Connection, user_id: str = _OWNER) -> None:
    connection.execute(sa.text("SET LOCAL ROLE vm_backend"))
    connection.execute(
        sa.text("SELECT set_config('app.current_tenant', :owner, true)"),
        {
            "owner": user_id,
        },
    )


def _denied(
    connection: sa.Connection, statement: str, *, code: str = "42501", **params: Any
) -> None:
    with pytest.raises(sa.exc.DBAPIError) as error, connection.begin_nested():
        connection.execute(sa.text(statement), params)
    assert (
        getattr(error.value.orig, "sqlstate", None) or getattr(error.value.orig, "pgcode", None)
    ) == code


def test_backend_owner_and_joined_rows_ignore_forged_tenant_scope(
    database: tuple[sa.Connection, int, int],
) -> None:
    connection, owned_id, peer_id = database
    _as_backend(connection)
    assert connection.scalar(sa.text("SELECT user_id FROM public.users")) == _OWNER
    assert (
        connection.scalar(sa.text("SELECT account_id FROM public.linked_broker_accounts"))
        == owned_id
    )
    assert (
        connection.scalar(sa.text("SELECT account_id FROM public.broker_credentials")) == owned_id
    )
    assert connection.scalar(sa.text("SELECT user_id FROM public.user_strategy_bindings")) == _OWNER
    _as_backend(connection, _PEER)
    for table in (
        "users",
        "linked_broker_accounts",
        "broker_credentials",
        "user_strategy_bindings",
        "api_audit_logs",
    ):
        assert connection.scalar(sa.text(f"SELECT count(*) FROM public.{table}")) == 0
    assert (
        connection.execute(
            sa.text(
                "UPDATE public.linked_broker_accounts SET display_name = 'forged' WHERE account_id = :id"
            ),
            {"id": peer_id},
        ).rowcount
        == 0
    )
    _denied(
        connection,
        "INSERT INTO public.broker_credentials (account_id, secret_ref) VALUES (:id, 'forged')",
        id=peer_id,
    )
    _denied(
        connection,
        "INSERT INTO public.api_audit_logs (user_id, action, status) VALUES (:id, 'forged', 'ok')",
        id=_PEER,
    )


@pytest.mark.parametrize("column", ["user_id", "status", "is_deployment_owner"])
def test_backend_cannot_change_owner_authority_columns(
    database: tuple[sa.Connection, int, int],
    column: str,
) -> None:
    connection, _, _ = database
    _as_backend(connection)
    _denied(
        connection, f"UPDATE public.users SET {column} = {column} WHERE user_id = :id", id=_OWNER
    )
    _denied(
        connection,
        "INSERT INTO public.users (user_id, email) VALUES ('forged', 'forged@test.invalid')",
    )


@pytest.mark.parametrize("column", ["account_id", "user_id", "broker_id", "environment"])
def test_backend_cannot_reassign_account_identity(
    database: tuple[sa.Connection, int, int],
    column: str,
) -> None:
    connection, owned_id, _ = database
    _as_backend(connection)
    _denied(
        connection,
        f"UPDATE public.linked_broker_accounts SET {column} = {column} WHERE account_id = :id",
        id=owned_id,
    )


def test_backend_profile_and_unused_account_columns_remain_editable(
    database: tuple[sa.Connection, int, int],
) -> None:
    connection, owned_id, _ = database
    _as_backend(connection)
    assert (
        connection.execute(
            sa.text("UPDATE public.users SET full_name = 'Reviewed owner' WHERE user_id = :id"),
            {"id": _OWNER},
        ).rowcount
        == 1
    )
    assert (
        connection.scalar(
            sa.text("SELECT public.vm_owner_account_has_activity(:id)"), {"id": owned_id}
        )
        is False
    )
    connection.execute(
        sa.text(
            "UPDATE public.linked_broker_accounts SET paper_initial_cash = 9000, config_key = 'primary' WHERE account_id = :id"
        ),
        {"id": owned_id},
    )
    assert (
        connection.scalar(sa.text("SELECT paper_initial_cash FROM public.linked_broker_accounts"))
        == 9000
    )


def test_backend_direct_sql_cannot_rewrite_recorded_finance(
    database: tuple[sa.Connection, int, int],
) -> None:
    connection, owned_id, _ = database
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        session.add(
            ExecutionLog(
                log_id="owner-control-recorded",
                user_id=_OWNER,
                account_id=owned_id,
                strategy_id="owner-control-test",
                signal_type="flat",
                execution_mode="paper",
                status="success",
            )
        )
        session.commit()
    _as_backend(connection)
    _denied(
        connection,
        "UPDATE public.linked_broker_accounts SET base_ccy = 'USD' WHERE account_id = :id",
        code="23514",
        id=owned_id,
    )
    connection.execute(
        sa.text(
            "UPDATE public.linked_broker_accounts SET display_name = 'Reviewed', base_ccy = base_ccy WHERE account_id = :id"
        ),
        {"id": owned_id},
    )
    assert connection.scalar(sa.text("SELECT base_ccy FROM public.linked_broker_accounts")) == "EUR"
    _denied(connection, "SELECT * FROM public.execution_logs")


def test_finance_guard_conflicts_with_runtime_execution_lock(
    database: tuple[sa.Connection, int, int],
) -> None:
    connection, owned_id, _ = database
    key = AccountExecutionSerializer._partition_key(_OWNER, owned_id)
    _as_backend(connection)
    with connection.engine.connect() as runtime:
        runtime.execute(sa.text("SELECT pg_advisory_lock(hashtextextended(:key, 0))"), {"key": key})
        try:
            _denied(
                connection,
                "SELECT public.vm_owner_account_has_activity(:id)",
                code="55P03",
                id=owned_id,
            )
            _denied(
                connection,
                "UPDATE public.linked_broker_accounts SET base_ccy = 'USD' WHERE account_id = :id",
                code="55P03",
                id=owned_id,
            )
            connection.execute(
                sa.text(
                    "UPDATE public.linked_broker_accounts SET display_name = 'Still editable' WHERE account_id = :id"
                ),
                {"id": owned_id},
            )
        finally:
            runtime.execute(
                sa.text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), {"key": key}
            )


@pytest.mark.parametrize(
    "role", ["vm_scoring", "vm_execution", "vm_feedback", "vm_market_data", "vm_indicator"]
)
def test_activity_disclosure_is_backend_only(
    database: tuple[sa.Connection, int, int],
    role: str,
) -> None:
    connection, owned_id, _ = database
    connection.execute(sa.text(f"SET LOCAL ROLE {role}"))
    _denied(connection, "SELECT public.vm_owner_account_has_activity(:id)", id=owned_id)


def test_activity_guard_rejects_foreign_account_and_public_execution(
    database: tuple[sa.Connection, int, int],
) -> None:
    connection, _, peer_id = database
    public_grants = connection.scalar(
        sa.text(
            "SELECT count(*) FROM pg_proc p, LATERAL aclexplode(p.proacl) a "
            "WHERE p.proname IN ('vm_owner_account_has_activity', 'vm_guard_account_financial_terms') "
            "AND a.grantee = 0 AND a.privilege_type = 'EXECUTE'"
        )
    )
    assert public_grants == 0
    _as_backend(connection)
    _denied(connection, "SELECT public.vm_owner_account_has_activity(:id)", id=peer_id)


def test_guard_is_owned_by_migration_identity_with_locked_search_path(
    database: tuple[sa.Connection, int, int],
) -> None:
    connection, _, _ = database
    rows = connection.execute(
        sa.text(
            "SELECT p.prosecdef, p.proconfig, pg_get_userbyid(p.proowner) = current_user "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname IN "
            "('vm_owner_account_has_activity', 'vm_guard_account_financial_terms')"
        )
    ).all()
    assert len(rows) == 2
    assert all(row == (True, ["search_path=pg_catalog, pg_temp"], True) for row in rows)


def test_migration_downgrade_and_reupgrade_preserve_account_values(
    database: tuple[sa.Connection, int, int],
) -> None:
    connection, _, _ = database
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts/db/alembic/versions/0102_owner_control_plane.py"
    )
    spec = importlib.util.spec_from_file_location("owner_control_plane_acceptance", path)
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    columns = "account_id, user_id, broker_id, environment, base_ccy, external_ref, config_key, paper_initial_equity, paper_initial_cash"
    before = connection.execute(
        sa.text(f"SELECT {columns} FROM public.linked_broker_accounts ORDER BY account_id")
    ).all()
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
    assert (
        connection.scalar(
            sa.text(
                "SELECT has_column_privilege('vm_backend', 'public.users', 'full_name', 'UPDATE')"
            )
        )
        is False
    )
    assert (
        connection.scalar(
            sa.text("SELECT to_regprocedure('public.vm_owner_account_has_activity(bigint)')")
        )
        is None
    )
    with Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
    after = connection.execute(
        sa.text(f"SELECT {columns} FROM public.linked_broker_accounts ORDER BY account_id")
    ).all()
    assert after == before
    assert (
        connection.scalar(
            sa.text(
                "SELECT has_column_privilege('vm_backend', 'public.users', 'full_name', 'UPDATE')"
            )
        )
        is True
    )
