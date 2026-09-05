"""Persist binding-owned entry cash buffers.

Revision ID: 0097_binding_entry_cash_buffer
Revises: 0096_model_prior_identity
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0097_binding_entry_cash_buffer"
down_revision = "0096_model_prior_identity"
branch_labels = None
depends_on = None

_CHECK_NAME = "ck_binding_entry_cash_buffer_bps_range"


def upgrade() -> None:
    with op.batch_alter_table("user_strategy_bindings") as batch_op:
        batch_op.add_column(sa.Column("entry_cash_buffer_bps", sa.Numeric(10, 4)))
        batch_op.create_check_constraint(
            _CHECK_NAME,
            "entry_cash_buffer_bps IS NULL OR "
            "(entry_cash_buffer_bps > 0 AND entry_cash_buffer_bps <= 1000)",
        )


def downgrade() -> None:
    configured = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM user_strategy_bindings WHERE entry_cash_buffer_bps IS NOT NULL"
        )
    )
    if int(configured or 0):
        message = "Cannot discard configured binding entry cash buffers"
        raise RuntimeError(message)
    with op.batch_alter_table("user_strategy_bindings") as batch_op:
        batch_op.drop_constraint(_CHECK_NAME, type_="check")
        batch_op.drop_column("entry_cash_buffer_bps")


__all__ = ["downgrade", "upgrade"]
