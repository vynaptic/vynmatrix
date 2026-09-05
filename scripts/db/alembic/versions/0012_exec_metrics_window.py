"""Add time-window + exposure fields to execution metrics."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0012_exec_metrics_window"
down_revision = "0011_backtest_experiment_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("execution_metrics", sa.Column("drawdown_duration_sec", sa.Numeric(20, 2)))
    op.add_column("execution_metrics", sa.Column("max_drawdown_duration_sec", sa.Numeric(20, 2)))
    op.add_column("execution_metrics", sa.Column("window_start", sa.DateTime(timezone=True)))
    op.add_column("execution_metrics", sa.Column("window_end", sa.DateTime(timezone=True)))
    op.add_column("execution_metrics", sa.Column("exposure", sa.Numeric(20, 8)))
    op.add_column("execution_metrics", sa.Column("leverage", sa.Numeric(10, 6)))
    op.create_index(
        "ix_execution_metrics_window_end",
        "execution_metrics",
        ["user_id", "strategy_id", "symbol", "window_end"],
    )


def downgrade() -> None:
    op.drop_index("ix_execution_metrics_window_end", table_name="execution_metrics")
    op.drop_column("execution_metrics", "leverage")
    op.drop_column("execution_metrics", "exposure")
    op.drop_column("execution_metrics", "window_end")
    op.drop_column("execution_metrics", "window_start")
    op.drop_column("execution_metrics", "max_drawdown_duration_sec")
    op.drop_column("execution_metrics", "drawdown_duration_sec")
