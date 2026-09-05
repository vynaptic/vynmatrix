"""Make feedback generation recoverable and horizon-scoped.

Parameter suggestions are scoped to the evaluation horizon that triggered
them.  A partial unique index prevents concurrent feedback workers from
creating duplicate pending work for the same strategy/instrument/horizon.

Revision ID: 0072_feedback_runtime_integrity
Revises: 0071_exact_broker_fill
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0072_feedback_runtime_integrity"
down_revision = "0071_exact_broker_fill"
branch_labels = None
depends_on = None

_VALID_HORIZONS = "'15min', '1h', '4h', '1d', '1w', '2w', '1m'"


def _backfill_feedback_horizons() -> None:
    """Copy horizon only from one exact linked tracker or abort the migration."""

    bind = op.get_bind()
    exact_tracker_match = """
        tracker.feedback_id = strategy_parameter_feedback.feedback_id
        AND tracker.strategy_id = strategy_parameter_feedback.strategy_id
        AND tracker.instr_id = strategy_parameter_feedback.instr_id
        AND (
            tracker.strat_ver_id = strategy_parameter_feedback.strat_ver_id
            OR (
                tracker.strat_ver_id IS NULL
                AND strategy_parameter_feedback.strat_ver_id IS NULL
            )
        )
    """
    unresolved = int(
        bind.execute(
            sa.text(
                f"""
                SELECT COUNT(*)
                FROM strategy_parameter_feedback
                WHERE (
                    SELECT COUNT(*)
                    FROM strategy_consecutive_wrong_tracker AS tracker
                    WHERE {exact_tracker_match}
                ) <> 1
                """
            )
        ).scalar_one()
    )
    if unresolved:
        message = (
            "Cannot add feedback horizon: "
            f"{unresolved} suggestion(s) lack exactly one identity-matched tracker"
        )
        raise RuntimeError(message)

    bind.execute(
        sa.text(
            f"""
            UPDATE strategy_parameter_feedback
            SET horizon = (
                SELECT tracker.horizon
                FROM strategy_consecutive_wrong_tracker AS tracker
                WHERE {exact_tracker_match}
            )
            """
        )
    )


def upgrade() -> None:
    with op.batch_alter_table("strategy_parameter_feedback") as batch:
        batch.add_column(
            sa.Column(
                "horizon",
                sa.String(length=10),
                nullable=True,
            )
        )

    _backfill_feedback_horizons()

    with op.batch_alter_table("strategy_parameter_feedback") as batch:
        batch.alter_column(
            "horizon",
            existing_type=sa.String(length=10),
            nullable=False,
        )
        batch.create_check_constraint(
            "ck_param_feedback_horizon",
            f"horizon IN ({_VALID_HORIZONS})",
        )

    # Every runtime writer already supplies an explicit evaluation horizon.
    # Drop the old tracker fallback too so missing provenance fails at write
    # time instead of silently becoming a daily evaluation.
    with op.batch_alter_table("strategy_consecutive_wrong_tracker") as batch:
        batch.alter_column(
            "horizon",
            existing_type=sa.String(length=10),
            existing_nullable=False,
            server_default=None,
        )

    op.create_index(
        "uq_pending_feedback_scope",
        "strategy_parameter_feedback",
        ["strategy_id", "instr_id", "horizon"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_pending_feedback_scope",
        table_name="strategy_parameter_feedback",
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )
    with op.batch_alter_table("strategy_consecutive_wrong_tracker") as batch:
        batch.alter_column(
            "horizon",
            existing_type=sa.String(length=10),
            existing_nullable=False,
            server_default="1d",
        )
    with op.batch_alter_table("strategy_parameter_feedback") as batch:
        batch.drop_constraint("ck_param_feedback_horizon", type_="check")
        batch.drop_column("horizon")
