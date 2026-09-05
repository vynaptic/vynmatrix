"""Add columns for gap fixes: run_id, idempotency_key, allowed_brokers.

- canonical_signals.run_id: cross-container correlation (GAP-2)
- execution_decision_logs.idempotency_key/signal_id/action: distributed dedup (GAP-14)
- execution_decision_logs: relax NOT NULL on instr_id/asset_score/should_execute for dedup rows
- user_strategy_bindings.allowed_brokers: broker restrictions (GAP-17)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0013_gap_fix_columns"
down_revision = "0012_exec_metrics_window"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # GAP-2: canonical_signals.run_id
    op.add_column("canonical_signals", sa.Column("run_id", sa.String(64)))
    op.create_index("ix_canonical_signal_run_id", "canonical_signals", ["run_id"])

    # GAP-14: execution_decision_logs deduplication columns
    op.add_column("execution_decision_logs", sa.Column("idempotency_key", sa.String(64)))
    op.add_column("execution_decision_logs", sa.Column("signal_id", sa.String(100)))
    op.add_column("execution_decision_logs", sa.Column("action", sa.String(20)))
    op.create_index(
        "ix_exec_decision_idempotency_key",
        "execution_decision_logs",
        ["idempotency_key"],
        unique=True,
    )

    # GAP-14: relax NOT NULL on columns that dedup rows cannot populate
    # (dedup inserts don't have real instrument IDs or scores)
    op.alter_column(
        "execution_decision_logs", "instr_id", existing_type=sa.Integer(), nullable=True
    )
    op.alter_column(
        "execution_decision_logs",
        "asset_score",
        existing_type=sa.Numeric(10, 4),
        nullable=True,
    )
    op.alter_column(
        "execution_decision_logs", "should_execute", existing_type=sa.Boolean(), nullable=True
    )

    # GAP-17: user_strategy_bindings.allowed_brokers
    op.add_column("user_strategy_bindings", sa.Column("allowed_brokers", sa.JSON))


def downgrade() -> None:
    op.drop_column("user_strategy_bindings", "allowed_brokers")

    op.alter_column(
        "execution_decision_logs", "should_execute", existing_type=sa.Boolean(), nullable=False
    )
    op.alter_column(
        "execution_decision_logs",
        "asset_score",
        existing_type=sa.Numeric(10, 4),
        nullable=False,
    )
    op.alter_column(
        "execution_decision_logs", "instr_id", existing_type=sa.Integer(), nullable=False
    )

    op.drop_index("ix_exec_decision_idempotency_key", table_name="execution_decision_logs")
    op.drop_column("execution_decision_logs", "action")
    op.drop_column("execution_decision_logs", "signal_id")
    op.drop_column("execution_decision_logs", "idempotency_key")
    op.drop_index("ix_canonical_signal_run_id", table_name="canonical_signals")
    op.drop_column("canonical_signals", "run_id")
