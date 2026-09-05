"""Add service_heartbeats (feedback heartbeat).

The feedback loop runs as a one-shot and exits, so an in-process Prometheus
gauge is never scraped. It now records a last-success heartbeat row per cycle;
a monitor reads ``last_success_at`` to alert when the loop has gone stale.
Generic (keyed on service_name) so other one-shot/cron services can reuse it.

Table create is a postgres-deploy concern; sqlite test fixtures build their
schema from the models via create_all, so this no-ops on sqlite.

Revision ID: 0034_service_heartbeats
Revises: 0033_signal_perf_did_execute
Create Date: 2026-06-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0034_service_heartbeats"
down_revision = "0033_signal_perf_did_execute"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    op.create_table(
        "service_heartbeats",
        sa.Column("service_name", sa.String(length=64), primary_key=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_status",
            sa.String(length=20),
            nullable=False,
            server_default="ok",
        ),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    op.drop_table("service_heartbeats")
