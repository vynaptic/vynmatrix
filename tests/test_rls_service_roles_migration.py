"""Contract tests for the service-role/RLS migration.

The repository's PostgreSQL integration job creates application tables through
SQLAlchemy and does not run Alembic as a role administrator. These tests execute
the migration against a recording Alembic operation facade so role, grant, and
policy regressions are still enforced in the normal suite.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import yaml
from sqlalchemy import BigInteger, Integer

from lib_application.db.models import Base

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_PATH = _ROOT / "scripts" / "db" / "alembic" / "versions" / "0052_service_role_rls.py"


class _ScalarResult:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> str | None:
        return self._value


class _RecordingBind:
    def __init__(self, dialect: str) -> None:
        self.dialect = SimpleNamespace(name=dialect)

    def execute(self, _statement: Any, params: dict[str, str]) -> _ScalarResult:
        relation = params["relation_name"]
        column = params["column_name"]
        table = relation.rsplit(".", 1)[-1]
        return _ScalarResult(f"public.{table}_{column}_seq")


class _RecordingOp:
    def __init__(self, dialect: str = "postgresql") -> None:
        self.bind = _RecordingBind(dialect)
        self.statements: list[str] = []

    def get_bind(self) -> _RecordingBind:
        return self.bind

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("service_role_rls_migration", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_sql(module: ModuleType) -> list[str]:
    recorder = _RecordingOp()
    module.op = recorder
    module.upgrade()
    return recorder.statements


def test_revision_follows_account_execution_scope() -> None:
    migration = _load_migration()

    assert migration.revision == "0052_service_role_rls"
    assert migration.down_revision == "0051_account_execution_scope"


def test_upgrade_creates_non_login_service_groups_and_retires_shared_login() -> None:
    migration = _load_migration()
    statements = _upgrade_sql(migration)
    sql = "\n".join(statements)

    for role in (
        "vm_backend",
        "vm_scoring",
        "vm_execution",
        "vm_feedback",
        "vm_market_data",
        "vm_indicator",
    ):
        role_ddl = next(statement for statement in statements if f"CREATE ROLE {role}" in statement)
        assert f"ALTER ROLE {role} NOLOGIN" in role_ddl
        assert "NOINHERIT" in role_ddl
        assert "NOSUPERUSER" in role_ddl
        assert "NOCREATEROLE" in role_ddl
        assert "NOBYPASSRLS" in role_ddl
        assert f"ALTER ROLE {role} RESET ALL" in role_ddl

    assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM vm_app" in statements
    assert (
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM vm_app" in statements
    )
    assert (
        "ALTER ROLE vm_app NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
        "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
    ) in statements
    assert "ALTER ROLE vm_app RESET ALL" in statements
    assert "DROP ROLE vm_app" not in statements
    assert not any(statement.startswith("GRANT vm_") for statement in statements)
    assert not any(
        "ON ALL TABLES" in statement and " TO vm_" in statement for statement in statements
    )
    assert "app.cross" + "_tenant" not in sql


def test_upgrade_resets_preexisting_service_role_privileges_before_granting() -> None:
    migration = _load_migration()
    statements = _upgrade_sql(migration)

    for role in migration._SERVICE_ROLES:
        membership_reset = next(
            statement
            for statement in statements
            if "FROM pg_auth_members membership" in statement
            and f"member_role.rolname = '{role}'" in statement
        )
        reset_statements = {
            f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {role}",
            f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {role}",
            (f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {role}"),
            (f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM {role}"),
            f"REVOKE USAGE ON SCHEMA public FROM {role}",
        }
        grant_index = next(
            index
            for index, statement in enumerate(statements)
            if statement.startswith("GRANT ") and statement.endswith(f" TO {role}")
        )
        for statement in reset_statements:
            assert statement in statements
            assert statements.index(statement) < grant_index
        assert statements.index(membership_reset) < grant_index


def test_upgrade_uses_command_specific_rls_and_account_owns_managed_secrets() -> None:
    migration = _load_migration()
    statements = _upgrade_sql(migration)
    policies = [statement for statement in statements if statement.startswith("CREATE POLICY")]
    sql = "\n".join(policies)

    assert policies
    assert " FOR ALL " not in sql
    assert "ALTER TABLE managed_secrets ENABLE ROW LEVEL SECURITY" in statements
    assert (
        "CREATE POLICY managed_secrets_backend_select ON managed_secrets "
        "FOR SELECT TO vm_backend USING "
        "(account_id IN (SELECT account_id FROM linked_broker_accounts "
        "WHERE user_id = current_setting('app.current_tenant', true)))"
    ) in statements
    assert (
        "CREATE POLICY managed_secrets_backend_insert ON managed_secrets "
        "FOR INSERT TO vm_backend WITH CHECK "
        "(account_id IN (SELECT account_id FROM linked_broker_accounts "
        "WHERE user_id = current_setting('app.current_tenant', true)))"
    ) in statements
    assert (
        "CREATE POLICY managed_secrets_execution_select ON managed_secrets "
        "FOR SELECT TO vm_execution USING (true)"
    ) in statements
    binding_insert = next(
        statement
        for statement in policies
        if statement.startswith("CREATE POLICY user_strategy_bindings_backend_insert ")
    )
    assert "user_id = current_setting('app.current_tenant', true)" in binding_insert
    assert "broker_account_id IN (SELECT account_id FROM linked_broker_accounts" in binding_insert
    assert not any("managed_secrets_scoring_" in statement for statement in policies)
    assert not any(
        "managed_secrets_execution_" in statement and "_select " not in statement
        for statement in policies
    )


def test_every_rls_table_grant_has_exactly_one_matching_command_policy() -> None:
    migration = _load_migration()
    statements = _upgrade_sql(migration)
    actual_policy_names = {
        statement.split()[2] for statement in statements if statement.startswith("CREATE POLICY")
    }
    expected_policy_names = {
        migration._policy_name(table, role, command)
        for role, privileges in migration._ROLE_PRIVILEGES.items()
        for command, tables in privileges.items()
        for table in tables & migration._RLS_PREDICATES.keys()
    }

    assert actual_policy_names == expected_policy_names


def test_privilege_matrix_separates_service_data_planes() -> None:
    migration = _load_migration()
    privileges = migration._ROLE_PRIVILEGES

    assert privileges["vm_backend"]["SELECT"] == {
        "broker_credentials",
        "brokers",
        "linked_broker_accounts",
        "managed_secrets",
        "user_strategy_bindings",
    }
    assert "orders" not in privileges["vm_backend"]["SELECT"]
    assert "managed_secrets" not in privileges["vm_scoring"]["SELECT"]
    assert "user_strategy_bindings" in privileges["vm_scoring"]["SELECT"]
    assert "mode_performance" in privileges["vm_scoring"]["SELECT"]
    assert "instrument_broker_symbols" in privileges["vm_scoring"]["SELECT"]
    assert "decision_contexts" in privileges["vm_scoring"]["SELECT"]
    assert "user_strategy_bindings" not in privileges["vm_scoring"]["INSERT"]
    assert "user_strategy_bindings" not in privileges["vm_scoring"]["UPDATE"]
    assert "mode_performance" not in privileges["vm_scoring"]["INSERT"]
    assert "mode_performance" not in privileges["vm_scoring"]["UPDATE"]
    assert privileges["vm_scoring"]["UPDATE"] >= {
        "asset_scores",
        "canonical_signals",
    }
    assert (
        not {
            "instruments",
            "instrument_sectors",
            "sectors",
            "strategies",
        }
        & privileges["vm_scoring"]["INSERT"]
    )
    assert "instruments" not in privileges["vm_scoring"]["UPDATE"]
    assert privileges["vm_execution"]["SELECT"] >= {
        "api_audit_logs",
        "instrument_broker_symbols",
        "linked_broker_accounts",
        "managed_secrets",
        "orders",
        "positions",
    }
    assert "linked_broker_accounts" not in privileges["vm_execution"]["INSERT"]
    assert "strategies" not in privileges["vm_execution"]["INSERT"]
    assert "managed_secrets" not in privileges["vm_feedback"]["SELECT"]
    assert "prices" in privileges["vm_feedback"]["SELECT"]
    assert "prices" not in privileges["vm_feedback"]["INSERT"]
    assert "prices" not in privileges["vm_feedback"]["UPDATE"]
    assert set().union(*privileges["vm_market_data"].values()) == {
        "brokers",
        "instrument_aliases",
        "instrument_broker_symbols",
        "instruments",
        "prices",
        "watermarks",
    }
    assert set().union(*privileges["vm_indicator"].values()) == {
        "instrument_aliases",
        "instrument_broker_symbols",
        "instruments",
        "prices",
        "watermarks",
    }
    for role_privileges in privileges.values():
        # SQLAlchemy ORM inserts use RETURNING for generated primary keys, and
        # PostgreSQL requires SELECT privilege for that result.
        assert role_privileges["INSERT"] <= role_privileges["SELECT"]
    all_insert_tables = set().union(
        *(role_privileges["INSERT"] for role_privileges in privileges.values())
    )
    assert migration._SERIAL_COLUMNS.keys() <= all_insert_tables


def test_privilege_and_rls_inventories_match_current_orm_schema() -> None:
    migration = _load_migration()
    metadata_tables = Base.metadata.tables
    granted_tables = {
        table
        for role_privileges in migration._ROLE_PRIVILEGES.values()
        for tables in role_privileges.values()
        for table in tables
    }
    direct_tenant_tables = {name for name, table in metadata_tables.items() if "user_id" in table.c}
    account_tenant_tables = {
        name
        for name, table in metadata_tables.items()
        if "account_id" in table.c and "user_id" not in table.c
    }

    # Revision 0056 removes the fixed two-leg options-position tracker. 0052
    # retains its historical grant so base -> head and downgrade traversal work.
    assert granted_tables - {"options_positions"} <= metadata_tables.keys()
    # 0052 must retain the historical inventories so base -> head and a
    # downgrade to 0054 can still traverse the then-present tables. Revision
    # 0055 removes the dormant opportunity tables; revision 0058 replaces the
    # polymorphic risk owners with direct user_id columns and installs their
    # command-specific policies. Revision 0088 adds the account rebalance
    # aggregate with policies owned by that revision; revision 0089 adds the
    # account execution generation table and its execution-only policies.
    # Those later changes must not be backported to 0052 because their tables
    # or tenant columns do not yet exist there.
    later_direct_tenant_tables = {
        "account_execution_generations",
        "account_rebalance_plan_legs",
        "account_rebalance_plan_resolutions",
        "account_rebalance_plans",
    }
    assert direct_tenant_tables - later_direct_tenant_tables == (
        migration._DIRECT_TENANT_TABLES - {"user_opportunity_subscriptions", "options_positions"}
        | {"risk_mandates", "risk_breaches"}
    )
    assert account_tenant_tables == (
        migration._ACCOUNT_TENANT_TABLES - {"opp_sub_execution_bindings", "options_positions"}
    )
    assert "executions" in migration._RLS_PREDICATES


def test_sequence_inventory_covers_every_inserted_integer_identity() -> None:
    migration = _load_migration()
    insert_tables = {
        table
        for role_privileges in migration._ROLE_PRIVILEGES.values()
        for table in role_privileges["INSERT"]
    }
    integer_identities = {
        table.name: column.name
        for table in Base.metadata.tables.values()
        for column in table.primary_key.columns
        if (
            table.name in insert_tables
            and len(table.primary_key.columns) == 1
            and isinstance(column.type, (BigInteger, Integer))
            and column.autoincrement is not False
        )
    }

    assert integer_identities == migration._SERIAL_COLUMNS


def test_downgrade_does_not_resurrect_mutable_bypass_or_for_all_policies() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration.downgrade()

    policies = "\n".join(
        statement for statement in recorder.statements if statement.startswith("CREATE POLICY")
    )
    assert "app.cross" + "_tenant" not in policies
    assert " FOR ALL " not in policies


def test_migration_is_a_noop_off_postgresql() -> None:
    migration = _load_migration()
    recorder = _RecordingOp(dialect="sqlite")
    migration.op = recorder

    migration.upgrade()
    migration.downgrade()

    assert recorder.statements == []


def test_local_stack_uses_one_runtime_login_per_service() -> None:
    compose = yaml.safe_load((_ROOT / "docker" / "docker-compose.stack.yml").read_text())
    services = compose["services"]
    expected_logins = {
        "backend": "vm_backend_login",
        "scoring-engine": "vm_scoring_login",
        "scoring-outbox-relay": "vm_scoring_login",
        "execution-engine": "vm_execution_login",
        "feedback-loop-engine": "vm_feedback_login",
        "market-data-ingestor": "vm_market_data_login",
        "market-data-backfill": "vm_market_data_login",
        "indicator-runner": "vm_indicator_login",
    }

    for service_name, login_name in expected_logins.items():
        service = services[service_name]
        assert login_name in service["environment"]["DATABASE_URL"]
        assert service["depends_on"]["db-runtime-roles"]["condition"] == (
            "service_completed_successfully"
        )
        assert "${DB_USER:-trader}" not in service["environment"]["DATABASE_URL"]

    assert "${DB_USER:-trader}" in services["db-migrate"]["environment"]["DATABASE_URL"]


def test_local_stack_keeps_backend_readable_without_weakening_execution_secrets() -> None:
    compose = yaml.safe_load((_ROOT / "docker" / "docker-compose.stack.yml").read_text())
    services = compose["services"]

    assert services["backend"]["environment"]["SECRETS_BACKEND"] == ("${SECRETS_BACKEND:-env}")
    assert services["execution-engine"]["environment"]["SECRETS_BACKEND"] == (
        "${SECRETS_BACKEND:-db}"
    )


def test_local_role_provisioner_resets_exact_group_membership() -> None:
    script = (_ROOT / "docker" / "provision-runtime-roles.sh").read_text()
    expected_pairs = {
        "vm_backend_login": "vm_backend",
        "vm_scoring_login": "vm_scoring",
        "vm_execution_login": "vm_execution",
        "vm_feedback_login": "vm_feedback",
        "vm_market_data_login": "vm_market_data",
        "vm_indicator_login": "vm_indicator",
    }

    assert "REVOKE vm_backend, vm_scoring, vm_execution" in script
    assert "rolbypassrls" in script
    for login_name, group_name in expected_pairs.items():
        assert f"provision_login {login_name} {group_name} " in script
