"""Make feedback writes unique and decision attribution exact.

Revision ID: 0078_feedback_exact_lineage
Revises: 0077_execution_safety
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0078_feedback_exact_lineage"
down_revision = "0077_execution_safety"
branch_labels = None
depends_on = None

_FEEDBACK_LINEAGE_TABLES = (
    "execution_decision_logs",
    "order_intents",
    "orders",
    "executions",
)


def _assert_no_duplicate_keys(
    bind: Any,
    *,
    table: str,
    columns: Sequence[str],
    where: str | None = None,
) -> None:
    group_by = ", ".join(columns)
    where_clause = f"WHERE {where}" if where else ""
    duplicate = bind.execute(
        sa.text(
            f"""
            SELECT {group_by}, COUNT(*) AS duplicate_count
              FROM {table}
              {where_clause}
             GROUP BY {group_by}
            HAVING COUNT(*) > 1
             LIMIT 1
            """
        )
    ).mappings().first()
    if duplicate is not None:
        msg = f"Cannot enforce {table} natural identity; duplicate key exists: {dict(duplicate)}"
        raise RuntimeError(msg)


def _backfill_decision_lineage(bind: Any) -> None:
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                """
                UPDATE execution_decision_logs AS decision
                   SET broker_account_id = binding.broker_account_id
                  FROM user_strategy_bindings AS binding
                 WHERE decision.broker_account_id IS NULL
                   AND decision.binding_id = binding.binding_id
                """
            )
        )
        bind.execute(
            sa.text(
                """
                UPDATE execution_decision_logs AS decision
                   SET canonical_signal_id = execution_log.canonical_signal_id
                  FROM execution_logs AS execution_log
                 WHERE decision.canonical_signal_id IS NULL
                   AND decision.execution_id = execution_log.log_id
                   AND execution_log.canonical_signal_id IS NOT NULL
                """
            )
        )
        bind.execute(
            sa.text(
                """
                UPDATE execution_decision_logs AS decision
                   SET canonical_signal_id = signal.signal_id
                  FROM canonical_signals AS signal
                 WHERE decision.canonical_signal_id IS NULL
                   AND decision.signal_id = signal.external_signal_id
                """
            )
        )
    else:
        bind.execute(
            sa.text(
                """
                UPDATE execution_decision_logs
                   SET broker_account_id = (
                       SELECT binding.broker_account_id
                         FROM user_strategy_bindings AS binding
                        WHERE binding.binding_id = execution_decision_logs.binding_id
                   )
                 WHERE broker_account_id IS NULL
                   AND binding_id IS NOT NULL
                """
            )
        )
        bind.execute(
            sa.text(
                """
                UPDATE execution_decision_logs
                   SET canonical_signal_id = (
                       SELECT execution_log.canonical_signal_id
                         FROM execution_logs AS execution_log
                        WHERE execution_log.log_id = execution_decision_logs.execution_id
                   )
                 WHERE canonical_signal_id IS NULL
                   AND execution_id IS NOT NULL
                """
            )
        )
        bind.execute(
            sa.text(
                """
                UPDATE execution_decision_logs
                   SET canonical_signal_id = (
                       SELECT signal.signal_id
                         FROM canonical_signals AS signal
                        WHERE signal.external_signal_id = execution_decision_logs.signal_id
                   )
                 WHERE canonical_signal_id IS NULL
                   AND signal_id IS NOT NULL
                """
            )
        )

    bind.execute(
        sa.text(
            """
            UPDATE execution_decision_logs
               SET lineage_schema_version = 'v1'
             WHERE canonical_signal_id IS NOT NULL
               AND broker_account_id IS NOT NULL
            """
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    _assert_no_duplicate_keys(
        bind,
        table="signal_performance",
        columns=("signal_id", "evaluation_horizon"),
    )
    _assert_no_duplicate_keys(
        bind,
        table="mode_performance",
        columns=(
            "account_id",
            "strategy_id",
            "instr_id",
            "execution_mode",
            "horizon",
        ),
        where="instr_id IS NOT NULL",
    )
    _assert_no_duplicate_keys(
        bind,
        table="mode_performance",
        columns=(
            "account_id",
            "strategy_id",
            "sector_id",
            "execution_mode",
            "horizon",
        ),
        where="instr_id IS NULL AND sector_id IS NOT NULL",
    )
    _assert_no_duplicate_keys(
        bind,
        table="mode_performance",
        columns=(
            "account_id",
            "strategy_id",
            "asset_class",
            "execution_mode",
            "horizon",
        ),
        where="instr_id IS NULL AND sector_id IS NULL AND asset_class IS NOT NULL",
    )

    with op.batch_alter_table("execution_decision_logs") as batch:
        batch.add_column(sa.Column("canonical_signal_id", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("broker_account_id", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("lineage_schema_version", sa.String(length=10), nullable=True))

    _backfill_decision_lineage(bind)

    with op.batch_alter_table("execution_decision_logs") as batch:
        batch.create_foreign_key(
            "fk_execution_decision_canonical_signal",
            "canonical_signals",
            ["canonical_signal_id"],
            ["signal_id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_execution_decision_account_owner",
            "linked_broker_accounts",
            ["broker_account_id", "user_id"],
            ["account_id", "user_id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_execution_decision_exact_lineage",
            "lineage_schema_version IS NULL OR "
            "(lineage_schema_version = 'v1' "
            "AND canonical_signal_id IS NOT NULL "
            "AND broker_account_id IS NOT NULL)",
        )
        batch.create_index(
            "ix_execution_decision_signal_account",
            ["canonical_signal_id", "broker_account_id"],
            unique=False,
        )

    with op.batch_alter_table("signal_performance") as batch:
        batch.create_unique_constraint(
            "uq_signal_performance_signal_horizon",
            ["signal_id", "evaluation_horizon"],
        )
    with op.batch_alter_table("mode_performance") as batch:
        batch.create_check_constraint(
            "ck_mode_performance_scope_required",
            "instr_id IS NOT NULL OR sector_id IS NOT NULL OR asset_class IS NOT NULL",
        )
    op.create_index(
        "uq_mode_performance_instrument_scope",
        "mode_performance",
        ["account_id", "strategy_id", "instr_id", "execution_mode", "horizon"],
        unique=True,
        postgresql_where=sa.text("instr_id IS NOT NULL"),
        sqlite_where=sa.text("instr_id IS NOT NULL"),
    )
    op.create_index(
        "uq_mode_performance_sector_scope",
        "mode_performance",
        ["account_id", "strategy_id", "sector_id", "execution_mode", "horizon"],
        unique=True,
        postgresql_where=sa.text("instr_id IS NULL AND sector_id IS NOT NULL"),
        sqlite_where=sa.text("instr_id IS NULL AND sector_id IS NOT NULL"),
    )
    op.create_index(
        "uq_mode_performance_asset_class_scope",
        "mode_performance",
        ["account_id", "strategy_id", "asset_class", "execution_mode", "horizon"],
        unique=True,
        postgresql_where=sa.text(
            "instr_id IS NULL AND sector_id IS NULL AND asset_class IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "instr_id IS NULL AND sector_id IS NULL AND asset_class IS NOT NULL"
        ),
    )
    if bind.dialect.name == "postgresql":
        for table in _FEEDBACK_LINEAGE_TABLES:
            op.execute(f"GRANT SELECT ON TABLE public.{table} TO vm_feedback")
            op.execute(
                f"CREATE POLICY {table}_feedback_select ON {table} "
                "FOR SELECT TO vm_feedback USING (true)"
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in _FEEDBACK_LINEAGE_TABLES:
            op.execute(
                f"DROP POLICY IF EXISTS {table}_feedback_select ON {table}"
            )
            op.execute(
                f"REVOKE SELECT ON TABLE public.{table} FROM vm_feedback"
            )
    op.drop_index(
        "uq_mode_performance_asset_class_scope",
        table_name="mode_performance",
    )
    op.drop_index(
        "uq_mode_performance_sector_scope",
        table_name="mode_performance",
    )
    op.drop_index(
        "uq_mode_performance_instrument_scope",
        table_name="mode_performance",
    )
    with op.batch_alter_table("mode_performance") as batch:
        batch.drop_constraint("ck_mode_performance_scope_required", type_="check")
    with op.batch_alter_table("signal_performance") as batch:
        batch.drop_constraint(
            "uq_signal_performance_signal_horizon",
            type_="unique",
        )
    with op.batch_alter_table("execution_decision_logs") as batch:
        batch.drop_index("ix_execution_decision_signal_account")
        batch.drop_constraint("ck_execution_decision_exact_lineage", type_="check")
        batch.drop_constraint("fk_execution_decision_account_owner", type_="foreignkey")
        batch.drop_constraint("fk_execution_decision_canonical_signal", type_="foreignkey")
        batch.drop_column("lineage_schema_version")
        batch.drop_column("broker_account_id")
        batch.drop_column("canonical_signal_id")
