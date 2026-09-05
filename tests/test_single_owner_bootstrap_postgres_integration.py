"""Explicit opt-in full bootstrap on a dedicated disposable PostgreSQL cluster.

Requires SINGLE_OWNER_BOOTSTRAP_TEST_ALLOW_CREATE=1 plus explicit ADMIN_DATABASE_URL
and target URL in SINGLE_OWNER_BOOTSTRAP_TEST_ADMIN_DATABASE_URL and
SINGLE_OWNER_BOOTSTRAP_TEST_DATABASE_URL. Target name must start with
single_owner_bootstrap_test. The target and all vm_* roles must be absent initially.
This test creates and removes only that target and the exact bootstrap role names.
Never run this module against the normal development cluster.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from psycopg2 import sql
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from dev_cli.core.bootstrap import BootstrapSettings, bootstrap_database
from dev_cli.core.runtime_roles import PASSWORD_ENV, RUNTIME_ROLES
from lib_application.db.models import (
    Broker,
    BrokerEnvironment,
    LinkedBrokerAccount,
    Strategy,
    StrategyVersion,
    User,
)
from lib_application.db.session import create_engine_for_env, dispose_engine, get_session_factory
from lib_application.services.account_onboarding import BrokerAccountIn, onboard_account
from lib_application.services.database_authority import require_backend_database_role
from lib_application.services.owner_onboarding import apply_owner_patch
from lib_infrastructure.brokers.secrets import create_secrets_provider

pytestmark = pytest.mark.integration
_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_settings() -> Iterator[BootstrapSettings]:
    if os.environ.get("SINGLE_OWNER_BOOTSTRAP_TEST_ALLOW_CREATE") != "1":
        pytest.skip("Full bootstrap requires separately authorized disposable-cluster creation")
    admin = os.environ.get("SINGLE_OWNER_BOOTSTRAP_TEST_ADMIN_DATABASE_URL")
    migration = os.environ.get("SINGLE_OWNER_BOOTSTRAP_TEST_DATABASE_URL")
    if not admin or not migration:
        pytest.fail("Both explicit disposable-cluster test URLs are required")
    settings = BootstrapSettings.parse(
        {
            "ADMIN_DATABASE_URL": admin,
            "MIGRATION_DATABASE_URL": migration,
            **{key: uuid4().hex for key in PASSWORD_ENV.values()},
        }
    )
    if not str(settings.migration.database).startswith("single_owner_bootstrap_test"):
        pytest.fail("Use an explicit single_owner_bootstrap_test-prefixed disposable target")
    names = ["vm_app", *(name for pair in RUNTIME_ROLES for name in pair)]
    engine = create_engine_for_env(db_url=admin)
    try:
        with engine.connect() as connection:
            assert not connection.scalar(
                text("SELECT 1 FROM pg_catalog.pg_database WHERE datname=:name"),
                {"name": settings.migration.database},
            ), "Test database must be absent"
            assert not connection.scalar(
                text("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=ANY(:names)"),
                {"names": names},
            ), "Use a dedicated cluster without existing vm_* roles"
        try:
            yield settings
        finally:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                driver = connection.connection.driver_connection
                assert driver is not None
                with driver.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {}").format(
                            sql.Identifier(settings.migration.database)
                        )
                    )
                    for name in [
                        *(login for login, _ in RUNTIME_ROLES),
                        *(group for _, group in RUNTIME_ROLES),
                        "vm_app",
                    ]:
                        cursor.execute(
                            sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name))
                        )
    finally:
        dispose_engine(engine)


def test_full_bootstrap_repeat_preserves_owner_accounts_and_inactive_releases(
    isolated_settings: BootstrapSettings,
) -> None:
    settings = isolated_settings
    owner_input = {
        "profile": {"email": "bootstrap-owner@example.invalid", "base_ccy": "EUR", "tz": "UTC"}
    }
    first = bootstrap_database(_ROOT, settings, owner_input)
    engine = create_engine_for_env(db_url=settings.migration.render_as_string(hide_password=False))
    try:
        with get_session_factory(engine=engine)() as session:
            assert session.scalars(select(User.user_id)).all() == [first["owner_id"]]
            assert not session.scalars(select(Strategy).where(Strategy.is_active.is_(True))).all()
            versions = session.scalars(select(StrategyVersion)).all()
            assert versions
            assert all(version.status == "registered" for version in versions)
            chosen_id = versions[0].strat_ver_id
            versions[0].status = "pulled"
            broker = session.scalar(
                select(Broker.code)
                .join(BrokerEnvironment)
                .where(Broker.code == "paper", BrokerEnvironment.environment == "paper")
            )
            assert broker is not None
            session.commit()
        backend_url = settings.migration.set(
            username="vm_backend_login", password=settings.passwords["vm_backend_login"]
        )
        backend = create_engine_for_env(db_url=backend_url.render_as_string(hide_password=False))
        try:
            factory = get_session_factory(engine=backend)
            provider = create_secrets_provider(
                backend="db", session_factory=factory, master_keys=[Fernet.generate_key().decode()]
            )
            with factory() as session, session.begin():
                require_backend_database_role(session)
                apply_owner_patch(
                    session,
                    expected={"full_name": None},
                    changes={"full_name": "Preserved operator edit"},
                )
                account = onboard_account(
                    session,
                    BrokerAccountIn(
                        config_key="acceptance.paper",
                        broker_code=broker,
                        environment="paper",
                        base_ccy="EUR",
                        paper_initial_equity=1000,
                        paper_initial_cash=1000,
                    ),
                    provider,
                )
        finally:
            dispose_engine(backend)
        repeated = bootstrap_database(_ROOT, settings, owner_input)
        assert repeated["owner_id"] == first["owner_id"]
        assert repeated["revision"] == first["revision"]
        with Session(engine) as session:
            assert session.get(User, first["owner_id"]).full_name == "Preserved operator edit"
            assert session.get(StrategyVersion, chosen_id).status == "pulled"
            persisted = session.get(LinkedBrokerAccount, account.account_id)
            assert persisted.config_key == "acceptance.paper"
            assert persisted.user_id == first["owner_id"]
            assert persisted.paper_initial_cash == 1000
    finally:
        dispose_engine(engine)
