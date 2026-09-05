"""Add canonical_signal_id column to execution_logs table."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0013_add_canonical_signal_id"
down_revision = "0012_exec_metrics_window"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {column["name"] for column in inspector.get_columns("execution_logs")}
    if "canonical_signal_id" not in columns:
        op.add_column(
            "execution_logs", sa.Column("canonical_signal_id", sa.BigInteger(), nullable=True)
        )

    indexes = {index["name"] for index in inspector.get_indexes("execution_logs")}
    if "ix_execution_logs_canonical_signal_id" not in indexes:
        op.create_index(
            "ix_execution_logs_canonical_signal_id", "execution_logs", ["canonical_signal_id"]
        )

    tables = set(inspector.get_table_names())
    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("execution_logs")}
    if (
        "canonical_signals" in tables
        and "fk_execution_logs_canonical_signal" not in foreign_keys
        and "fk_execution_logs_canonical_signal_id" not in foreign_keys
    ):
        op.create_foreign_key(
            "fk_execution_logs_canonical_signal",
            "execution_logs",
            "canonical_signals",
            ["canonical_signal_id"],
            ["signal_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Drop foreign key constraint if it exists
    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("execution_logs")}
    if "fk_execution_logs_canonical_signal" in foreign_keys:
        op.drop_constraint(
            "fk_execution_logs_canonical_signal", "execution_logs", type_="foreignkey"
        )

    # Drop index
    indexes = {index["name"] for index in inspector.get_indexes("execution_logs")}
    if "ix_execution_logs_canonical_signal_id" in indexes:
        op.drop_index("ix_execution_logs_canonical_signal_id", table_name="execution_logs")

    # Drop column
    columns = {column["name"] for column in inspector.get_columns("execution_logs")}
    if "canonical_signal_id" in columns:
        op.drop_column("execution_logs", "canonical_signal_id")
