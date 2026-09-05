"""Retire the four consumer-less outbox topics.

Revision ID: 0105_retire_observational_topics
Revises: 0104_saxo_capability_flags

``signals.ingested``, ``signals.scored``, ``execution.results`` and
``feedback.ready`` were durably enqueued and handed to a no-op publisher; nothing
consumed them. Their producers are removed in the same change. Any row still
undelivered is marked published with a ``retired`` marker so no backlog is
stranded and no relay ever claims it again. Published rows stay as history.

The revision id is shorter than the file name because ``alembic_version`` is
``varchar(32)``.

Downgrade is a deliberate no-op: recreating producers for topics nobody consumes
has no value, and the marked rows remain readable.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0105_retire_observational_topics"
down_revision = "0104_saxo_capability_flags"
branch_labels = None
depends_on = None

_RETIRED_TOPICS = ("signals.ingested", "signals.scored", "execution.results", "feedback.ready")
_UNDELIVERED = ("pending", "failed", "in_progress")
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
        {"marker": _MARKER, "topics": list(_RETIRED_TOPICS), "statuses": list(_UNDELIVERED)},
    )


def downgrade() -> None:
    """Intentional no-op: retired rows stay published and producers are not recreated."""
