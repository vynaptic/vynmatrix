"""Persist binding-owned gross-exposure ceilings.

Revision ID: 0095_binding_total_exposure
Revises: 0094_backend_control_plane
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0095_binding_total_exposure"
down_revision = "0094_backend_control_plane"
branch_labels = None
depends_on = None

_DEFAULT_TOTAL_EXPOSURE = "0.50"
_CHECK_NAME = "ck_binding_max_total_exposure_pct_range"


def upgrade() -> None:
    with op.batch_alter_table("user_strategy_bindings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "max_total_exposure_pct",
                sa.Numeric(10, 4),
                nullable=False,
                server_default=_DEFAULT_TOTAL_EXPOSURE,
            )
        )
        batch_op.create_check_constraint(
            _CHECK_NAME,
            "max_total_exposure_pct > 0 AND max_total_exposure_pct <= 1",
        )


def downgrade() -> None:
    configured = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM user_strategy_bindings "
            "WHERE max_total_exposure_pct <> 0.50"
        )
    )
    if int(configured or 0):
        message = "Cannot discard non-default binding gross-exposure policy"
        raise RuntimeError(message)
    with op.batch_alter_table("user_strategy_bindings") as batch_op:
        batch_op.drop_constraint(_CHECK_NAME, type_="check")
        batch_op.drop_column("max_total_exposure_pct")


__all__ = ["downgrade", "upgrade"]
