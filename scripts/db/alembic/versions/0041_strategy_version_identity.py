"""Enforce one immutable catalogue row per strategy semantic version.

Revision ID: 0041_strategy_version_identity
Revises: 0040_rls_account_scoped
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0041_strategy_version_identity"
down_revision = "0040_rls_account_scoped"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM strategy_versions
                    GROUP BY strategy_id, semver
                    HAVING COUNT(*) > 1
                ) THEN
                    RAISE EXCEPTION
                        'strategy_versions contains duplicate strategy/semver lineage; '
                        'repair it before applying 0041_strategy_version_identity';
                END IF;
            END $$
            """
        )
    )
    op.create_unique_constraint(
        "uq_strategy_version_semver",
        "strategy_versions",
        ["strategy_id", "semver"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_strategy_version_semver",
        "strategy_versions",
        type_="unique",
    )
