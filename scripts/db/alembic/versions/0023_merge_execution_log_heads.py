"""Merge execution log canonical signal migration heads.

Revision ID: 0023_merge_execution_log_heads
Revises: 0022_execution_claim_status, 0013_add_canonical_signal_id
Create Date: 2026-05-09
"""

from __future__ import annotations

# revision identifiers, used by Alembic.
revision = "0023_merge_execution_log_heads"
down_revision = ("0022_execution_claim_status", "0013_add_canonical_signal_id")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Merge-only revision; schema changes are in parent revisions."""


def downgrade() -> None:
    """Merge-only revision; no schema changes to revert."""
