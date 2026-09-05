"""Retire dormant strategy-engine and experimental runtime metadata.

All production strategy versions execute through the indicator runtime. The
legacy engine discriminator and ML, RL, and agent-graph columns have no reader
or writer in the active platform. Existing noncanonical data is not safe to
reinterpret, so the upgrade refuses to remove the columns until every row is a
plain production-runtime row with no dormant metadata.

Revision ID: 0065_retire_strategy_metadata
Revises: 0064_fill_lineage
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "0065_retire_strategy_metadata"
down_revision = "0064_fill_lineage"
branch_labels = None
depends_on = None


def _lock_strategy_versions(bind: Connection) -> None:
    if bind.dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.execute("LOCK TABLE strategy_versions IN ACCESS EXCLUSIVE MODE")


def _assert_no_dormant_metadata(bind: Connection) -> None:
    invalid_rows = int(
        bind.execute(
            sa.text(
                """
                SELECT count(*)
                FROM strategy_versions
                WHERE engine_kind IS NULL
                   OR engine_kind <> 'python_service'
                   OR mlflow_run_id IS NOT NULL
                   OR rl_policy_uri IS NOT NULL
                   OR agent_graph IS NOT NULL
                """
            )
        ).scalar_one()
    )
    if invalid_rows:
        msg = (
            "Dormant strategy-metadata retirement found "
            f"{invalid_rows} noncanonical strategy_versions row(s). "
            "Remove or explicitly reconcile non-python_service versions and "
            "ML/RL/agent metadata before upgrading; this migration will not "
            "reinterpret production history."
        )
        raise RuntimeError(msg)


def _drop_dormant_columns(bind: Connection) -> None:
    if bind.dialect.name == "postgresql":
        op.drop_constraint("ck_engine_kind", "strategy_versions", type_="check")
        op.drop_column("strategy_versions", "agent_graph")
        op.drop_column("strategy_versions", "rl_policy_uri")
        op.drop_column("strategy_versions", "mlflow_run_id")
        op.drop_column("strategy_versions", "engine_kind")
        return
    constraint_names: tuple[str, ...] = ("ck_engine_kind",)
    if bind.dialect.name == "sqlite":
        # SQLite's inspector can lose a named CHECK's name when the original
        # DDL wraps ``CONSTRAINT <name>`` and ``CHECK`` onto separate lines.
        # Alembic omits such reflected unnamed checks during table recreation,
        # while named checks must be removed explicitly before their referenced
        # column is dropped.
        constraint_names = tuple(
            str(constraint["name"])
            for constraint in sa.inspect(bind).get_check_constraints("strategy_versions")
            if constraint.get("name")
            and "engine_kind" in str(constraint.get("sqltext", "")).lower()
        )
    with op.batch_alter_table("strategy_versions") as batch:
        for constraint_name in constraint_names:
            batch.drop_constraint(constraint_name, type_="check")
        batch.drop_column("agent_graph")
        batch.drop_column("rl_policy_uri")
        batch.drop_column("mlflow_run_id")
        batch.drop_column("engine_kind")


def _restore_dormant_columns(bind: Connection) -> None:
    engine_kind = sa.Column(
        "engine_kind",
        sa.String(length=50),
        nullable=False,
        server_default=sa.text("'python_service'"),
    )
    mlflow_run_id = sa.Column("mlflow_run_id", sa.String(length=100))
    rl_policy_uri = sa.Column("rl_policy_uri", sa.String(length=500))
    agent_graph = sa.Column("agent_graph", sa.JSON())
    if bind.dialect.name == "postgresql":
        op.add_column("strategy_versions", engine_kind)
        op.add_column("strategy_versions", mlflow_run_id)
        op.add_column("strategy_versions", rl_policy_uri)
        op.add_column("strategy_versions", agent_graph)
        op.alter_column(
            "strategy_versions",
            "engine_kind",
            existing_type=sa.String(length=50),
            server_default=None,
        )
        op.create_check_constraint(
            "ck_engine_kind",
            "strategy_versions",
            "engine_kind IN ('python_service', 'spark', 'ray')",
        )
        return
    with op.batch_alter_table("strategy_versions") as batch:
        batch.add_column(engine_kind)
        batch.add_column(mlflow_run_id)
        batch.add_column(rl_policy_uri)
        batch.add_column(agent_graph)

    with op.batch_alter_table("strategy_versions") as batch:
        batch.alter_column(
            "engine_kind",
            existing_type=sa.String(length=50),
            server_default=None,
        )
        batch.create_check_constraint(
            "ck_engine_kind",
            "engine_kind IN ('python_service', 'spark', 'ray')",
        )


def upgrade() -> None:
    bind = op.get_bind()
    _lock_strategy_versions(bind)
    _assert_no_dormant_metadata(bind)
    _drop_dormant_columns(bind)


def downgrade() -> None:
    _restore_dormant_columns(op.get_bind())
