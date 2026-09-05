"""Align high-volume IDs to BigInteger and add missing FKs."""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0006_bigints_and_fks"
down_revision = "0005_remaining_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add missing foreign keys for instrumentation
    op.create_foreign_key(
        "fk_trade_signals_instr",
        "trade_signals",
        "instruments",
        ["instr_id"],
        ["instr_id"],
    )
    op.create_foreign_key(
        "fk_decision_runs_instr",
        "decision_runs",
        "instruments",
        ["instr_id"],
        ["instr_id"],
    )
    op.create_foreign_key(
        "fk_opportunities_instr",
        "opportunities",
        "instruments",
        ["instr_id"],
        ["instr_id"],
    )
    op.create_foreign_key(
        "fk_canonical_signals_strat_ver",
        "canonical_signals",
        "strategy_versions",
        ["strat_ver_id"],
        ["strat_ver_id"],
        initially="DEFERRED",
        deferrable=True,
    )


def downgrade() -> None:
    op.drop_constraint("fk_canonical_signals_strat_ver", "canonical_signals", type_="foreignkey")
    op.drop_constraint("fk_opportunities_instr", "opportunities", type_="foreignkey")
    op.drop_constraint("fk_decision_runs_instr", "decision_runs", type_="foreignkey")
    op.drop_constraint("fk_trade_signals_instr", "trade_signals", type_="foreignkey")
