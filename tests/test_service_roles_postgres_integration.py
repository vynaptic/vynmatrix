"""Live PostgreSQL acceptance for runtime service roles and tenant RLS."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine, make_url

_ROLE_PASSWORD_ENV = {
    "vm_backend_login": "VM_BACKEND_DB_PASSWORD",
    "vm_scoring_login": "VM_SCORING_DB_PASSWORD",
    "vm_execution_login": "VM_EXECUTION_DB_PASSWORD",
    "vm_feedback_login": "VM_FEEDBACK_DB_PASSWORD",
    "vm_market_data_login": "VM_MARKET_DATA_DB_PASSWORD",
    "vm_indicator_login": "VM_INDICATOR_DB_PASSWORD",
}
_PRIVILEGE_EXPECTATIONS = {
    "vm_backend_login": (
        ("broker_credentials", "UPDATE", True),
        ("strategies", "INSERT", False),
    ),
    "vm_scoring_login": (
        ("canonical_signals", "INSERT", True),
        ("strategies", "INSERT", False),
    ),
    "vm_execution_login": (
        ("orders", "INSERT", True),
        ("pending_orders", "UPDATE", True),
        ("execution_metrics", "SELECT", True),
        ("execution_metrics", "INSERT", True),
        ("strategy_versions", "SELECT", True),
        ("strategy_versions", "UPDATE", False),
        ("user_strategy_bindings", "SELECT", True),
        ("linked_broker_accounts", "INSERT", False),
    ),
    "vm_feedback_login": (
        ("service_heartbeats", "UPDATE", True),
        ("execution_decision_logs", "SELECT", True),
        ("execution_decision_logs", "INSERT", False),
        ("order_intents", "SELECT", True),
        ("orders", "SELECT", True),
        ("executions", "SELECT", True),
        ("prices", "INSERT", False),
        ("strategies", "UPDATE", False),
        ("strategy_versions", "UPDATE", False),
    ),
    "vm_market_data_login": (
        ("prices", "INSERT", True),
        ("strategies", "SELECT", False),
    ),
    "vm_indicator_login": (
        ("watermarks", "UPDATE", True),
        ("strategy_runtime_states", "UPDATE", True),
        ("strategy_decisions", "INSERT", True),
        ("strategy_decisions", "UPDATE", False),
        ("outbox_events", "UPDATE", True),
        ("prices", "INSERT", False),
        ("prices", "UPDATE", False),
    ),
}


def _admin_url() -> sa.URL:
    raw = os.getenv("DATABASE_URL")
    if not raw:
        pytest.skip("DATABASE_URL is required for PostgreSQL service-role acceptance")
    url = make_url(raw)
    if not url.drivername.startswith("postgresql"):
        pytest.skip("PostgreSQL service-role acceptance requires a PostgreSQL DATABASE_URL")
    return url


def _runtime_engine(admin_url: sa.URL, role: str) -> Engine:
    password = os.getenv(_ROLE_PASSWORD_ENV[role])
    if not password:
        pytest.skip(f"{_ROLE_PASSWORD_ENV[role]} is required for service-role acceptance")
    return sa.create_engine(
        admin_url.set(username=role, password=password),
        future=True,
    )


@contextmanager
def _tenant_rows(admin: Engine) -> Iterator[tuple[str, str, int, int]]:
    suffix = uuid4().hex[:12]
    own_user = f"rls-own-{suffix}"
    other_user = f"rls-other-{suffix}"
    own_account = 0
    other_account = 0
    with admin.begin() as connection:
        broker_id = int(
            connection.execute(
                sa.text("SELECT broker_id FROM brokers WHERE code = 'saxo'")
            ).scalar_one()
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO users (user_id, email, base_ccy, status)
                VALUES
                    (:own_user, :own_email, 'EUR', 'active'),
                    (:other_user, :other_email, 'INR', 'active')
                """
            ),
            {
                "own_user": own_user,
                "own_email": f"{own_user}@example.invalid",
                "other_user": other_user,
                "other_email": f"{other_user}@example.invalid",
            },
        )
        own_account = int(
            connection.execute(
                sa.text(
                    """
                    INSERT INTO linked_broker_accounts (
                        user_id, broker_id, environment, display_name, base_ccy, status
                    )
                    VALUES (:user_id, :broker_id, 'live', 'RLS own', 'EUR', 'connected')
                    RETURNING account_id
                    """
                ),
                {"user_id": own_user, "broker_id": broker_id},
            ).scalar_one()
        )
        other_account = int(
            connection.execute(
                sa.text(
                    """
                    INSERT INTO linked_broker_accounts (
                        user_id, broker_id, environment, display_name, base_ccy, status
                    )
                    VALUES (:user_id, :broker_id, 'live', 'RLS other', 'INR', 'connected')
                    RETURNING account_id
                    """
                ),
                {"user_id": other_user, "broker_id": broker_id},
            ).scalar_one()
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO broker_credentials (account_id, secret_ref, status)
                VALUES
                    (:own_account, :own_ref, 'active'),
                    (:other_account, :other_ref, 'active')
                """
            ),
            {
                "own_account": own_account,
                "own_ref": f"service-role-test/{own_account}",
                "other_account": other_account,
                "other_ref": f"service-role-test/{other_account}",
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO managed_secrets (secret_ref, account_id, ciphertext)
                VALUES
                    (:own_ref, :own_account, 'ciphertext-own'),
                    (:other_ref, :other_account, 'ciphertext-other')
                """
            ),
            {
                "own_account": own_account,
                "own_ref": f"service-role-test/{own_account}",
                "other_account": other_account,
                "other_ref": f"service-role-test/{other_account}",
            },
        )
    try:
        yield own_user, other_user, own_account, other_account
    finally:
        with admin.begin() as connection:
            connection.execute(
                sa.text(
                    "DELETE FROM managed_secrets WHERE account_id IN (:own_account, :other_account)"
                ),
                {
                    "own_account": own_account,
                    "other_account": other_account,
                },
            )
            connection.execute(
                sa.text(
                    "DELETE FROM broker_credentials WHERE account_id IN "
                    "(:own_account, :other_account)"
                ),
                {
                    "own_account": own_account,
                    "other_account": other_account,
                },
            )
            connection.execute(
                sa.text(
                    "DELETE FROM linked_broker_accounts WHERE account_id IN "
                    "(:own_account, :other_account)"
                ),
                {
                    "own_account": own_account,
                    "other_account": other_account,
                },
            )
            connection.execute(
                sa.text("DELETE FROM users WHERE user_id IN (:own_user, :other_user)"),
                {"own_user": own_user, "other_user": other_user},
            )


@pytest.mark.integration
def test_runtime_logins_enforce_privilege_matrix_and_backend_tenant_rls() -> None:
    admin = sa.create_engine(_admin_url(), future=True)
    runtime_engines = {role: _runtime_engine(_admin_url(), role) for role in _ROLE_PASSWORD_ENV}
    try:
        for role, engine in runtime_engines.items():
            with engine.connect() as connection:
                identity = connection.execute(
                    sa.text(
                        """
                        SELECT current_user, rolcanlogin, rolsuper, rolbypassrls
                        FROM pg_roles
                        WHERE rolname = current_user
                        """
                    )
                ).one()
                assert tuple(identity) == (role, True, False, False)
                for table, privilege, expected in _PRIVILEGE_EXPECTATIONS[role]:
                    actual = bool(
                        connection.execute(
                            sa.text(
                                """
                                SELECT has_table_privilege(
                                    current_user,
                                    :table,
                                    :privilege
                                )
                                """
                            ),
                            {
                                "table": f"public.{table}",
                                "privilege": privilege,
                            },
                        ).scalar_one()
                    )
                    assert actual is expected, f"{role} {privilege} on {table}"
                if role == "vm_feedback_login":
                    can_execute_gate = bool(
                        connection.execute(
                            sa.text(
                                """
                                SELECT has_function_privilege(
                                    current_user,
                                    'public.vm_feedback_exact_strategy_version_active'
                                    '(character varying, bigint)',
                                    'EXECUTE'
                                )
                                """
                            )
                        ).scalar_one()
                    )
                    assert can_execute_gate is True
                if role == "vm_indicator_login":
                    can_lock_source = bool(
                        connection.execute(
                            sa.text(
                                """
                                SELECT has_function_privilege(
                                    current_user,
                                    'public.vm_indicator_lock_source_price'
                                    '(bigint, integer, timestamp without time zone, '
                                    'character varying, character varying, bigint)',
                                    'EXECUTE'
                                )
                                """
                            )
                        ).scalar_one()
                    )
                    assert can_lock_source is True

        with admin.connect() as connection:
            feedback_lineage_policies = {
                str(name)
                for name in connection.execute(
                    sa.text(
                        """
                        SELECT policyname
                          FROM pg_policies
                         WHERE roles @> ARRAY['vm_feedback']::name[]
                           AND cmd = 'SELECT'
                           AND tablename IN (
                               'execution_decision_logs',
                               'order_intents',
                               'orders',
                               'executions'
                           )
                        """
                    )
                ).scalars()
            }
            assert feedback_lineage_policies == {
                "execution_decision_logs_feedback_select",
                "order_intents_feedback_select",
                "orders_feedback_select",
                "executions_feedback_select",
            }
            execution_authority_policies = {
                str(name)
                for name in connection.execute(
                    sa.text(
                        """
                        SELECT policyname
                          FROM pg_policies
                         WHERE roles @> ARRAY['vm_execution']::name[]
                           AND cmd = 'SELECT'
                           AND tablename = 'user_strategy_bindings'
                        """
                    )
                ).scalars()
            }
            assert execution_authority_policies == {
                "user_strategy_bindings_execution_select",
            }

        with (
            _tenant_rows(admin) as (
                own_user,
                _other_user,
                own_account,
                other_account,
            ),
            runtime_engines["vm_backend_login"].begin() as connection,
        ):
            connection.execute(
                sa.text("SELECT set_config('app.current_tenant', :tenant, true)"),
                {"tenant": own_user},
            )
            visible_accounts = {
                int(value)
                for value in connection.execute(
                    sa.text("SELECT account_id FROM broker_credentials ORDER BY account_id")
                ).scalars()
            }
            assert visible_accounts == {own_account}
            visible_secret_accounts = {
                int(value)
                for value in connection.execute(
                    sa.text("SELECT account_id FROM managed_secrets ORDER BY account_id")
                ).scalars()
            }
            assert visible_secret_accounts == {own_account}

            own_update = connection.execute(
                sa.text(
                    """
                        UPDATE broker_credentials
                        SET last_rotated_at = CURRENT_TIMESTAMP
                        WHERE account_id = :account_id
                        """
                ),
                {"account_id": own_account},
            )
            cross_tenant_update = connection.execute(
                sa.text(
                    """
                        UPDATE broker_credentials
                        SET last_rotated_at = CURRENT_TIMESTAMP
                        WHERE account_id = :account_id
                        """
                ),
                {"account_id": other_account},
            )
            assert own_update.rowcount == 1
            assert cross_tenant_update.rowcount == 0
            own_secret_update = connection.execute(
                sa.text(
                    """
                    UPDATE managed_secrets
                    SET ciphertext = 'ciphertext-own-rotated'
                    WHERE account_id = :account_id
                    """
                ),
                {"account_id": own_account},
            )
            cross_tenant_secret_update = connection.execute(
                sa.text(
                    """
                    UPDATE managed_secrets
                    SET ciphertext = 'must-not-write'
                    WHERE account_id = :account_id
                    """
                ),
                {"account_id": other_account},
            )
            assert own_secret_update.rowcount == 1
            assert cross_tenant_secret_update.rowcount == 0
    finally:
        for engine in runtime_engines.values():
            engine.dispose()
        admin.dispose()
