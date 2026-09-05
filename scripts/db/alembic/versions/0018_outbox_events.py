"""Add transactional outbox events table.

Revision ID: 0018_outbox_events
Revises: 0017_signal_worker_runtime
Create Date: 2026-03-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0018_outbox_events"
down_revision = "0017_signal_worker_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("event_id", sa.String(length=50), nullable=False),
        sa.Column("topic", sa.String(length=100), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("schema_version", sa.String(length=20), nullable=False),
        sa.Column("aggregate_type", sa.String(length=50), nullable=True),
        sa.Column("aggregate_id", sa.String(length=100), nullable=True),
        sa.Column("event_key", sa.String(length=128), nullable=True),
        sa.Column("ordering_key", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("headers", sa.JSON(), nullable=False),
        sa.Column("delivery_metadata", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("claim_owner", sa.String(length=100), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'published', 'failed', 'dead_letter')",
            name="ck_outbox_status",
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_outbox_events_topic", "outbox_events", ["topic"], unique=False)
    op.create_index("ix_outbox_events_status", "outbox_events", ["status"], unique=False)
    op.create_index(
        "ix_outbox_events_available_at",
        "outbox_events",
        ["available_at"],
        unique=False,
    )
    op.create_index(
        "ix_outbox_events_created_at",
        "outbox_events",
        ["created_at"],
        unique=False,
    )
    op.create_index("ix_outbox_events_event_key", "outbox_events", ["event_key"], unique=True)
    op.create_index(
        "ix_outbox_topic_status_available",
        "outbox_events",
        ["topic", "status", "available_at"],
        unique=False,
    )
    op.create_index(
        "ix_outbox_aggregate",
        "outbox_events",
        ["aggregate_type", "aggregate_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_aggregate", table_name="outbox_events")
    op.drop_index("ix_outbox_topic_status_available", table_name="outbox_events")
    op.drop_index("ix_outbox_events_event_key", table_name="outbox_events")
    op.drop_index("ix_outbox_events_created_at", table_name="outbox_events")
    op.drop_index("ix_outbox_events_available_at", table_name="outbox_events")
    op.drop_index("ix_outbox_events_status", table_name="outbox_events")
    op.drop_index("ix_outbox_events_topic", table_name="outbox_events")
    op.drop_table("outbox_events")
