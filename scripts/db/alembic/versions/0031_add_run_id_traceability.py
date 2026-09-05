"""Add run_id trace columns to execution_decision_logs and execution_logs (DB-1).

canonical_signals / pending_orders / execution_metrics / signal_performance /
decision_contexts already carry run_id, but the decision + execution rows did
not (run_id lived only inside execution_logs.execution_details JSON), so an RCA
join across the run chain needed three different keys. Add an indexed run_id
column to both so a single ``WHERE run_id = :x`` spans the chain.

Column add is a postgres-deploy concern; sqlite test fixtures build their schema
from the models via create_all, so the migration no-ops on sqlite. Index names
match SQLAlchemy's ``index=True`` auto-naming (``ix_<table>_<column>``) so the
schema-drift gate stays at 0.

Revision ID: 0031_add_run_id_traceability
Revises: 0030_decision_contexts
Create Date: 2026-06-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0031_add_run_id_traceability"
down_revision = "0030_decision_contexts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    op.add_column("execution_decision_logs", sa.Column("run_id", sa.String(64), nullable=True))
    op.create_index("ix_execution_decision_logs_run_id", "execution_decision_logs", ["run_id"])
    op.add_column("execution_logs", sa.Column("run_id", sa.String(64), nullable=True))
    op.create_index("ix_execution_logs_run_id", "execution_logs", ["run_id"])


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    op.drop_index("ix_execution_logs_run_id", table_name="execution_logs")
    op.drop_column("execution_logs", "run_id")
    op.drop_index("ix_execution_decision_logs_run_id", table_name="execution_decision_logs")
    op.drop_column("execution_decision_logs", "run_id")
