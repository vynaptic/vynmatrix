"""Add backtest experiments for parameter sweeps and walk-forward runs."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0008_backtest_experiments"
down_revision = "0007_add_prices_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backtest_experiments",
        sa.Column("experiment_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "strategy_id",
            sa.String(length=50),
            sa.ForeignKey("strategies.strategy_id"),
            nullable=False,
        ),
        sa.Column("experiment_type", sa.String(length=30), nullable=False),
        sa.Column("metric", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "experiment_type IN ('parameter_sweep', 'walk_forward')",
            name="ck_backtest_experiment_type",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_backtest_experiment_status",
        ),
    )
    op.create_index(
        "ix_backtest_experiment_strategy",
        "backtest_experiments",
        ["strategy_id", "created_at"],
    )

    op.add_column(
        "backtest_results",
        sa.Column(
            "experiment_id",
            sa.BigInteger(),
            sa.ForeignKey("backtest_experiments.experiment_id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_backtest_results_experiment",
        "backtest_results",
        ["experiment_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_backtest_results_experiment", table_name="backtest_results")
    op.drop_column("backtest_results", "experiment_id")

    op.drop_index("ix_backtest_experiment_strategy", table_name="backtest_experiments")
    op.drop_table("backtest_experiments")
