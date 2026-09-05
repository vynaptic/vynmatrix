"""Add durable pending orders table for crash-safe execution recovery.

Revision ID: 0020_pending_orders
Revises: 0019_exec_decision_snap
Create Date: 2026-03-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0020_pending_orders"
down_revision = "0019_exec_decision_snap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_orders",
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=50), nullable=False),
        sa.Column("signal_id", sa.String(length=100), nullable=False),
        sa.Column("instr_id", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("order_type", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("limit_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("execution_mode", sa.String(length=30), nullable=False),
        sa.Column("broker", sa.String(length=30), nullable=False),
        sa.Column("broker_environment", sa.String(length=20), nullable=True),
        sa.Column("credential_ref", sa.String(length=100), nullable=True),
        sa.Column("breaker_key", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("broker_order_id", sa.String(length=100), nullable=True),
        sa.Column("filled_quantity", sa.Numeric(20, 8), nullable=True),
        sa.Column("filled_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("commission", sa.Numeric(20, 8), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("strategy_id", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'submitted', 'filled', 'partially_filled', "
            "'cancelled', 'expired', 'rejected')",
            name="ck_pending_order_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["instr_id"], ["instruments.instr_id"]),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.strategy_id"]),
        sa.PrimaryKeyConstraint("order_id"),
    )
    op.create_index("ix_pending_orders_signal_id", "pending_orders", ["signal_id"], unique=False)
    op.create_index("ix_pending_orders_status", "pending_orders", ["status"], unique=False)
    op.create_index(
        "ix_pending_orders_created_at",
        "pending_orders",
        ["created_at"],
        unique=False,
    )
    op.create_index("ix_pending_orders_run_id", "pending_orders", ["run_id"], unique=False)
    op.create_index(
        "ix_pending_orders_idempotency_key",
        "pending_orders",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_pending_orders_breaker_key",
        "pending_orders",
        ["breaker_key"],
        unique=False,
    )
    op.create_index(
        "ix_pending_order_breaker_status",
        "pending_orders",
        ["breaker_key", "status"],
        unique=False,
    )
    op.create_index(
        "ix_pending_order_user_status",
        "pending_orders",
        ["user_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_pending_order_user_status", table_name="pending_orders")
    op.drop_index("ix_pending_order_breaker_status", table_name="pending_orders")
    op.drop_index("ix_pending_orders_breaker_key", table_name="pending_orders")
    op.drop_index("ix_pending_orders_idempotency_key", table_name="pending_orders")
    op.drop_index("ix_pending_orders_run_id", table_name="pending_orders")
    op.drop_index("ix_pending_orders_created_at", table_name="pending_orders")
    op.drop_index("ix_pending_orders_status", table_name="pending_orders")
    op.drop_index("ix_pending_orders_signal_id", table_name="pending_orders")
    op.drop_table("pending_orders")
