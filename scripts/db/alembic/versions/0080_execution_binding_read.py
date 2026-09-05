"""Allow execution to revalidate current binding authority.

Revision ID: 0080_execution_binding_read
Revises: 0079_feedback_concurrency_fences
"""

from __future__ import annotations

from alembic import op

revision = "0080_execution_binding_read"
down_revision = "0079_feedback_concurrency_fences"
branch_labels = None
depends_on = None

_POLICY_NAME = "user_strategy_bindings_execution_select"


def _grant_postgresql_execution_access() -> None:
    op.execute("GRANT SELECT ON TABLE public.user_strategy_bindings TO vm_execution")
    op.execute(f"DROP POLICY IF EXISTS {_POLICY_NAME} ON user_strategy_bindings")
    op.execute(
        f"CREATE POLICY {_POLICY_NAME} ON user_strategy_bindings "
        "FOR SELECT TO vm_execution USING (true)"
    )


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _grant_postgresql_execution_access()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"DROP POLICY IF EXISTS {_POLICY_NAME} ON user_strategy_bindings")
        op.execute("REVOKE SELECT ON TABLE public.user_strategy_bindings FROM vm_execution")
