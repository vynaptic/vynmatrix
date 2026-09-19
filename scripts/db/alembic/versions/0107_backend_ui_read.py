"""Let the backend read the owner UI's projections, fills, signals and prices.

Revision ID: 0107_backend_ui_read
Revises: 0106_retire_topic_dead_letters

The owner UI is served by the backend, whose role could read only control-plane
and catalogue tables. This revision adds the narrowest read surface the UI's
read API needs: column-level ``SELECT`` on exactly the columns it selects, so
order payloads, client and broker order references, signal features and
execution-metric metadata stay unreadable, plus owner-scoped ``FOR SELECT``
policies on the row-secured tables in the style of ``0102``. Nothing is granted
for INSERT, UPDATE, DELETE or any sequence.

``canonical_signals`` and ``prices`` carry no row security: they are pipeline
and reference rows of the single deployment owner.
"""

from __future__ import annotations

from alembic import op

revision = "0107_backend_ui_read"
down_revision = "0106_retire_topic_dead_letters"
branch_labels = None
depends_on = None

_ROLE = "vm_backend"
_OWNER = "public.vm_deployment_owner_id()"
_TENANT = "current_setting('app.current_tenant', true)"
_DIRECT_OWNER = f"user_id = {_OWNER} AND user_id = {_TENANT}"
_OWNER_ACCOUNTS = (
    "SELECT a.account_id FROM public.linked_broker_accounts AS a "
    f"WHERE a.user_id = {_OWNER} AND a.user_id = {_TENANT}"
)
_ACCOUNT_OWNER = f"account_id IN ({_OWNER_ACCOUNTS})"
_EXECUTION_OWNER = (
    "order_id IN (SELECT o.order_id FROM public.orders AS o "
    f"WHERE o.account_id IN ({_OWNER_ACCOUNTS}))"
)

READ_COLUMNS: dict[str, tuple[str, ...]] = {
    "daily_nav": ("user_id", "account_id", "date", "nav_ccy", "nav_value"),
    "execution_metrics": (
        "user_id",
        "account_id",
        "strategy_id",
        "symbol",
        "execution_mode",
        "equity",
        "realized_pnl",
        "created_at",
    ),
    "order_intents": ("intent_id", "user_id", "strategy_id", "side"),
    "orders": ("order_id", "intent_id", "account_id", "settlement_currency"),
    "positions": (
        "account_id",
        "instr_id",
        "qty",
        "avg_price",
        "last_mark",
        "gross_notional",
        "notional_currency",
        "updated_at",
    ),
    "executions": (
        "exec_id",
        "order_id",
        "instr_id",
        "fill_ts",
        "qty",
        "price",
        "fee_ccy",
        "fee_amount",
        "venue",
    ),
    "canonical_signals": ("strategy_id", "instr_id", "action", "confidence", "ts"),
    "prices": ("ts",),
}

# The column each row policy filters on; granted even where no query names it.
POLICY_KEYS: dict[str, str] = {
    "daily_nav": "user_id",
    "execution_metrics": "user_id",
    "order_intents": "user_id",
    "orders": "account_id",
    "positions": "account_id",
    "executions": "order_id",
}

ROW_POLICIES: dict[str, str] = {
    "daily_nav": _DIRECT_OWNER,
    "execution_metrics": _DIRECT_OWNER,
    "order_intents": _DIRECT_OWNER,
    "orders": _ACCOUNT_OWNER,
    "positions": _ACCOUNT_OWNER,
    "executions": _EXECUTION_OWNER,
}


def _policy_name(table: str) -> str:
    return f"{table}_backend_select"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table, columns in READ_COLUMNS.items():
        op.execute(f"GRANT SELECT ({', '.join(columns)}) ON TABLE public.{table} TO {_ROLE}")
    for table, predicate in ROW_POLICIES.items():
        op.execute(f"DROP POLICY IF EXISTS {_policy_name(table)} ON public.{table}")
        op.execute(
            f"CREATE POLICY {_policy_name(table)} ON public.{table} "
            f"FOR SELECT TO {_ROLE} USING ({predicate})"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in ROW_POLICIES:
        op.execute(f"DROP POLICY IF EXISTS {_policy_name(table)} ON public.{table}")
    for table, columns in READ_COLUMNS.items():
        op.execute(f"REVOKE SELECT ({', '.join(columns)}) ON TABLE public.{table} FROM {_ROLE}")


__all__ = ["POLICY_KEYS", "READ_COLUMNS", "ROW_POLICIES", "downgrade", "upgrade"]
