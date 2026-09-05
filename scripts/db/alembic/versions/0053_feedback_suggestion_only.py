"""Retire the impossible runtime strategy-config apply surface.

Feedback remains an evidence and review workflow. Approved suggestions are
promoted through a separate source-controlled strategy change; runtime services
do not write strategy files.

Revision ID: 0053_feedback_suggestion_only
Revises: 0052_service_role_rls
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0053_feedback_suggestion_only"
down_revision = "0052_service_role_rls"
branch_labels = None
depends_on = None

_TABLE = "strategy_parameter_feedback"
_STATUS_CONSTRAINT = "ck_param_feedback_status"
_SUGGESTION_STATUS_CHECK = "status IN ('pending', 'approved', 'rejected', 'expired')"
_LEGACY_STATUS_CHECK = "status IN ('pending', 'approved', 'rejected', 'applied', 'expired')"


def _preserve_historical_apply_review() -> None:
    op.execute(
        """
        UPDATE strategy_parameter_feedback
        SET
            status = 'approved',
            review_notes = concat_ws(
                E'\\n',
                NULLIF(review_notes, ''),
                CASE
                    WHEN applied_at IS NULL
                    THEN 'Historical runtime apply status retired'
                    ELSE 'Historical runtime apply status retired; recorded at '
                         || applied_at::text
                END
            )
        WHERE status = 'applied'
        """
    )


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        _preserve_historical_apply_review()
        op.drop_constraint(_STATUS_CONSTRAINT, _TABLE, type_="check")
        op.drop_column(_TABLE, "config_file_path")
        op.drop_column(_TABLE, "applied_at")
        op.create_check_constraint(
            _STATUS_CONSTRAINT,
            _TABLE,
            _SUGGESTION_STATUS_CHECK,
        )
        return

    # SQLite fixtures are normally empty, but preserve the same status
    # transition without PostgreSQL-specific concat/cast syntax.
    op.execute(
        """
        UPDATE strategy_parameter_feedback
        SET
            status = 'approved',
            review_notes = CASE
                WHEN review_notes IS NULL OR review_notes = ''
                THEN 'Historical runtime apply status retired'
                ELSE review_notes || char(10) || 'Historical runtime apply status retired'
            END
        WHERE status = 'applied'
        """
    )
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_STATUS_CONSTRAINT, type_="check")
        batch.drop_column("config_file_path")
        batch.drop_column("applied_at")
        batch.create_check_constraint(_STATUS_CONSTRAINT, _SUGGESTION_STATUS_CHECK)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(_STATUS_CONSTRAINT, _TABLE, type_="check")
        op.add_column(
            _TABLE,
            sa.Column("config_file_path", sa.String(length=500), nullable=True),
        )
        op.add_column(
            _TABLE,
            sa.Column("applied_at", sa.DateTime(), nullable=True),
        )
        op.create_check_constraint(_STATUS_CONSTRAINT, _TABLE, _LEGACY_STATUS_CHECK)
        return

    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_STATUS_CONSTRAINT, type_="check")
        batch.add_column(sa.Column("config_file_path", sa.String(length=500), nullable=True))
        batch.add_column(sa.Column("applied_at", sa.DateTime(), nullable=True))
        batch.create_check_constraint(_STATUS_CONSTRAINT, _LEGACY_STATUS_CHECK)
