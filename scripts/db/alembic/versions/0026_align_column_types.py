"""Align column types with the ORM models (lengths / numeric precision).

17 columns carry a looser type in the migrations than in the models — wider
VARCHARs, VARCHAR-instead-of-TEXT, and a couple of numeric widenings. The models
are the deployed truth (create_all), so this tightens the migration schema to
match them. (The 33 naive-vs-tz-aware timestamp diffs were fixed model-side; the
strategy_consecutive_wrong_tracker.feedback_id BIGINT mismatch was a model FK
bug fixed in the same change as this migration.)

Type alignment is a postgres-deploy concern; sqlite test fixtures build their
schema from the models via create_all, so the migration no-ops on sqlite.

Revision ID: 0026_align_column_types
Revises: 0025_align_not_null
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0026_align_column_types"
down_revision = "0025_align_not_null"
branch_labels = None
depends_on = None

# (table, column, model_type, old_migration_type) — model_type is the target.
_TYPE_CHANGES = [
    ("feature_flags", "flag_code", sa.String(100), sa.String(50)),
    ("feature_flags", "description", sa.Text(), sa.String(500)),
    ("instruments", "asset_class", sa.String(20), sa.String(50)),
    ("instruments", "canonical", sa.String(50), sa.String(100)),
    ("option_strategy_template_legs", "ratio", sa.Numeric(10, 4), sa.Integer()),
    ("option_strategy_templates", "description", sa.Text(), sa.String(500)),
    ("risk_mandates", "owner_type", sa.String(20), sa.String(50)),
    ("scoring_rules", "description", sa.Text(), sa.String(500)),
    ("scoring_rules", "target_type", sa.String(20), sa.String(50)),
    ("scoring_rules", "decay_half_life_hours", sa.Numeric(10, 2), sa.Float()),
    ("sectors", "code", sa.String(50), sa.String(100)),
    ("sectors", "asset_class", sa.String(20), sa.String(50)),
    ("sectors", "description", sa.Text(), sa.String(500)),
    ("sizing_profiles", "name", sa.String(100), sa.String(255)),
    ("sizing_profiles", "method", sa.String(30), sa.String(50)),
    ("user_feature_flags", "flag_code", sa.String(100), sa.String(50)),
    ("users", "tz", sa.String(50), sa.String(64)),
]


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    for table, column, new_type, _old in _TYPE_CHANGES:
        op.alter_column(table, column, type_=new_type)


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    for table, column, _new, old_type in _TYPE_CHANGES:
        op.alter_column(table, column, type_=old_type)
