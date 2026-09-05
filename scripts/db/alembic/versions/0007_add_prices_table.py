"""Add prices table for historical OHLC data."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0007_add_prices_table"
down_revision = "0006_bigints_and_fks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prices",
        sa.Column("price_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("instr_id", sa.Integer(), sa.ForeignKey("instruments.instr_id"), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("timeframe", sa.String(length=20), nullable=False, server_default="1d"),
        sa.Column("open", sa.Numeric(20, 8), nullable=True),
        sa.Column("high", sa.Numeric(20, 8), nullable=True),
        sa.Column("low", sa.Numeric(20, 8), nullable=True),
        sa.Column("close", sa.Numeric(20, 8), nullable=False),
        sa.Column("volume", sa.Numeric(20, 8), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False, server_default="lean"),
    )
    op.create_index("ix_prices_instr_ts", "prices", ["instr_id", "ts"])
    op.create_unique_constraint(
        "uq_prices_instr_ts",
        "prices",
        ["instr_id", "ts", "timeframe", "source"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_prices_instr_ts", "prices", type_="unique")
    op.drop_index("ix_prices_instr_ts", table_name="prices")
    op.drop_table("prices")
