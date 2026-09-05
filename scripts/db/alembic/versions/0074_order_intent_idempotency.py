"""Add an account-scoped idempotency identity to canonical order intents.

Revision ID: 0074_order_intent_idempotency
Revises: 0073_feedback_catalogue_gate
"""

from __future__ import annotations

from collections.abc import Mapping

import sqlalchemy as sa
from alembic import op

revision = "0074_order_intent_idempotency"
down_revision = "0073_feedback_catalogue_gate"
branch_labels = None
depends_on = None

_IDEMPOTENCY_KEY_MAX_LENGTH = 64

_TABLE = sa.table(
    "order_intents",
    sa.column("intent_id", sa.BigInteger()),
    sa.column("account_id", sa.BigInteger()),
    sa.column("payload", sa.JSON()),
    sa.column("idempotency_key", sa.String(length=64)),
)


def _payload_key(payload: object) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("idempotency_key")
    if raw is None:
        return None
    key = str(raw).strip()
    if not key or len(key) > _IDEMPOTENCY_KEY_MAX_LENGTH:
        msg = "order_intents contains an invalid payload idempotency_key"
        raise RuntimeError(msg)
    return key


def upgrade() -> None:
    op.add_column(
        "order_intents",
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
    )

    bind = op.get_bind()
    seen: set[tuple[int, str]] = set()
    rows = bind.execute(
        sa.select(
            _TABLE.c.intent_id,
            _TABLE.c.account_id,
            _TABLE.c.payload,
        )
    ).mappings()
    for row in rows:
        key = _payload_key(row["payload"])
        if key is None:
            continue
        identity = (int(row["account_id"]), key)
        if identity in seen:
            msg = "order_intents contains duplicate account-scoped payload idempotency keys"
            raise RuntimeError(msg)
        seen.add(identity)
        bind.execute(
            sa.update(_TABLE)
            .where(_TABLE.c.intent_id == int(row["intent_id"]))
            .values(idempotency_key=key)
        )

    with op.batch_alter_table("order_intents") as batch_op:
        batch_op.create_check_constraint(
            "ck_order_intent_idempotency_key",
            "idempotency_key IS NULL OR length(trim(idempotency_key)) > 0",
        )
        batch_op.create_unique_constraint(
            "uq_order_intent_account_idempotency",
            ["account_id", "idempotency_key"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    retained = int(
        bind.execute(
            sa.select(sa.func.count())
            .select_from(_TABLE)
            .where(_TABLE.c.idempotency_key.is_not(None))
        ).scalar_one()
    )
    if retained:
        msg = "Cannot remove canonical order idempotency while attributed intents exist"
        raise RuntimeError(msg)

    with op.batch_alter_table("order_intents") as batch_op:
        batch_op.drop_constraint(
            "uq_order_intent_account_idempotency",
            type_="unique",
        )
        batch_op.drop_constraint(
            "ck_order_intent_idempotency_key",
            type_="check",
        )
        batch_op.drop_column("idempotency_key")
