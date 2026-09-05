"""Require signal lineage and a stable identity for canonical executions.

The platform has no manual-order path: every broker submission originates from
a persisted canonical strategy signal.  The canonical OMS therefore fails
closed when an order intent lacks that signal or an execution lacks a stable
identity. Revision 0071 subsequently narrows this predecessor contract to exact
venue-reported fills and rejects cumulative status synthesis and fee-only rows.

Revision ID: 0064_fill_lineage
Revises: 0063_required_signal_identity
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0064_fill_lineage"
down_revision = "0063_required_signal_identity"
branch_labels = None
depends_on = None


def _assert_complete_lineage() -> None:
    bind = op.get_bind()
    missing_signal_count = int(
        bind.execute(
            sa.text(
                """
                SELECT count(*)
                FROM order_intents
                WHERE canonical_signal_id IS NULL
                """
            )
        ).scalar_one()
    )
    missing_trade_count = int(
        bind.execute(
            sa.text(
                """
                SELECT count(*)
                FROM executions
                WHERE trade_id IS NULL OR trim(trade_id) = ''
                """
            )
        ).scalar_one()
    )
    if missing_signal_count or missing_trade_count:
        msg = (
            "Canonical fill-lineage migration found unattributed historical rows "
            f"(order_intents.canonical_signal_id={missing_signal_count}, "
            f"executions.trade_id={missing_trade_count}). "
            "Reconcile or remove them before upgrade."
        )
        raise RuntimeError(msg)


def upgrade() -> None:
    _assert_complete_lineage()
    with op.batch_alter_table("order_intents") as batch:
        batch.alter_column(
            "canonical_signal_id",
            existing_type=sa.BigInteger(),
            nullable=False,
        )
    with op.batch_alter_table("executions") as batch:
        batch.alter_column(
            "trade_id",
            existing_type=sa.String(length=255),
            nullable=False,
        )
        batch.create_check_constraint(
            "ck_execution_trade_identity",
            "length(trim(trade_id)) > 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("executions") as batch:
        batch.drop_constraint(
            "ck_execution_trade_identity",
            type_="check",
        )
        batch.alter_column(
            "trade_id",
            existing_type=sa.String(length=255),
            nullable=True,
        )
    with op.batch_alter_table("order_intents") as batch:
        batch.alter_column(
            "canonical_signal_id",
            existing_type=sa.BigInteger(),
            nullable=True,
        )
