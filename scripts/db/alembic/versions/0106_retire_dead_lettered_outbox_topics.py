"""Retire the dead-lettered rows of the four consumer-less outbox topics.

Revision ID: 0106_retire_topic_dead_letters
Revises: 0105_retire_observational_topics

``0105`` marked the pending, failed and in-progress rows of ``signals.ingested``,
``signals.scored``, ``execution.results`` and ``feedback.ready`` published, but
left their ``dead_letter`` rows untouched. The soak-acceptance outbox check counts
dead letters across every topic, so one such row would fail acceptance for a
topic nobody consumes, while the admin dead-letter listing and redrive are
limited to the execution topics and cannot act on it. This revision applies the
same ``retired`` marker to those rows. ``last_error`` and ``failure_class`` are
kept as evidence of the original failure.

The revision id is shorter than the file name because ``alembic_version`` is
``varchar(32)``.

Downgrade is a deliberate no-op: the marker preserves provenance and the topics
have no consumer to restore.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0106_retire_topic_dead_letters"
down_revision = "0105_retire_observational_topics"
branch_labels = None
depends_on = None

_RETIRED_TOPICS = ("signals.ingested", "signals.scored", "execution.results", "feedback.ready")
_STATUSES = ("dead_letter",)
_MARKER = json.dumps({"publisher": "retired", "revision": revision})


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # ``outbox_events.delivery_metadata`` is a plain ``json`` column (JSONType = JSON).
        metadata_expr = "CAST(:marker AS json)"
        now_expr = "now()"
    else:
        metadata_expr = ":marker"
        now_expr = "CURRENT_TIMESTAMP"
    statement = sa.text(
        "UPDATE outbox_events "
        "SET status = 'published', "
        f"    published_at = COALESCE(published_at, {now_expr}), "
        "    claimed_at = NULL, "
        "    claim_owner = NULL, "
        f"    delivery_metadata = {metadata_expr}, "
        f"    updated_at = {now_expr} "
        "WHERE topic IN :topics AND status IN :statuses"
    ).bindparams(
        sa.bindparam("topics", expanding=True),
        sa.bindparam("statuses", expanding=True),
    )
    bind.execute(
        statement,
        {"marker": _MARKER, "topics": list(_RETIRED_TOPICS), "statuses": list(_STATUSES)},
    )


def downgrade() -> None:
    """Intentional no-op: retired rows stay published and producers are not recreated."""
