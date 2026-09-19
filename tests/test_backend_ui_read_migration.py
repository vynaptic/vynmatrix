"""Contract tests for the backend's column-scoped owner UI read surface (0107)."""

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
    / "0107_backend_ui_read.py"
)
_NEVER_READABLE = {
    "orders": {"client_order_id", "broker_order_ref"},
    "order_intents": {"payload", "idempotency_key"},
    "canonical_signals": {"features", "signal_meta", "external_signal_id"},
    "execution_metrics": {"metadata", "signal_id", "run_id", "unrealized_pnl"},
    "prices": {"open", "high", "low", "close", "volume"},
    "executions": {"trade_id"},
}


class _RecordingOp:
    def __init__(self, dialect: str = "postgresql") -> None:
        self._bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))
        self.statements: list[str] = []

    def get_bind(self) -> Any:
        return self._bind

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("backend_ui_read", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(direction: str, dialect: str = "postgresql") -> tuple[Any, list[str]]:
    migration = cast(Any, _load_migration())
    recorder = _RecordingOp(dialect)
    migration.op = recorder
    getattr(migration, direction)()
    return migration, recorder.statements


def test_revision_follows_the_dead_letter_retirement() -> None:
    migration = _load_migration()
    assert migration.revision == "0107_backend_ui_read"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0106_retire_topic_dead_letters"


def test_upgrade_grants_column_level_select_only() -> None:
    migration, statements = _run("upgrade")
    grants = [statement for statement in statements if statement.startswith("GRANT")]

    assert len(grants) == len(migration.READ_COLUMNS)
    for table, columns in migration.READ_COLUMNS.items():
        expected = f"GRANT SELECT ({', '.join(columns)}) ON TABLE public.{table} TO vm_backend"
        assert expected in grants
    for statement in statements:
        for forbidden in ("INSERT", "UPDATE", "DELETE", "SEQUENCE", "ALL PRIVILEGES", "BYPASSRLS"):
            assert forbidden not in statement, statement
    for table, columns in _NEVER_READABLE.items():
        assert not columns & set(migration.READ_COLUMNS[table]), table


def test_every_row_secured_table_gets_an_owner_scoped_select_policy() -> None:
    migration, statements = _run("upgrade")
    policies = [statement for statement in statements if statement.startswith("CREATE POLICY")]

    assert len(policies) == len(migration.ROW_POLICIES)
    for table in migration.ROW_POLICIES:
        (policy,) = [p for p in policies if f" ON public.{table} " in p]
        assert policy.startswith(f"CREATE POLICY {table}_backend_select ON public.{table} ")
        assert " FOR SELECT TO vm_backend USING (" in policy
        assert "public.vm_deployment_owner_id()" in policy
        assert "current_setting('app.current_tenant', true)" in policy
        assert "USING (true)" not in policy
        # The predicate's own column is granted so the policy never widens a refusal.
        assert migration.POLICY_KEYS[table] in migration.READ_COLUMNS[table]
        assert f"{migration.POLICY_KEYS[table]} " in policy
    # The executions policy reads orders, and both account policies read the account list.
    assert {"order_id", "account_id"} <= set(migration.READ_COLUMNS["orders"])


def test_downgrade_removes_exactly_what_upgrade_added() -> None:
    migration, statements = _run("downgrade")

    for table in migration.ROW_POLICIES:
        assert f"DROP POLICY IF EXISTS {table}_backend_select ON public.{table}" in statements
    for table, columns in migration.READ_COLUMNS.items():
        revoke = f"REVOKE SELECT ({', '.join(columns)}) ON TABLE public.{table} FROM vm_backend"
        assert revoke in statements
    assert len(statements) == len(migration.ROW_POLICIES) + len(migration.READ_COLUMNS)


def test_the_migration_is_inert_outside_postgresql() -> None:
    assert _run("upgrade", "sqlite")[1] == []
    assert _run("downgrade", "sqlite")[1] == []
