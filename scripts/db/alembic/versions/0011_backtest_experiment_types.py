"""Expand backtest experiment types for stress tests and Monte Carlo."""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0011_backtest_experiment_types"
down_revision = "0010_execution_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_backtest_experiment_type",
        "backtest_experiments",
        type_="check",
    )
    op.create_check_constraint(
        "ck_backtest_experiment_type",
        "backtest_experiments",
        "experiment_type IN ('parameter_sweep', 'walk_forward', 'stress_test', 'monte_carlo')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_backtest_experiment_type",
        "backtest_experiments",
        type_="check",
    )
    op.create_check_constraint(
        "ck_backtest_experiment_type",
        "backtest_experiments",
        "experiment_type IN ('parameter_sweep', 'walk_forward')",
    )
