"""RLS for account-scoped tenant tables (H-8 follow-up to 0039).

0039 covered tables with a direct ``user_id``. This adds tenant-isolation policies
to the trade tables scoped INDIRECTLY, via ``account_id`` ->
``linked_broker_accounts.user_id`` (and ``executions`` via ``order_id`` -> orders ->
account). Same seam: rows visible to the row's owning tenant
(``app.current_tenant``) or the sanctioned fan-out (``app.cross_tenant = 'on'``);
writes self-scoped (``WITH CHECK``).

Postgres only; inert while services connect as the superuser ``trader`` (see 0039).

Revision ID: 0040_rls_account_scoped
Revises: 0039_rls_tenant_isolation
Create Date: 2026-07-02
"""

from __future__ import annotations

from alembic import op

revision = "0040_rls_account_scoped"
down_revision = "0039_rls_tenant_isolation"
branch_labels = None
depends_on = None

# account_id -> linked_broker_accounts.user_id
_ACCOUNT_SCOPED = ("broker_credentials", "orders", "positions", "opp_sub_execution_bindings")

_ACCOUNT_OWNED = (
    "account_id IN (SELECT account_id FROM linked_broker_accounts "
    "WHERE user_id = current_setting('app.current_tenant', true))"
)
_CROSS = "current_setting('app.cross_tenant', true) = 'on'"

# executions has no account_id — it hangs off order_id -> orders.account_id.
_EXEC_OWNED = (
    "order_id IN (SELECT order_id FROM orders WHERE account_id IN "
    "(SELECT account_id FROM linked_broker_accounts "
    "WHERE user_id = current_setting('app.current_tenant', true)))"
)


def _policy(table: str, owned: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        f"FOR ALL USING (({owned}) OR {_CROSS}) WITH CHECK ({owned})"
    )


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    for table in _ACCOUNT_SCOPED:
        _policy(table, _ACCOUNT_OWNED)
    _policy("executions", _EXEC_OWNED)


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    for table in (*_ACCOUNT_SCOPED, "executions"):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
