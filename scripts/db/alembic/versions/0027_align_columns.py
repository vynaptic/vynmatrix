"""Align table columns with the ORM models (add/drop superseded columns).

- scoring_rules.asset_class exists in the model (NULL = all asset classes) but
  was missing from the migrations -> add it.
- The users table was evolved in the models (name -> full_name, is_active ->
  status) and dropped risk_profile/updated_at; the early migration still creates
  the superseded columns. None are referenced in code -> drop them.

Column add/drop is a postgres-deploy concern; sqlite test fixtures build their
schema from the models via create_all, so the migration no-ops on sqlite.

Revision ID: 0027_align_columns
Revises: 0026_align_column_types
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0027_align_columns"
down_revision = "0026_align_column_types"
branch_labels = None
depends_on = None

_DROPPED_USER_COLS = [
    ("name", sa.String(255)),
    ("is_active", sa.Boolean()),
    ("updated_at", sa.DateTime(timezone=True)),
    ("risk_profile", sa.String(20)),
]


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    op.add_column("scoring_rules", sa.Column("asset_class", sa.String(20), nullable=True))
    for col, _type in _DROPPED_USER_COLS:
        op.drop_column("users", col)


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    for col, col_type in _DROPPED_USER_COLS:
        op.add_column("users", sa.Column(col, col_type, nullable=True))
    op.drop_column("scoring_rules", "asset_class")
