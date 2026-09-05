"""Add external_signal_id to asset_scores for idempotent score writes (SC-6).

upsert_score INSERTed a new asset_scores row on every score computation, so an
idempotent re-POST of the same signal (NOTIFY redelivery / worker restart) left
duplicate rows for the same (instrument, ts). That bloats the table and, because
recent_asset_alpha_history reads alpha_raw from these rows to warm the rolling
standardization on boot, double-counts the same value and skews the warmed
mean/std (interacts with SC-4).

Carry the originating signal's stable identity (Signal.external_signal_id) on the
score row and make the asset write idempotent on it: a re-POST UPDATEs its row;
distinct same-bar signals from different strategies carry different ids and stay
separate. The column is nullable + uniquely indexed — NULLs stay distinct so
legacy / identity-less signals fall back to the prior insert behavior.

Column add is a postgres-deploy concern; sqlite test fixtures build their schema
from the models via create_all, so the migration no-ops on sqlite. The index name
matches SQLAlchemy's ``index=True`` auto-naming (``ix_<table>_<column>``) so the
schema-drift gate stays at 0.

Revision ID: 0035_asset_score_ext_signal_id
Revises: 0034_service_heartbeats
Create Date: 2026-06-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0035_asset_score_ext_signal_id"
down_revision = "0034_service_heartbeats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    op.add_column("asset_scores", sa.Column("external_signal_id", sa.String(64), nullable=True))
    op.create_index(
        "ix_asset_scores_external_signal_id",
        "asset_scores",
        ["external_signal_id"],
        unique=True,
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    op.drop_index("ix_asset_scores_external_signal_id", table_name="asset_scores")
    op.drop_column("asset_scores", "external_signal_id")
