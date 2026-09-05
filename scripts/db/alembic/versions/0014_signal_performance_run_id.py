"""Add run_id to signal_performance for cross-container tracing into feedback loop.

Propagates the run_id correlation ID from canonical_signals through to
signal_performance so that feedback evaluations can be traced end-to-end.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0014_signal_perf_run_id"
down_revision = "0013_gap_fix_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("signal_performance", sa.Column("run_id", sa.String(64)))
    op.create_index("ix_signal_performance_run_id", "signal_performance", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_signal_performance_run_id", table_name="signal_performance")
    op.drop_column("signal_performance", "run_id")
