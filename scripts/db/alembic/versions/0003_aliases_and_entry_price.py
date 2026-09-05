"""Add instrument_aliases table and canonical_signals table."""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0003_aliases_and_entry_price"
down_revision = "0002_core_app_schema"
branch_labels = None
depends_on = None


def upgrade():
    # Create canonical_signals table (was missing from earlier migrations)
    # Note: FK types match those in migration 0002
    op.create_table(
        "canonical_signals",
        sa.Column("signal_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "strategy_id",
            sa.String(length=50),
            sa.ForeignKey("strategies.strategy_id"),
            nullable=False,
        ),
        sa.Column("strat_ver_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "instr_id",
            sa.Integer(),
            sa.ForeignKey("instruments.instr_id"),
            nullable=False,
        ),
        sa.Column("sector_id", sa.Integer(), sa.ForeignKey("sectors.sector_id"), nullable=True),
        # Signal data
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.Numeric(10, 4), nullable=True),
        sa.Column("raw_score", sa.Numeric(10, 4), nullable=True),
        sa.Column("direction", sa.String(length=10), nullable=True),
        sa.Column("entry_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("expected_return", sa.Numeric(20, 8), nullable=True),
        sa.Column("predicted_risk", sa.Numeric(20, 8), nullable=True),
        sa.Column("horizon_seconds", sa.BigInteger(), nullable=True),
        # Metadata
        sa.Column("features", sa.JSON(), nullable=True),
        sa.Column("signal_meta", sa.JSON(), nullable=True),
        sa.Column("source_runner", sa.String(length=50), nullable=True),
        # Timing
        sa.Column("ts", sa.DateTime(), nullable=False, index=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "action IN ('long', 'short', 'flat', 'hold', 'open_spread', 'close_spread')",
            name="ck_canonical_action",
        ),
        sa.CheckConstraint("direction IN ('long', 'short', 'flat')", name="ck_canonical_direction"),
    )
    op.create_index("ix_canonical_signal_instr_ts", "canonical_signals", ["instr_id", "ts"])
    op.create_index("ix_canonical_signal_strategy_ts", "canonical_signals", ["strategy_id", "ts"])

    # Create instrument_aliases table
    op.create_table(
        "instrument_aliases",
        sa.Column("alias_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "instr_id",
            sa.Integer(),
            sa.ForeignKey("instruments.instr_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("alias", sa.String(length=100), nullable=False, unique=True),
        sa.Column("source", sa.String(length=50), nullable=True),
    )
    op.create_index("ix_instrument_alias_alias", "instrument_aliases", ["alias"], unique=True)


def downgrade():
    op.drop_index("ix_instrument_alias_alias", table_name="instrument_aliases")
    op.drop_table("instrument_aliases")
    op.drop_index("ix_canonical_signal_strategy_ts", table_name="canonical_signals")
    op.drop_index("ix_canonical_signal_instr_ts", table_name="canonical_signals")
    op.drop_table("canonical_signals")
