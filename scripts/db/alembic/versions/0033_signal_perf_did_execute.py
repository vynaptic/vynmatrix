"""Add did_execute to signal_performance (DB-2).

signal_performance recorded only a hypothetical price-move P&L, fully decoupled
from whether the signal was actually traded. Record did_execute (resolved from
execution_logs by run_id) so the feedback is grounded in real trading and signal
quality can be analyzed separately from execution.

Nullable column add is a postgres-deploy concern; sqlite test fixtures build
their schema from the models via create_all, so this no-ops on sqlite.

Revision ID: 0033_signal_perf_did_execute
Revises: 0032_tracker_horizon
Create Date: 2026-06-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0033_signal_perf_did_execute"
down_revision = "0032_tracker_horizon"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    op.add_column("signal_performance", sa.Column("did_execute", sa.Boolean(), nullable=True))


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    op.drop_column("signal_performance", "did_execute")
