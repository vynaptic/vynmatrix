"""Checkpoint local-paper fill projection delivery.

Revision ID: 0083_paper_fill_projection
Revises: 0082_execution_replay_read
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0083_paper_fill_projection"
down_revision = "0082_execution_replay_read"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pending_orders",
        sa.Column("pending_projection_exec_id", sa.BigInteger()),
    )
    with op.batch_alter_table("pending_orders") as batch:
        batch.create_foreign_key(
            "fk_pending_order_projection_execution",
            "executions",
            ["pending_projection_exec_id"],
            ["exec_id"],
            ondelete="RESTRICT",
        )
        batch.create_index(
            "ix_pending_orders_pending_projection_exec_id",
            ["pending_projection_exec_id"],
        )

    if op.get_bind().dialect.name == "postgresql":
        op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE public.pending_orders TO vm_execution")
        op.execute("GRANT SELECT, INSERT ON TABLE public.execution_metrics TO vm_execution")


def downgrade() -> None:
    with op.batch_alter_table("pending_orders") as batch:
        batch.drop_index("ix_pending_orders_pending_projection_exec_id")
        batch.drop_constraint(
            "fk_pending_order_projection_execution",
            type_="foreignkey",
        )
    op.drop_column("pending_orders", "pending_projection_exec_id")
