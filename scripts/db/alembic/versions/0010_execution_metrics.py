"""Add execution metrics table for user/strategy performance snapshots."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0010_execution_metrics"
down_revision = "0008_backtest_experiments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "execution_metrics",
        sa.Column("metric_id", sa.String(length=50), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=50),
            sa.ForeignKey("users.user_id"),
            nullable=False,
        ),
        sa.Column(
            "strategy_id",
            sa.String(length=50),
            sa.ForeignKey("strategies.strategy_id"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("execution_mode", sa.String(length=20), nullable=False),
        sa.Column("broker", sa.String(length=20), nullable=False),
        sa.Column("signal_id", sa.String(length=100)),
        sa.Column("run_id", sa.String(length=100)),
        sa.Column("asset_class", sa.String(length=20)),
        sa.Column("equity", sa.Numeric(20, 8)),
        sa.Column("available_cash", sa.Numeric(20, 8)),
        sa.Column("margin_used", sa.Numeric(20, 8)),
        sa.Column("unrealized_pnl", sa.Numeric(20, 8)),
        sa.Column("realized_pnl", sa.Numeric(20, 8)),
        sa.Column("orders_submitted", sa.Integer(), server_default="0"),
        sa.Column("orders_filled", sa.Integer(), server_default="0"),
        sa.Column("total_commission", sa.Numeric(20, 8)),
        sa.Column("peak_equity", sa.Numeric(20, 8)),
        sa.Column("drawdown_pct", sa.Numeric(10, 6)),
        sa.Column("metadata", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_execution_metrics_user_strategy_time",
        "execution_metrics",
        ["user_id", "strategy_id", "created_at"],
    )
    op.create_index(
        "ix_execution_metrics_symbol",
        "execution_metrics",
        ["symbol"],
    )


def downgrade() -> None:
    op.drop_index("ix_execution_metrics_symbol", table_name="execution_metrics")
    op.drop_index("ix_execution_metrics_user_strategy_time", table_name="execution_metrics")
    op.drop_table("execution_metrics")
