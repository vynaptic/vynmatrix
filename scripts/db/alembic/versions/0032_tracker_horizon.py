"""Add horizon to the consecutive-wrong tracker key (FB-2).

The tracker was unique on (strategy_id, instr_id), but the feedback loop
evaluates every signal at all six horizons (1h/4h/1d/1w/2w/1m) and routed every
(signal, horizon) result into the one shared row — so the horizons corrupted
each other's consecutive-wrong counters. Make it per (strategy, instrument,
horizon): add the column (server_default backfills existing rows) and swap the
unique constraint.

Column/constraint change is a postgres-deploy concern; sqlite test fixtures
build their schema from the models via create_all, so this no-ops on sqlite.

Revision ID: 0032_tracker_horizon
Revises: 0031_add_run_id_traceability
Create Date: 2026-06-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0032_tracker_horizon"
down_revision = "0031_add_run_id_traceability"
branch_labels = None
depends_on = None

_TABLE = "strategy_consecutive_wrong_tracker"


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    op.add_column(_TABLE, sa.Column("horizon", sa.String(10), nullable=False, server_default="1d"))
    op.drop_constraint("uq_strategy_instr_tracker", _TABLE, type_="unique")
    op.create_unique_constraint(
        "uq_strategy_instr_horizon_tracker", _TABLE, ["strategy_id", "instr_id", "horizon"]
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    op.drop_constraint("uq_strategy_instr_horizon_tracker", _TABLE, type_="unique")
    op.create_unique_constraint("uq_strategy_instr_tracker", _TABLE, ["strategy_id", "instr_id"])
    op.drop_column(_TABLE, "horizon")
