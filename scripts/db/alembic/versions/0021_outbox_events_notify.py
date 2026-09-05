"""Add LISTEN/NOTIFY trigger so OutboxRelayWorker wakes on new events.

Revision ID: 0021_outbox_events_notify
Revises: 0020_pending_orders
Create Date: 2026-05-03

This migration installs an ``AFTER INSERT`` trigger on ``outbox_events`` that
emits ``pg_notify('outbox_events', payload)`` for rows newly enqueued in the
``pending`` status. The ``OutboxRelayWorker`` LISTENs on this channel so it
can short-circuit its poll interval and drain immediately when new work is
available, instead of waiting up to ``poll_interval_seconds`` for the next
tick.

The notification payload is intentionally minimal (``topic``, ``event_id``) —
the relay still uses ``claim_outbox_batch`` to claim the row with proper lease
semantics. The notification is just a wake-up signal, not a delivery channel.

Polling is retained as a safety net (catch-up after reconnect, missed
notifications, dead-letter retries).
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0021_outbox_events_notify"
down_revision = "0020_pending_orders"
branch_labels = None
depends_on = None


_NOTIFY_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION outbox_events_notify_pending()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    payload text;
BEGIN
    -- Only emit on initial pending inserts; failed/retry transitions go
    -- through UPDATE and are picked up by the catch-up poll. Keeping the
    -- payload tiny avoids bumping into the 8000-byte NOTIFY limit.
    IF NEW.status = 'pending' THEN
        payload := json_build_object(
            'topic', NEW.topic,
            'event_id', NEW.event_id
        )::text;
        PERFORM pg_notify('outbox_events', payload);
    END IF;
    RETURN NEW;
END;
$$;
"""

_TRIGGER_SQL = """
DROP TRIGGER IF EXISTS trg_outbox_events_notify_pending ON outbox_events;
CREATE TRIGGER trg_outbox_events_notify_pending
AFTER INSERT ON outbox_events
FOR EACH ROW
EXECUTE FUNCTION outbox_events_notify_pending();
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite (used by unit tests) has no LISTEN/NOTIFY; the relay falls
        # back to its existing poll loop in that environment.
        return
    op.execute(_NOTIFY_FUNCTION_SQL)
    op.execute(_TRIGGER_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS trg_outbox_events_notify_pending ON outbox_events;")
    op.execute("DROP FUNCTION IF EXISTS outbox_events_notify_pending();")
