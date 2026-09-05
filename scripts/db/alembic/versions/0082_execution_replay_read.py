"""Allow execution replay to validate exact strategy-version lineage.

Revision ID: 0082_execution_replay_read
Revises: 0081_indicator_source_lock
"""

from __future__ import annotations

from alembic import op

revision = "0082_execution_replay_read"
down_revision = "0081_indicator_source_lock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("GRANT SELECT ON TABLE public.strategy_versions TO vm_execution")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("REVOKE SELECT ON TABLE public.strategy_versions FROM vm_execution")
