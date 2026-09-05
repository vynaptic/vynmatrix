"""Contract tests for tenant-scoped backend control-plane grants."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0094_backend_control_plane.py"
)


class _ScalarResult:
    def __init__(self, value: str) -> None:
        self._value = value

    def scalar_one_or_none(self) -> str:
        return self._value


class _RecordingBind:
    def __init__(self, dialect: str) -> None:
        self.dialect = SimpleNamespace(name=dialect)

    def execute(self, _statement: Any, params: dict[str, str]) -> _ScalarResult:
        table = params["table_name"].rsplit(".", 1)[-1]
        column = params["column_name"]
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
    spec = importlib.util.spec_from_file_location("backend_control_plane", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_follows_factor_risk_exposure() -> None:
    migration = _load_migration()

    assert migration.revision == "0094_backend_control_plane"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0093_factor_risk_exposure"


def test_upgrade_grants_only_reviewed_control_plane_commands() -> None:
    migration = cast(Any, _load_migration())
    recorder = _RecordingOp()
    migration.op = recorder

    migration.upgrade()

    assert (
        "GRANT SELECT ON TABLE strategies, strategy_versions TO vm_backend" in recorder.statements
    )
    assert (
        "GRANT SELECT, INSERT, UPDATE ON TABLE user_strategy_configs TO vm_backend"
        in recorder.statements
    )
    assert "GRANT SELECT, INSERT ON TABLE risk_mandates TO vm_backend" in recorder.statements
    assert "GRANT SELECT, INSERT ON TABLE api_audit_logs TO vm_backend" in recorder.statements
    assert not any(
        statement.startswith("GRANT") and "DELETE" in statement for statement in recorder.statements
    )
    assert not any(
        statement.startswith("GRANT") and "risk_mandates" in statement and "UPDATE" in statement
        for statement in recorder.statements
    )

    policies = [
        statement for statement in recorder.statements if statement.startswith("CREATE POLICY")
    ]
    assert len(policies) == 7
    assert all(" TO vm_backend " in policy for policy in policies)
    assert all(
        "user_id = nullif(current_setting('app.current_tenant', true), '')" in policy
        for policy in policies
    )
    assert all("user_id IS NULL" not in policy for policy in policies)
    assert not any("risk_mandates_backend_update" in policy for policy in policies)
    assert not any("risk_mandates_backend_delete" in policy for policy in policies)
    assert (
        "CREATE POLICY user_strategy_configs_backend_update ON user_strategy_configs "
        "FOR UPDATE TO vm_backend USING "
        "(user_id = nullif(current_setting('app.current_tenant', true), '')) "
        "WITH CHECK (user_id = nullif(current_setting('app.current_tenant', true), ''))"
    ) in policies
    assert (
        "GRANT USAGE, SELECT ON SEQUENCE public.risk_mandates_mandate_id_seq TO vm_backend"
    ) in recorder.statements
    assert (
        "GRANT USAGE, SELECT ON SEQUENCE public.api_audit_logs_audit_id_seq TO vm_backend"
    ) in recorder.statements


def test_downgrade_removes_only_added_backend_authority() -> None:
    migration = cast(Any, _load_migration())
    recorder = _RecordingOp()
    migration.op = recorder

    migration.downgrade()

    assert (
        "REVOKE SELECT, INSERT, UPDATE ON TABLE user_strategy_configs FROM vm_backend"
        in recorder.statements
    )
    assert "REVOKE SELECT, INSERT ON TABLE risk_mandates FROM vm_backend" in recorder.statements
    assert "REVOKE SELECT, INSERT ON TABLE api_audit_logs FROM vm_backend" in recorder.statements
    assert all(not statement.startswith("CREATE POLICY") for statement in recorder.statements)


def test_migration_is_a_noop_off_postgresql() -> None:
    migration = cast(Any, _load_migration())
    recorder = _RecordingOp(dialect="sqlite")
    migration.op = recorder

    migration.upgrade()
    migration.downgrade()

    assert recorder.statements == []
