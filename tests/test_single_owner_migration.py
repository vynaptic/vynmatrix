"""Designation preserves historical identity and fails closed before tenant selection."""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from lib_application.db.models import Base, LinkedBrokerAccount, User


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables[name] for name in ("users", "brokers", "linked_broker_accounts")
        ],
    )
    yield engine
    engine.dispose()


def _user(user_id: str, **kwargs: object) -> User:
    return User(user_id=user_id, email=f"{user_id}@example.invalid", base_ccy="EUR", **kwargs)


def _migration() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts/db/alembic/versions/0099_single_owner_authority.py"
    )
    spec = importlib.util.spec_from_file_location("single_owner_authority", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_model_designation_default_and_uniqueness(engine: sa.Engine) -> None:
    assert "is_deployment_owner" in User.__table__.c
    with Session(engine) as session:
        session.add_all([_user("historical"), _user("designated", is_deployment_owner=True)])
        session.commit()
        assert session.get(User, "historical").is_deployment_owner is False
        session.add(_user("second", is_deployment_owner=True))
        with pytest.raises(sa.exc.IntegrityError):
            session.commit()


@pytest.mark.parametrize("status", [None, "active", "suspended", "closed"])
def test_resolver_requires_active_explicit_owner(engine: sa.Engine, status: str | None) -> None:
    from lib_application.services.deployment_owner import (
        DeploymentOwnerError,
        require_deployment_owner_id,
    )

    with Session(engine) as session:
        session.add(_user("historical"))
        if status is not None:
            session.add(_user("owner-with-non-uuid-id", status=status, is_deployment_owner=True))
        session.commit()
        if status == "active":
            assert require_deployment_owner_id(session) == "owner-with-non-uuid-id"
        else:
            with pytest.raises(DeploymentOwnerError, match="active deployment owner"):
                require_deployment_owner_id(session)


def test_resolver_rechecks_after_owner_revocation(engine: sa.Engine) -> None:
    from lib_application.services.deployment_owner import (
        DeploymentOwnerError,
        require_deployment_owner_id,
    )

    with Session(engine) as session:
        session.add(_user("owner", is_deployment_owner=True))
        session.commit()
        assert require_deployment_owner_id(session) == "owner"
        session.execute(sa.update(User).values(status="suspended"))
        session.commit()
        with pytest.raises(DeploymentOwnerError):
            require_deployment_owner_id(session)


def _account(user_id: str, key: str | None) -> LinkedBrokerAccount:
    return LinkedBrokerAccount(
        user_id=user_id,
        broker_id=1,
        config_key=key,
        environment="paper",
        display_name="Account",
        base_ccy="EUR",
        paper_initial_equity=100,
        paper_initial_cash=100,
    )


def test_account_key_unique_per_owner_and_legacy_null_allowed(engine: sa.Engine) -> None:
    assert "config_key" in LinkedBrokerAccount.__table__.c
    with Session(engine) as session:
        session.add_all([_user("owner"), _user("historical")])
        session.add_all(
            [
                _account("owner", None),
                _account("owner", None),
                _account("owner", "primary"),
                _account("historical", "primary"),
            ]
        )
        session.commit()
        session.add(_account("owner", "primary"))
        with pytest.raises(sa.exc.IntegrityError):
            session.commit()


@pytest.mark.parametrize("key", ["", "   ", "\t\n\r", " \v\f "])
def test_account_key_rejects_blank(engine: sa.Engine, key: str) -> None:
    assert "config_key" in LinkedBrokerAccount.__table__.c
    with Session(engine) as session:
        session.add(_user("owner"))
        session.add(_account("owner", key))
        with pytest.raises(sa.exc.IntegrityError):
            session.commit()


def test_migration_round_trip_preserves_historical_rows(engine: sa.Engine) -> None:
    migration = _migration()
    assert migration.down_revision == "0098_retire_liquidity_leaders"
    with Session(engine) as session:
        session.add(_user("historical"))
        session.add(_account("historical", None))
        session.commit()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()
        assert "is_deployment_owner" not in {
            c["name"] for c in sa.inspect(connection).get_columns("users")
        }
        migration.upgrade()
    with Session(engine) as session:
        assert session.get(User, "historical").is_deployment_owner is False
        account = session.scalars(sa.select(LinkedBrokerAccount)).one()
        assert account.user_id == "historical"
        assert account.config_key is None
    for model in [User, LinkedBrokerAccount]:
        assert {
            c["name"]: c["nullable"] for c in sa.inspect(engine).get_columns(model.__tablename__)
        } == {c.name: c.nullable for c in model.__table__.c}


@pytest.mark.parametrize("configured", ["owner", "account"])
def test_downgrade_refuses_configured_authority_before_ddl(
    engine: sa.Engine, configured: str
) -> None:
    with Session(engine) as session:
        session.add(_user("owner", is_deployment_owner=configured == "owner"))
        if configured == "account":
            session.add(_account("owner", "primary"))
        session.commit()
    migration = _migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        with pytest.raises(RuntimeError, match=r"backup.*disposition"):
            migration.downgrade()
        assert "is_deployment_owner" in {
            c["name"] for c in sa.inspect(connection).get_columns("users")
        }
        assert "config_key" in {
            c["name"] for c in sa.inspect(connection).get_columns("linked_broker_accounts")
        }


@pytest.fixture
def postgres_connection() -> Iterator[sa.Connection]:
    """Explicit opt-in: an isolated maintenance connection migrated through 0099."""
    raw = os.getenv("SINGLE_OWNER_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("SINGLE_OWNER_TEST_DATABASE_URL is required for isolated PostgreSQL acceptance")
    engine = sa.create_engine(raw)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.fail("Owner authority acceptance requires PostgreSQL")
    with engine.connect() as connection, connection.begin():
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM public.users WHERE is_deployment_owner")
            )
            == 0
        ), "Use an isolated unconfigured database for owner acceptance"
        yield connection
        connection.rollback()
    engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize(
    "role",
    ["vm_backend", "vm_scoring", "vm_execution", "vm_feedback", "vm_market_data", "vm_indicator"],
)
def test_postgres_discovery_before_scope_and_marker_write_denial(
    postgres_connection: sa.Connection,
    role: str,
) -> None:
    from lib_application.services.deployment_owner import (
        DeploymentOwnerError,
        require_deployment_owner_id,
    )

    connection = postgres_connection
    connection.execute(
        sa.text(
            "INSERT INTO public.users (user_id, email, base_ccy, is_deployment_owner) "
            "VALUES ('authority-acceptance', 'authority@example.invalid', 'EUR', true)"
        )
    )
    connection.execute(sa.text(f"SET LOCAL ROLE {role}"))
    connection.execute(sa.text("SELECT set_config('app.current_tenant', '', true)"))
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        assert require_deployment_owner_id(session) == "authority-acceptance"
    assert (
        connection.scalar(
            sa.text(
                "SELECT has_column_privilege(current_user, 'public.users', 'is_deployment_owner', 'UPDATE')"
            )
        )
        is False
    )
    assert (
        connection.scalar(
            sa.text(
                "SELECT has_column_privilege(current_user, 'public.users', 'is_deployment_owner', 'INSERT')"
            )
        )
        is False
    )
    with pytest.raises(sa.exc.DBAPIError), connection.begin_nested():
        connection.execute(sa.text("UPDATE public.users SET is_deployment_owner = false"))
    connection.execute(sa.text("RESET ROLE"))
    connection.execute(
        sa.text("UPDATE public.users SET status = 'suspended' WHERE is_deployment_owner")
    )
    connection.execute(sa.text(f"SET LOCAL ROLE {role}"))
    with (
        Session(bind=connection, join_transaction_mode="create_savepoint") as session,
        pytest.raises(DeploymentOwnerError),
    ):
        require_deployment_owner_id(session)


@pytest.mark.integration
def test_postgres_function_uses_locked_definer_and_no_public_grant(
    postgres_connection: sa.Connection,
) -> None:
    row = postgres_connection.execute(
        sa.text(
            "SELECT p.prosecdef, p.proconfig, p.proowner = c.relowner AS table_owned "
            "FROM pg_proc p JOIN pg_class c ON c.oid = 'public.users'::regclass "
            "WHERE p.oid = 'public.vm_deployment_owner_id()'::regprocedure"
        )
    ).one()
    assert row.prosecdef is True
    assert row.proconfig == ["search_path=pg_catalog, pg_temp"]
    assert row.table_owned is True
    assert (
        postgres_connection.scalar(
            sa.text(
                "SELECT count(*) FROM pg_proc p, LATERAL aclexplode(p.proacl) acl "
                "WHERE p.oid = 'public.vm_deployment_owner_id()'::regprocedure "
                "AND acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'"
            )
        )
        == 0
    )


@pytest.mark.integration
def test_postgres_concurrent_designation_cannot_create_two_owners(
    postgres_connection: sa.Connection,
) -> None:
    connection = postgres_connection
    connection.execute(
        sa.text(
            "INSERT INTO public.users (user_id, email, base_ccy, is_deployment_owner) "
            "VALUES ('owner-race-a', 'owner-race-a@example.invalid', 'EUR', true)"
        )
    )
    with connection.engine.connect() as contender, contender.begin():
        contender.execute(sa.text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(sa.exc.DBAPIError) as caught, contender.begin_nested():
            contender.execute(
                sa.text(
                    "INSERT INTO public.users (user_id, email, base_ccy, is_deployment_owner) "
                    "VALUES ('owner-race-b', 'owner-race-b@example.invalid', 'EUR', true)"
                )
            )
        assert (
            getattr(caught.value.orig, "sqlstate", None)
            or getattr(caught.value.orig, "pgcode", None)
        ) == "55P03"
        contender.rollback()


@pytest.mark.integration
@pytest.mark.parametrize("completion", ["commit", "rollback"])
def test_postgres_owner_scope_resets_on_reused_connection(
    postgres_connection: sa.Connection,
    completion: str,
) -> None:
    from uuid import uuid4

    from lib_application.db.session import tenant_scope
    from lib_application.services.deployment_owner import require_deployment_owner_id

    owner_id = f"scope-owner-{uuid4().hex}"
    # A separate, single-connection pool exercises actual checkout after commit
    # and rollback. The exact test-created identity is removed in finally.
    pool = sa.create_engine(postgres_connection.engine.url, pool_size=1, max_overflow=0)
    try:
        with pool.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO public.users (user_id, email, base_ccy, is_deployment_owner) "
                    "VALUES (:owner_id, :email, 'EUR', true)"
                ),
                {"owner_id": owner_id, "email": f"{owner_id}@example.invalid"},
            )
        with Session(pool) as session:
            assert require_deployment_owner_id(session) == owner_id
            backend_pid = session.scalar(sa.text("SELECT pg_backend_pid()"))
            with tenant_scope(session, user_id=owner_id):
                assert (
                    session.scalar(sa.text("SELECT current_setting('app.current_tenant', true)"))
                    == owner_id
                )
                getattr(session, completion)()
        with Session(pool) as session:
            assert session.scalar(sa.text("SELECT pg_backend_pid()")) == backend_pid
            assert session.scalar(
                sa.text("SELECT current_setting('app.current_tenant', true)")
            ) in (None, "")
            assert require_deployment_owner_id(session) == owner_id
    finally:
        with pool.begin() as connection:
            connection.execute(
                sa.text("DELETE FROM public.users WHERE user_id = :owner_id"),
                {"owner_id": owner_id},
            )
        pool.dispose()
