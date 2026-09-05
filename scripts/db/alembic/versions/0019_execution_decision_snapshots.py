"""Add frozen policy/routing snapshots to execution decision logs.

Revision ID: 0019_exec_decision_snap
Revises: 0018_outbox_events
Create Date: 2026-03-07 23:56:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_exec_decision_snap"
down_revision = "0018_outbox_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_decision_logs",
        sa.Column("binding_config_snapshot", sa.JSON(), nullable=True),
    )
    op.add_column(
        "execution_decision_logs",
        sa.Column("broker_route_snapshot", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("execution_decision_logs", "broker_route_snapshot")
    op.drop_column("execution_decision_logs", "binding_config_snapshot")
