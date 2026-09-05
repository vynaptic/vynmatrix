"""Allow the '15min' evaluation horizon in signal_performance (M2 follow-up).

The sub-hour evaluation machinery (H5) added EvaluationHorizon.MIN15 = '15min' in
code, and the feedback gating routes a short-holding-period signal to {MIN15, H1}.
But the ck_signal_perf_horizon CHECK constraint was never widened to include
'15min', so the first time a scalper signal is actually gated to MIN15 (which only
started once the scalpers declared a sub-hour horizon — M2), every '15min'
signal_performance insert fails with a CheckViolation and the feedback engine goes
unhealthy. This widens the constraint to match the EvaluationHorizon enum.

Postgres only; sqlite test fixtures build from the models via create_all (which
already carries the widened constraint), so this no-ops there.

Revision ID: 0037_signal_perf_15min_horizon
Revises: 0036_dedupe_instruments
Create Date: 2026-06-29
"""

from __future__ import annotations

from alembic import op

revision = "0037_signal_perf_15min_horizon"
down_revision = "0036_dedupe_instruments"
branch_labels = None
depends_on = None

_HORIZONS_NEW = "('15min', '1h', '4h', '1d', '1w', '2w', '1m')"
_HORIZONS_OLD = "('1h', '4h', '1d', '1w', '2w', '1m')"


def _set_constraint(allowed: str) -> None:
    op.execute("ALTER TABLE signal_performance DROP CONSTRAINT IF EXISTS ck_signal_perf_horizon")
    op.execute(
        "ALTER TABLE signal_performance ADD CONSTRAINT ck_signal_perf_horizon "
        f"CHECK (evaluation_horizon IN {allowed})"
    )


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    _set_constraint(_HORIZONS_NEW)


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    # Drop any rows the widened constraint allowed but the old one forbids, else
    # re-adding the narrower constraint would fail.
    op.execute("DELETE FROM signal_performance WHERE evaluation_horizon = '15min'")
    _set_constraint(_HORIZONS_OLD)
