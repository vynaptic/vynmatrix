"""Contract tests for the service-role/RLS migration.

The PostgreSQL integration job applies Alembic and provisions explicit runtime
logins. These unit tests record migration operations and validate the CI role
configuration without connecting to PostgreSQL.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml
from sqlalchemy import BigInteger, Integer
from sqlalchemy.engine import make_url

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
    # 0103 removes empty commercial roles/subscriptions without reviving grants.
    later_direct_tenant_tables = {
        "account_execution_generations",
        "account_rebalance_plan_legs",
        "account_rebalance_plan_resolutions",
        "account_rebalance_plans",
    }
    assert direct_tenant_tables - later_direct_tenant_tables == (
        migration._DIRECT_TENANT_TABLES
        - {
            "user_opportunity_subscriptions",
            "options_positions",
            "user_roles",
            "user_plan_subscriptions",
        }
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


def test_local_stack_keeps_six_role_urls_separate_from_bootstrap() -> None:
    compose = yaml.safe_load((_ROOT / "docker" / "docker-compose.stack.yml").read_text())
    services = compose["services"]
    application = services["application"]["environment"]
    workers = services["workers"]["environment"]
    for role in ("BACKEND", "SCORING", "EXECUTION", "FEEDBACK", "MARKET_DATA", "INDICATOR"):
        key = f"{role}_DATABASE_URL"
        assert application[key] == "${" + key + ":-}"
    assert (
        not {"BACKEND_DATABASE_URL", "SCORING_DATABASE_URL", "EXECUTION_DATABASE_URL"}
        & workers.keys()
    )
    assert "DATABASE_URL" not in application
    assert "DATABASE_URL" not in workers
    bootstrap = services["bootstrap"]["environment"]
    assert "DATABASE_URL" not in bootstrap
    for key in ("ADMIN_DATABASE_URL", "MIGRATION_DATABASE_URL"):
        assert bootstrap[key] == "${" + key + ":-}"
        assert key not in application
        assert key not in workers


def test_local_stack_limits_encrypted_key_ring_to_application_group() -> None:
    compose = yaml.safe_load((_ROOT / "docker" / "docker-compose.stack.yml").read_text())
    services = compose["services"]
    assert (
        services["application"]["environment"]["SECRETS_MASTER_KEYS"] == "${SECRETS_MASTER_KEYS:-}"
    )
    assert "SECRETS_MASTER_KEYS" not in services["workers"]["environment"]


def test_local_role_plan_requires_six_isolated_groups_without_repair() -> None:
    from dev_cli.core.runtime_roles import RuntimeRoleError, plan_runtime_roles

    expected_pairs = {
        "vm_backend_login": "vm_backend",
        "vm_scoring_login": "vm_scoring",
        "vm_execution_login": "vm_execution",
        "vm_feedback_login": "vm_feedback",
        "vm_market_data_login": "vm_market_data",
        "vm_indicator_login": "vm_indicator",
    }

    catalog = {
        group: {
            "rolcanlogin": False,
            "rolsuper": False,
            "rolcreatedb": False,
            "rolcreaterole": False,
            "rolreplication": False,
            "rolbypassrls": False,
            "owns_objects": False,
            "memberships": [],
        }
        for group in expected_pairs.values()
    }
    plan = plan_runtime_roles(catalog, rotate=False)
    assert plan.create == tuple(expected_pairs)
    assert plan.authenticate == ()
    assert plan.rotate == ()
    catalog["vm_backend"]["memberships"] = [
        {"role": "vm_execution", "inherit": True, "set": True, "admin": False}
    ]
    with pytest.raises(RuntimeRoleError, match="Service group vm_backend"):
        plan_runtime_roles(catalog, rotate=False)


def test_local_role_wrapper_preserves_cli_arguments_without_database_access(tmp_path: Path) -> None:
    interpreter = tmp_path / "capture-interpreter"
    output = tmp_path / "arguments.json"
    interpreter.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['CAPTURE_ARGUMENTS']).write_text(json.dumps(sys.argv[1:]))\n"
    )
    interpreter.chmod(0o700)
    result = subprocess.run(
        ["/bin/sh", str(_ROOT / "docker/provision-runtime-roles.sh"), "--rotate"],
        env={"PYTHON": str(interpreter), "CAPTURE_ARGUMENTS": str(output)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(output.read_text()) == ["-m", "dev_cli.main", "db", "roles", "--rotate"]


def test_ci_role_provisioning_supplies_current_cli_environment() -> None:
    from dev_cli.core.runtime_roles import PASSWORD_ENV

    workflow = yaml.safe_load((_ROOT / ".github/workflows/ci.yml").read_text())
    job = workflow["jobs"]["postgres-integration"]
    steps = job["steps"]
    role_step = next(
        step for step in steps if step.get("name") == "Provision least-privilege runtime logins"
    )
    environment = {**job["env"], **role_step.get("env", {})}
    admin = make_url(environment["ADMIN_DATABASE_URL"])
    migration = make_url(environment["MIGRATION_DATABASE_URL"])
    postgres = job["services"]["postgres"]["env"]
    assert admin.database == "postgres"
    assert migration.database == postgres["POSTGRES_DB"]
    assert admin.host == migration.host == "localhost"
    assert admin.port == migration.port == 5432
    assert admin.username == migration.username == postgres["POSTGRES_USER"]
    assert admin.password == migration.password == postgres["POSTGRES_PASSWORD"]
    runtime_passwords = [environment[key] for key in PASSWORD_ENV.values()]
    assert all(runtime_passwords)
    assert len(set(runtime_passwords)) == len(PASSWORD_ENV)
    assert admin.password not in runtime_passwords
    assert not {"PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE"} & role_step["env"].keys()
    assert role_step["run"] == "sh docker/provision-runtime-roles.sh"
    assert next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Build schema from Alembic head"
    ) < steps.index(role_step)
    assert "tools/dev_cli" in environment["PYTHONPATH"].split(":")

    result = subprocess.run(
        ["/bin/sh", "docker/provision-runtime-roles.sh", "--help"],
        cwd=_ROOT,
        env={"PYTHON": sys.executable, "PYTHONPATH": environment["PYTHONPATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--rotate" in result.stdout
