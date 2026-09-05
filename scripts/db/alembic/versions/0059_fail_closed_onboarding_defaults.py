"""Make new strategy bindings inactive and manual by default.

Existing rows are intentionally untouched: an active or automatic binding may
represent an explicit operator decision. Only the database defaults for future
rows change, matching the backend request model's fail-closed defaults. The
downgrade intentionally preserves those safety defaults.

Revision ID: 0059_fail_closed_onboarding
Revises: 0058_risk_ownership
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0059_fail_closed_onboarding"
down_revision = "0058_risk_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "user_strategy_bindings",
        "is_active",
        existing_type=sa.Boolean(),
        server_default="false",
        existing_nullable=False,
    )
    op.alter_column(
        "user_strategy_bindings",
        "autopilot",
        existing_type=sa.Boolean(),
        server_default="false",
        existing_nullable=False,
    )


def downgrade() -> None:
    # This safety default is intentionally non-symmetric. Returning to the
    # predecessor revision must not make future bindings active or automatic
    # without an explicit operator decision.
    op.alter_column(
        "user_strategy_bindings",
        "is_active",
        existing_type=sa.Boolean(),
        server_default="false",
        existing_nullable=False,
    )
    op.alter_column(
        "user_strategy_bindings",
        "autopilot",
        existing_type=sa.Boolean(),
        server_default="false",
        existing_nullable=False,
    )
