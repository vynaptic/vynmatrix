"""Let the backend read what the owner UI's readiness checklist reports.

Revision ID: 0109_backend_ui_readiness
Revises: 0108_deployment_record

The Strategies page explains why a bound strategy is or is not trading. Three
of its answers live in tables the backend role could not read: whether a signal
worker has processed market data for the strategy, what scoring last decided
for the binding, and whether a risk rule recently blocked the account.

This revision follows ``0107``: column-level ``SELECT`` on exactly the columns
the checklist selects, plus owner-scoped ``FOR SELECT`` policies. Model state,
deduplication keys, policy snapshots and breach context stay unreadable. The
decision ``reasoning`` column is granted because the one reason string the
checklist shows lives inside it; the read model returns that string and
nothing else from the document. Nothing is granted for INSERT, UPDATE, DELETE
or any sequence.

``strategy_runtime_states`` has no owner column: a worker's row is shared
strategy-model state. Its policy admits only strategies the deployment owner
has bound, so the backend sees exactly the workers the checklist asks about.
"""

from __future__ import annotations

from alembic import op

revision = "0109_backend_ui_readiness"
down_revision = "0108_deployment_record"
branch_labels = None
depends_on = None

_ROLE = "vm_backend"
_OWNER = "public.vm_deployment_owner_id()"
_TENANT = "current_setting('app.current_tenant', true)"
_DIRECT_OWNER = f"user_id = {_OWNER} AND user_id = {_TENANT}"
_BOUND_STRATEGY = (
    "strategy_id IN (SELECT b.strategy_id FROM public.user_strategy_bindings AS b "
    f"WHERE b.user_id = {_OWNER} AND b.user_id = {_TENANT})"
)

READ_COLUMNS: dict[str, tuple[str, ...]] = {
    "strategy_runtime_states": ("strategy_id", "consolidation_minutes", "last_source_ts"),
    "execution_decision_logs": (
        "decision_id",
        "user_id",
        "binding_id",
        "status",
        "reasoning",
        "ts",
    ),
    "risk_breaches": (
        "breach_id",
        "user_id",
        "broker_account_id",
        "occurred_at",
        "rule_code",
        "severity",
    ),
}

# The column each row policy filters on; granted even where no query names it.
POLICY_KEYS: dict[str, str] = {
    "strategy_runtime_states": "strategy_id",
    "execution_decision_logs": "user_id",
    "risk_breaches": "user_id",
}

ROW_POLICIES: dict[str, str] = {
    "strategy_runtime_states": _BOUND_STRATEGY,
    "execution_decision_logs": _DIRECT_OWNER,
    "risk_breaches": _DIRECT_OWNER,
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
