"""Allow execution_decision_logs to represent an in-flight execution claim.

Revision ID: 0022_execution_claim_status
Revises: 0021_outbox_events_notify
Create Date: 2026-05-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0022_execution_claim_status"
down_revision = "0021_outbox_events_notify"
branch_labels = None
depends_on = None

_OLD_STATUSES = "'pending', 'executed', 'rejected', 'failed', 'skipped'"
_NEW_STATUSES = "'pending', 'executing', 'executed', 'rejected', 'failed', 'skipped'"


def _replace_constraint(statuses: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("execution_decision_logs") as batch_op:
            batch_op.drop_constraint("ck_decision_status", type_="check")
            batch_op.create_check_constraint(
                "ck_decision_status",
                f"status IN ({statuses})",
            )
        return

    op.drop_constraint("ck_decision_status", "execution_decision_logs", type_="check")
    op.create_check_constraint(
        "ck_decision_status",
        "execution_decision_logs",
        f"status IN ({statuses})",
    )


def upgrade() -> None:
    _replace_constraint(_NEW_STATUSES)


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE execution_decision_logs " "SET status = 'failed' " "WHERE status = 'executing'"
        )
    )
    _replace_constraint(_OLD_STATUSES)
