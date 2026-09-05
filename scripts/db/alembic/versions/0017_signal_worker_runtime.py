"""Add canonical signal idempotency column and worker watermarks table.

Revision ID: 0017_signal_worker_runtime
Revises: 0016_autopilot_default_true
Create Date: 2026-03-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0017_signal_worker_runtime"
down_revision = "0016_autopilot_default_true"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "canonical_signals",
        sa.Column("external_signal_id", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_canonical_signal_ext_id",
        "canonical_signals",
        ["external_signal_id"],
        unique=True,
    )

    op.create_table(
        "watermarks",
        sa.Column("worker_id", sa.String(length=100), nullable=False),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("timeframe", sa.String(length=20), nullable=False),
        sa.Column("last_ts", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("worker_id", "symbol", "timeframe"),
    )


def downgrade() -> None:
    op.drop_table("watermarks")
    op.drop_index("ix_canonical_signal_ext_id", table_name="canonical_signals")
    op.drop_column("canonical_signals", "external_signal_id")
