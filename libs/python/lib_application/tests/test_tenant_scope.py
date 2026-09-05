"""Unit tests for the backend RLS tenant-scope GUC helper.

``tenant_scope`` issues Postgres ``set_config`` calls that the RLS policies read;
the actual row-level enforcement is Postgres-only (verified against a live
service-role-connected DB, not sqlite). Here we assert the helper issues the
transaction-local tenant statement via a spy session.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

from lib_application.db.session import tenant_scope


class _SpySession:
    """Records executed statements without touching a DB.

    Presents a ``postgresql`` bind so ``tenant_scope`` issues the GUC statements
    (it no-ops on non-Postgres dialects, which have no RLS).
    """

    def __init__(self, dialect: str = "postgresql") -> None:
        self.executed: list[tuple[str, Any]] = []
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))

    def execute(self, stmt: Any, params: Any = None) -> None:
        self.executed.append((str(stmt), params))


def test_tenant_scope_has_no_mutable_bypass_argument() -> None:
    assert "cross" + "_tenant" not in inspect.signature(tenant_scope).parameters


def test_tenant_scope_sets_current_tenant_guc() -> None:
    spy = _SpySession()
    with tenant_scope(spy, user_id="u-1"):  # type: ignore[arg-type]
        pass

    assert len(spy.executed) == 1
    sql, params = spy.executed[0]
    assert "set_config('app.current_tenant'" in sql
    assert ", true)" in sql  # transaction-local
    assert params == {"uid": "u-1"}


def test_tenant_scope_noop_when_nothing_requested() -> None:
    # No tenant issues nothing; backend RLS then fail-closes to no rows.
    spy = _SpySession()
    with tenant_scope(spy):  # type: ignore[arg-type]
        pass
    assert spy.executed == []


def test_tenant_scope_noop_on_non_postgres() -> None:
    # No RLS off Postgres (e.g. sqlite fixtures) → issue nothing.
    spy = _SpySession(dialect="sqlite")
    with tenant_scope(spy, user_id="u-1"):  # type: ignore[arg-type]
        pass
    assert spy.executed == []
