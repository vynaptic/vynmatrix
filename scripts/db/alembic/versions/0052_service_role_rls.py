"""Replace the shared application role with least-privilege service roles.

The migration owner remains the only role that owns schema objects and may
bypass row-level security. Runtime credentials are provisioned outside Alembic
and inherit exactly one of the NOLOGIN group roles created here.

The shared ``vm_app`` login is retired in place as a NOLOGIN role. PostgreSQL
roles are cluster-scoped, so a database migration must not try to drop a role
that can still have grants in another database. Deployments provision one LOGIN
role per service outside Alembic and grant each login exactly one of the NOLOGIN
groups created here.

Revision ID: 0052_service_role_rls
Revises: 0051_account_execution_scope
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import sqlalchemy as sa
from alembic import op

revision = "0052_service_role_rls"
down_revision = "0051_account_execution_scope"
branch_labels = None
depends_on = None

_BACKEND = "vm_backend"
_SCORING = "vm_scoring"
_EXECUTION = "vm_execution"
_FEEDBACK = "vm_feedback"
_MARKET_DATA = "vm_market_data"
_INDICATOR = "vm_indicator"

_SERVICE_ROLES = (
    _BACKEND,
    _SCORING,
    _EXECUTION,
    _FEEDBACK,
    _MARKET_DATA,
    _INDICATOR,
)

# Table privileges are deliberately explicit. There are no default grants:
# every new table or new service dependency must be reviewed in a migration.
_ROLE_PRIVILEGES: dict[str, dict[str, frozenset[str]]] = {
    _BACKEND: {
        "SELECT": frozenset(
            {
                "broker_credentials",
                "brokers",
                "linked_broker_accounts",
                "managed_secrets",
                "user_strategy_bindings",
            }
        ),
        "INSERT": frozenset(
            {
                "broker_credentials",
                "linked_broker_accounts",
                "managed_secrets",
                "user_strategy_bindings",
            }
        ),
        "UPDATE": frozenset({"managed_secrets", "user_strategy_bindings"}),
        "DELETE": frozenset(),
    },
    _SCORING: {
        "SELECT": frozenset(
            {
                "asset_scores",
                "broker_credentials",
                "brokers",
                "canonical_signals",
                "decision_contexts",
                "execution_decision_logs",
                "instrument_aliases",
                "instrument_broker_symbols",
                "instrument_sectors",
                "instruments",
                "linked_broker_accounts",
                "market_scores",
                "mode_performance",
                "outbox_events",
                "prices",
                "sector_scores",
                "sectors",
                "sizing_profiles",
                "strategies",
                "strategy_versions",
                "user_strategy_bindings",
                "user_strategy_configs",
                "user_trading_policies",
                "users",
            }
        ),
        "INSERT": frozenset(
            {
                "asset_scores",
                "canonical_signals",
                "decision_contexts",
                "execution_decision_logs",
                "market_scores",
                "outbox_events",
                "sector_scores",
            }
        ),
        "UPDATE": frozenset(
            {
                "asset_scores",
                "canonical_signals",
                "execution_decision_logs",
                "outbox_events",
            }
        ),
        "DELETE": frozenset(),
    },
    _EXECUTION: {
        "SELECT": frozenset(
            {
                "broker_credentials",
                "brokers",
                "canonical_signals",
                "api_audit_logs",
                "daily_nav",
                "execution_decision_logs",
                "execution_logs",
                "execution_metrics",
                "executions",
                "instrument_aliases",
                "instrument_broker_symbols",
                "instruments",
                "linked_broker_accounts",
                "managed_secrets",
                "options_positions",
                "order_intents",
                "orders",
                "outbox_events",
                "pending_orders",
                "positions",
                "prices",
                "risk_breaches",
                "risk_mandates",
                "strategies",
                "users",
            }
        ),
        "INSERT": frozenset(
            {
                "api_audit_logs",
                "daily_nav",
                "execution_decision_logs",
                "execution_logs",
                "execution_metrics",
                "executions",
                "options_positions",
                "order_intents",
                "orders",
                "outbox_events",
                "pending_orders",
                "positions",
                "risk_breaches",
                "risk_mandates",
            }
        ),
        "UPDATE": frozenset(
            {
                "daily_nav",
                "execution_decision_logs",
                "execution_logs",
                "options_positions",
                "order_intents",
                "orders",
                "pending_orders",
                "positions",
                "risk_breaches",
            }
        ),
        "DELETE": frozenset(
            {
                "execution_decision_logs",
                "positions",
                "risk_breaches",
            }
        ),
    },
    _FEEDBACK: {
        "SELECT": frozenset(
            {
                "canonical_signals",
                "execution_logs",
                "execution_metrics",
                "instruments",
                "mode_performance",
                "outbox_events",
                "prices",
                "service_heartbeats",
                "signal_performance",
                "strategies",
                "strategy_consecutive_wrong_tracker",
                "strategy_parameter_feedback",
                "strategy_versions",
            }
        ),
        "INSERT": frozenset(
            {
                "mode_performance",
                "outbox_events",
                "service_heartbeats",
                "signal_performance",
                "strategy_consecutive_wrong_tracker",
                "strategy_parameter_feedback",
            }
        ),
        "UPDATE": frozenset(
            {
                "mode_performance",
                "service_heartbeats",
                "strategy_consecutive_wrong_tracker",
                "strategy_parameter_feedback",
            }
        ),
        "DELETE": frozenset(),
    },
    _MARKET_DATA: {
        "SELECT": frozenset(
            {
                "brokers",
                "instrument_aliases",
                "instrument_broker_symbols",
                "instruments",
                "prices",
                "watermarks",
            }
        ),
        "INSERT": frozenset({"prices"}),
        "UPDATE": frozenset({"prices", "watermarks"}),
        "DELETE": frozenset(),
    },
    _INDICATOR: {
        "SELECT": frozenset(
            {
                "instrument_aliases",
                "instrument_broker_symbols",
                "instruments",
                "prices",
                "watermarks",
            }
        ),
        "INSERT": frozenset({"watermarks"}),
        "UPDATE": frozenset({"watermarks"}),
        "DELETE": frozenset(),
    },
}

# PostgreSQL serial/identity sequences needed by INSERT privileges. String,
# UUID, and composite primary keys intentionally do not appear here.
_SERIAL_COLUMNS: dict[str, str] = {
    "api_audit_logs": "audit_id",
    "asset_scores": "score_id",
    "broker_credentials": "cred_id",
    "canonical_signals": "signal_id",
    "daily_nav": "nav_id",
    "decision_contexts": "context_id",
    "execution_decision_logs": "decision_id",
    "executions": "exec_id",
    "linked_broker_accounts": "account_id",
    "market_scores": "score_id",
    "mode_performance": "perf_id",
    "order_intents": "intent_id",
    "orders": "order_id",
    "positions": "pos_id",
    "prices": "price_id",
    "risk_breaches": "breach_id",
    "risk_mandates": "mandate_id",
    "sector_scores": "score_id",
    "signal_performance": "perf_id",
    "strategy_consecutive_wrong_tracker": "tracker_id",
    "strategy_parameter_feedback": "feedback_id",
    "user_strategy_bindings": "binding_id",
}

_DIRECT_TENANT_TABLES = frozenset(
    {
        "api_audit_logs",
        "daily_nav",
        "execution_decision_logs",
        "execution_logs",
        "execution_metrics",
        "linked_broker_accounts",
        "options_positions",
        "order_intents",
        "outbound_webhooks",
        "pending_orders",
        "sizing_profiles",
        "user_budget_buckets",
        "user_consents",
        "user_feature_flags",
        "user_notifications",
        "user_opportunity_subscriptions",
        "user_option_presets",
        "user_plan_subscriptions",
        "user_roles",
        "user_strategy_bindings",
        "user_strategy_configs",
        "user_suitability_responses",
        "user_trading_policies",
        "users",
    }
)

_ACCOUNT_TENANT_TABLES = frozenset(
    {
        "broker_credentials",
        "managed_secrets",
        "mode_performance",
        "opp_sub_execution_bindings",
        "orders",
        "positions",
    }
)

_DIRECT_OWNED = "user_id = current_setting('app.current_tenant', true)"
_ACCOUNT_OWNED = (
    "account_id IN (SELECT account_id FROM linked_broker_accounts "
    "WHERE user_id = current_setting('app.current_tenant', true))"
)
_BINDING_OWNED = (
    "user_id = current_setting('app.current_tenant', true) AND "
    "broker_account_id IN "
    "(SELECT account_id FROM linked_broker_accounts "
    "WHERE user_id = current_setting('app.current_tenant', true))"
)
_EXECUTION_OWNED = (
    "order_id IN (SELECT order_id FROM orders WHERE account_id IN "
    "(SELECT account_id FROM linked_broker_accounts "
    "WHERE user_id = current_setting('app.current_tenant', true)))"
)

_RLS_PREDICATES = {
    **dict.fromkeys(_DIRECT_TENANT_TABLES, _DIRECT_OWNED),
    **dict.fromkeys(_ACCOUNT_TENANT_TABLES, _ACCOUNT_OWNED),
    "user_strategy_bindings": _BINDING_OWNED,
    "executions": _EXECUTION_OWNED,
}

_NEW_RLS_TABLES = frozenset({"managed_secrets", "mode_performance"})
_QUALIFIED_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")


def _qualified_tables(tables: Iterable[str]) -> str:
    return ", ".join(f"public.{table}" for table in sorted(tables))


def _create_service_roles() -> None:
    for role in _SERVICE_ROLES:
        op.execute(
            "DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            f"CREATE ROLE {role}; "
            "END IF; "
            f"ALTER ROLE {role} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS; "
            f"ALTER ROLE {role} RESET ALL; "
            "END $$;"
        )


def _reset_service_role_privileges() -> None:
    """Remove privileges from a pre-existing role before applying the matrix.

    Role creation is intentionally idempotent because an operator may
    pre-provision the NOLOGIN groups. Without this reset, such a role could
    retain table, sequence, schema, or default privileges outside the explicit
    matrix below, defeating the migration's least-privilege guarantee.
    """
    for role in _SERVICE_ROLES:
        op.execute(
            "DO $$ DECLARE inherited_role RECORD; BEGIN "
            "FOR inherited_role IN "
            "SELECT parent.rolname "
            "FROM pg_auth_members membership "
            "JOIN pg_roles member_role ON member_role.oid = membership.member "
            "JOIN pg_roles parent ON parent.oid = membership.roleid "
            f"WHERE member_role.rolname = '{role}' "
            "LOOP "
            f"EXECUTE format('REVOKE %I FROM %I', inherited_role.rolname, '{role}'); "
            "END LOOP; "
            "END $$;"
        )
        op.execute(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {role}")
        op.execute(f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {role}")
        op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {role}")
        op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM {role}")
        op.execute(f"REVOKE USAGE ON SCHEMA public FROM {role}")


def _retire_vm_app_privileges() -> None:
    # Remove the 0039 all-table surface and its future-table inheritance.
    op.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM vm_app")
    op.execute("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM vm_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM vm_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM vm_app")
    op.execute("REVOKE USAGE ON SCHEMA public FROM vm_app")

    op.execute(
        "REVOKE vm_backend, vm_scoring, vm_execution, vm_feedback, "
        "vm_market_data, vm_indicator FROM vm_app"
    )
    op.execute(
        "ALTER ROLE vm_app NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
        "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
    )
    op.execute("ALTER ROLE vm_app RESET ALL")


def _grant_table_privileges() -> None:
    for role, privileges in _ROLE_PRIVILEGES.items():
        op.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            tables = privileges[privilege]
            if tables:
                op.execute(f"GRANT {privilege} ON TABLE {_qualified_tables(tables)} TO {role}")


def _grant_sequence_privileges() -> None:
    bind = op.get_bind()
    for role, privileges in _ROLE_PRIVILEGES.items():
        for table in sorted(privileges["INSERT"]):
            column = _SERIAL_COLUMNS.get(table)
            if column is None:
                continue
            sequence_name = bind.execute(
                sa.text("SELECT pg_get_serial_sequence(:relation_name, :column_name)"),
                {
                    "relation_name": f"public.{table}",
                    "column_name": column,
                },
            ).scalar_one_or_none()
            if sequence_name is None:
                continue
            sequence = str(sequence_name)
            if _QUALIFIED_IDENTIFIER.fullmatch(sequence) is None:
                msg = f"Unexpected serial sequence identifier: {sequence!r}"
                raise RuntimeError(msg)
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {sequence} TO {role}")


def _policy_name(table: str, role: str, command: str) -> str:
    return f"{table}_{role.removeprefix('vm_')}_{command.lower()}"


def _drop_service_policies() -> None:
    for role, privileges in _ROLE_PRIVILEGES.items():
        for command, tables in privileges.items():
            for table in sorted(tables & _RLS_PREDICATES.keys()):
                op.execute(f"DROP POLICY IF EXISTS {_policy_name(table, role, command)} ON {table}")


def _create_command_policy(
    *,
    table: str,
    role: str,
    command: str,
    predicate: str,
) -> None:
    name = _policy_name(table, role, command)
    if command == "SELECT":
        clause = f"FOR SELECT TO {role} USING ({predicate})"
    elif command == "INSERT":
        clause = f"FOR INSERT TO {role} WITH CHECK ({predicate})"
    elif command == "UPDATE":
        clause = f"FOR UPDATE TO {role} USING ({predicate}) WITH CHECK ({predicate})"
    elif command == "DELETE":
        clause = f"FOR DELETE TO {role} USING ({predicate})"
    else:  # pragma: no cover - the privilege matrix is a closed constant
        msg = f"Unsupported policy command: {command}"
        raise ValueError(msg)
    op.execute(f"CREATE POLICY {name} ON {table} {clause}")


def _replace_rls_policies() -> None:
    # Remove the permissive PUBLIC policies from 0039/0040, including their
    # session-controlled cross-tenant escape.
    for table in sorted(_RLS_PREDICATES):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

    _drop_service_policies()
    for role, privileges in _ROLE_PRIVILEGES.items():
        for command in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            for table in sorted(privileges[command] & _RLS_PREDICATES.keys()):
                predicate = _RLS_PREDICATES[table] if role == _BACKEND else "true"
                _create_command_policy(
                    table=table,
                    role=role,
                    command=command,
                    predicate=predicate,
                )


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    _create_service_roles()
    _reset_service_role_privileges()
    _retire_vm_app_privileges()
    _grant_table_privileges()
    _grant_sequence_privileges()
    _replace_rls_policies()


def _restore_vm_app_policies() -> None:
    # A downgrade restores the previous shared-role grants but deliberately does
    # not resurrect the deleted mutable cross-tenant escape. The four command
    # policies preserve tenant isolation without a FOR ALL compatibility policy.
    historical_tables = (_DIRECT_TENANT_TABLES | _ACCOUNT_TENANT_TABLES | {"executions"}) - (
        _NEW_RLS_TABLES
    )
    for table in sorted(historical_tables):
        predicate = _RLS_PREDICATES[table]
        for command in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            name = f"{table}_vm_app_{command.lower()}"
            op.execute(f"DROP POLICY IF EXISTS {name} ON {table}")
            if command == "SELECT":
                clause = f"FOR SELECT TO vm_app USING ({predicate})"
            elif command == "INSERT":
                clause = f"FOR INSERT TO vm_app WITH CHECK ({predicate})"
            elif command == "UPDATE":
                clause = f"FOR UPDATE TO vm_app USING ({predicate}) WITH CHECK ({predicate})"
            else:
                clause = f"FOR DELETE TO vm_app USING ({predicate})"
            op.execute(f"CREATE POLICY {name} ON {table} {clause}")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(
        "ALTER ROLE vm_app LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOREPLICATION NOBYPASSRLS"
    )
    _drop_service_policies()
    for table in sorted(_RLS_PREDICATES):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    for table in sorted(_NEW_RLS_TABLES):
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    _restore_vm_app_policies()

    for role in _SERVICE_ROLES:
        op.execute(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {role}")
        op.execute(f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {role}")
        op.execute(f"REVOKE USAGE ON SCHEMA public FROM {role}")
        op.execute(
            f"ALTER ROLE {role} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        )
        op.execute(f"ALTER ROLE {role} RESET ALL")

    op.execute("GRANT USAGE ON SCHEMA public TO vm_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO vm_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO vm_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO vm_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO vm_app"
    )
