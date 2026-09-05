"""Add canonical_signal_id to execution_logs for signal-execution traceability."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0015_exec_log_canon_sig"
down_revision = "0014_signal_perf_run_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {column["name"] for column in inspector.get_columns("execution_logs")}
    if "canonical_signal_id" not in columns:
        op.add_column(
            "execution_logs",
            sa.Column("canonical_signal_id", sa.BigInteger(), nullable=True),
        )

    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("execution_logs")}
    if (
        "fk_execution_logs_canonical_signal_id" not in foreign_keys
        and "fk_execution_logs_canonical_signal" not in foreign_keys
    ):
        op.create_foreign_key(
            "fk_execution_logs_canonical_signal_id",
            "execution_logs",
            "canonical_signals",
            ["canonical_signal_id"],
            ["signal_id"],
        )

    indexes = {index["name"] for index in inspector.get_indexes("execution_logs")}
    if "ix_execution_logs_canonical_signal_id" not in indexes:
        op.create_index(
            "ix_execution_logs_canonical_signal_id",
            "execution_logs",
            ["canonical_signal_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    indexes = {index["name"] for index in inspector.get_indexes("execution_logs")}
    if "ix_execution_logs_canonical_signal_id" in indexes:
        op.drop_index("ix_execution_logs_canonical_signal_id", table_name="execution_logs")

    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("execution_logs")}
    if "fk_execution_logs_canonical_signal_id" in foreign_keys:
        op.drop_constraint(
            "fk_execution_logs_canonical_signal_id",
            "execution_logs",
            type_="foreignkey",
        )

    columns = {column["name"] for column in inspector.get_columns("execution_logs")}
    if "canonical_signal_id" in columns:
        op.drop_column("execution_logs", "canonical_signal_id")
