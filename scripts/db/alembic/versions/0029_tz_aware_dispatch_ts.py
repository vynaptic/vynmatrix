"""Make the dispatch/outbox timestamp columns timezone-aware.

The transactional-outbox edge tables (``outbox_events``, ``pending_orders``,
``execution_decision_logs``) were created with naive ``TIMESTAMP`` columns
(0003/0005/0018/0020) while the rest of the schema standardised on
``TIMESTAMP WITH TIME ZONE``. The 224->0 drift reconcile only made model and
migration *consistent* (both naive) for these tables; it did not make them
tz-aware. The models write ``datetime.now(tz=UTC)`` already, so storing them in
naive columns silently drops the offset and invites cross-timezone comparison
bugs in the relay/reconciliation workers. This aligns the 9 dispatch timestamp
columns with the model (now ``DateTime(timezone=True)``) and the rest of the DB.

Type alignment is a postgres-deploy concern; sqlite test fixtures build their
schema from the models via create_all, so the migration no-ops on sqlite.
The platform is pre-prod with no rows in these tables, so no data conversion is
needed (timestamp -> timestamptz reads existing values as the session TZ).

Revision ID: 0029_tz_aware_dispatch_ts
Revises: 0028_align_indexes_fks
Create Date: 2026-06-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0029_tz_aware_dispatch_ts"
down_revision = "0028_align_indexes_fks"
branch_labels = None
depends_on = None

# (table, column) — every timestamp column on the dispatch/outbox edge tables.
_TZ_COLUMNS = [
    ("execution_decision_logs", "ts"),
    ("pending_orders", "created_at"),
    ("pending_orders", "submitted_at"),
    ("pending_orders", "resolved_at"),
    ("outbox_events", "available_at"),
    ("outbox_events", "claimed_at"),
    ("outbox_events", "published_at"),
    ("outbox_events", "created_at"),
    ("outbox_events", "updated_at"),
]


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    for table, column in _TZ_COLUMNS:
        op.alter_column(table, column, type_=sa.DateTime(timezone=True))


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    for table, column in _TZ_COLUMNS:
        op.alter_column(table, column, type_=sa.DateTime(timezone=False))
