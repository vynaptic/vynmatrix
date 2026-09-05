"""Contract tests for account-scoped execution schema migration 0051."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_PATH = (
    _ROOT / "scripts" / "db" / "alembic" / "versions" / "0051_account_execution_scope.py"
)


class _RecordingOp:
    def __init__(self, dialect: str = "postgresql") -> None:
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def get_bind(self) -> Any:
        return self.bind

    def _record(self, operation: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((operation, args, kwargs))

    def execute(self, *args: Any, **kwargs: Any) -> None:
        self._record("execute", *args, **kwargs)

    def create_unique_constraint(self, *args: Any, **kwargs: Any) -> None:
        self._record("create_unique_constraint", *args, **kwargs)

    def create_foreign_key(self, *args: Any, **kwargs: Any) -> None:
        self._record("create_foreign_key", *args, **kwargs)

    def create_index(self, *args: Any, **kwargs: Any) -> None:
        self._record("create_index", *args, **kwargs)

    def alter_column(self, *args: Any, **kwargs: Any) -> None:
        self._record("alter_column", *args, **kwargs)

    def drop_index(self, *args: Any, **kwargs: Any) -> None:
        self._record("drop_index", *args, **kwargs)

    def drop_constraint(self, *args: Any, **kwargs: Any) -> None:
        self._record("drop_constraint", *args, **kwargs)

    def drop_column(self, *args: Any, **kwargs: Any) -> None:
        self._record("drop_column", *args, **kwargs)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "account_execution_scope_migration", _MIGRATION_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _find_call(
    recorder: _RecordingOp,
    operation: str,
    name: str,
) -> tuple[int, tuple[Any, ...], dict[str, Any]]:
    for index, (actual_operation, args, kwargs) in enumerate(recorder.calls):
        if actual_operation == operation and args and args[0] == name:
            return index, args, kwargs
    msg = f"Missing {operation} call for {name}"
    raise AssertionError(msg)


def test_revision_follows_strategy_retirement_head() -> None:
    migration = _load_migration()

    assert migration.revision == "0051_account_execution_scope"
    assert migration.down_revision == "0050_retire_vol_reversal_v1"


def test_upgrade_enforces_same_account_execution_chain() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration._create_constraints()

    intent_unique_index, intent_unique, _ = _find_call(
        recorder,
        "create_unique_constraint",
        "uq_order_intent_account",
    )
    order_fk_index, order_fk, order_fk_kwargs = _find_call(
        recorder,
        "create_foreign_key",
        "fk_order_intent_account_match",
    )
    order_unique_index, order_unique, _ = _find_call(
        recorder,
        "create_unique_constraint",
        "uq_order_account",
    )
    pending_fk_index, pending_fk, pending_fk_kwargs = _find_call(
        recorder,
        "create_foreign_key",
        "fk_pending_order_account_match",
    )

    assert intent_unique == (
        "uq_order_intent_account",
        "order_intents",
        ["intent_id", "account_id"],
    )
    assert order_fk == (
        "fk_order_intent_account_match",
        "orders",
        "order_intents",
        ["intent_id", "account_id"],
        ["intent_id", "account_id"],
    )
    assert order_fk_kwargs == {"ondelete": "CASCADE"}
    assert order_unique == (
        "uq_order_account",
        "orders",
        ["order_id", "account_id"],
    )
    assert pending_fk == (
        "fk_pending_order_account_match",
        "pending_orders",
        "orders",
        ["canonical_order_id", "broker_account_id"],
        ["order_id", "account_id"],
    )
    assert pending_fk_kwargs == {}
    assert intent_unique_index < order_fk_index
    assert order_unique_index < pending_fk_index


def test_upgrade_enforces_one_wildcard_binding_per_account() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration._create_indexes()

    _, args, kwargs = _find_call(
        recorder,
        "create_index",
        "uq_user_strategy_binding_wildcard_account",
    )
    assert args == (
        "uq_user_strategy_binding_wildcard_account",
        "user_strategy_bindings",
        ["user_id", "broker_account_id"],
    )
    assert kwargs["unique"] is True
    assert str(kwargs["postgresql_where"]) == "strategy_id IS NULL"
    assert str(kwargs["sqlite_where"]) == "strategy_id IS NULL"


def test_backfill_fails_closed_on_ambiguous_account_identity() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration._backfill_postgresql()

    sql = "\n".join(str(args[0]) for operation, args, _ in recorder.calls if operation == "execute")
    assert "HAVING COUNT(*) > 1" in sql
    assert "duplicate wildcard account groups" in sql
    assert "canonical_order.account_id <> intent.account_id" in sql
    assert "intent/account mismatches" in sql


def test_downgrade_drops_composite_fks_before_target_uniques() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration.downgrade()

    wildcard_index, _, _ = _find_call(
        recorder,
        "drop_index",
        "uq_user_strategy_binding_wildcard_account",
    )
    pending_fk_index, _, _ = _find_call(
        recorder,
        "drop_constraint",
        "fk_pending_order_account_match",
    )
    order_fk_index, _, _ = _find_call(
        recorder,
        "drop_constraint",
        "fk_order_intent_account_match",
    )
    order_unique_index, _, _ = _find_call(
        recorder,
        "drop_constraint",
        "uq_order_account",
    )
    intent_unique_index, _, _ = _find_call(
        recorder,
        "drop_constraint",
        "uq_order_intent_account",
    )

    assert wildcard_index < pending_fk_index
    assert pending_fk_index < order_unique_index
    assert order_fk_index < intent_unique_index
