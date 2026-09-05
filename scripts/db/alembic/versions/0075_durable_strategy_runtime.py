"""Add the durable indicator strategy runtime journal.

Revision ID: 0075_durable_strategy_runtime
Revises: 0074_order_intent_idempotency
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0075_durable_strategy_runtime"
down_revision = "0074_order_intent_idempotency"
branch_labels = None
depends_on = None

_OUTBOX_ROLES: dict[str, tuple[str, ...]] = {
    "vm_scoring": ("SELECT", "INSERT", "UPDATE"),
    "vm_execution": ("SELECT", "INSERT"),
    "vm_feedback": ("SELECT", "INSERT"),
}
_INDICATOR = "vm_indicator"
_SIGNAL_TOPIC = "signals.submit"


def _create_policy(
    table: str,
    *,
    role: str,
    command: str,
    predicate: str,
) -> None:
    name = f"{table}_{role.removeprefix('vm_')}_{command.lower()}"
    if command == "SELECT":
        clause = f"FOR SELECT TO {role} USING ({predicate})"
    elif command == "INSERT":
        clause = f"FOR INSERT TO {role} WITH CHECK ({predicate})"
    elif command == "UPDATE":
        clause = f"FOR UPDATE TO {role} USING ({predicate}) WITH CHECK ({predicate})"
    else:
        msg = f"Unsupported policy command: {command}"
        raise ValueError(msg)
    op.execute(f"CREATE POLICY {name} ON {table} {clause}")


def _enable_postgresql_runtime_access() -> None:
    for table in ("strategy_runtime_states", "strategy_decisions"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON TABLE public.strategy_runtime_states "
        f"TO {_INDICATOR}"
    )
    op.execute(
        "GRANT SELECT, INSERT ON TABLE public.strategy_decisions "
        f"TO {_INDICATOR}"
    )
    for command in ("SELECT", "INSERT", "UPDATE"):
        _create_policy(
            "strategy_runtime_states",
            role=_INDICATOR,
            command=command,
            predicate="true",
        )
    for command in ("SELECT", "INSERT"):
        _create_policy(
            "strategy_decisions",
            role=_INDICATOR,
            command=command,
            predicate="true",
        )

    # Existing scoring/execution/feedback grants predate RLS on this table.
    # Preserve their exact command surfaces while restricting the indicator
    # role to its strategy-ingestion topic.
    op.execute("ALTER TABLE outbox_events ENABLE ROW LEVEL SECURITY")
    for role, commands in _OUTBOX_ROLES.items():
        for command in commands:
            _create_policy(
                "outbox_events",
                role=role,
                command=command,
                predicate="true",
            )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON TABLE public.outbox_events "
        f"TO {_INDICATOR}"
    )
    topic_predicate = f"topic = '{_SIGNAL_TOPIC}'"
    for command in ("SELECT", "INSERT", "UPDATE"):
        _create_policy(
            "outbox_events",
            role=_INDICATOR,
            command=command,
            predicate=topic_predicate,
        )


def upgrade() -> None:
    op.create_table(
        "strategy_runtime_states",
        sa.Column("worker_id", sa.String(length=100), nullable=False),
        sa.Column("strategy_id", sa.String(length=50), nullable=False),
        sa.Column("strategy_version", sa.String(length=20), nullable=False),
        sa.Column("symbols", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("timeframe", sa.String(length=20), nullable=False),
        sa.Column("consolidation_minutes", sa.Integer(), nullable=False),
        sa.Column("config_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("state_schema_version", sa.Integer(), nullable=False),
        sa.Column("state_payload", sa.JSON(), nullable=False),
        sa.Column("last_source_symbol", sa.String(length=50), nullable=True),
        sa.Column("last_source_price_id", sa.BigInteger(), nullable=True),
        sa.Column("last_source_ts", sa.DateTime(), nullable=True),
        sa.Column("last_source_content_revision", sa.BigInteger(), nullable=True),
        sa.Column(
            "generation",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "length(trim(strategy_version)) > 0",
            name="ck_strategy_runtime_version_nonempty",
        ),
        sa.CheckConstraint(
            "length(config_fingerprint) = 64",
            name="ck_strategy_runtime_config_fingerprint",
        ),
        sa.CheckConstraint(
            "consolidation_minutes >= 0",
            name="ck_strategy_runtime_consolidation_nonnegative",
        ),
        sa.CheckConstraint(
            "state_schema_version > 0",
            name="ck_strategy_runtime_state_schema_positive",
        ),
        sa.CheckConstraint(
            "generation > 0",
            name="ck_strategy_runtime_generation_positive",
        ),
        sa.CheckConstraint(
            "last_source_content_revision IS NULL OR last_source_content_revision > 0",
            name="ck_strategy_runtime_source_revision_positive",
        ),
        sa.ForeignKeyConstraint(
            ["last_source_price_id"],
            ["prices.price_id"],
            name="fk_strategy_runtime_last_source_price",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["strategies.strategy_id"],
            name="fk_strategy_runtime_strategy",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index(
        "ix_strategy_runtime_states_strategy_id",
        "strategy_runtime_states",
        ["strategy_id"],
    )
    op.create_index(
        "ix_strategy_runtime_identity",
        "strategy_runtime_states",
        ["strategy_id", "strategy_version", "source", "timeframe"],
    )

    op.create_table(
        "strategy_decisions",
        sa.Column("decision_key", sa.String(length=64), nullable=False),
        sa.Column("worker_id", sa.String(length=100), nullable=False),
        sa.Column("strategy_id", sa.String(length=50), nullable=False),
        sa.Column("strategy_version", sa.String(length=20), nullable=False),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("timeframe", sa.String(length=20), nullable=False),
        sa.Column("consolidation_minutes", sa.Integer(), nullable=False),
        sa.Column("strategy_bar_ts", sa.DateTime(), nullable=False),
        sa.Column("source_price_id", sa.BigInteger(), nullable=False),
        sa.Column("source_content_revision", sa.BigInteger(), nullable=False),
        sa.Column("state_schema_version", sa.Integer(), nullable=False),
        sa.Column("signal_envelopes", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "length(decision_key) = 64",
            name="ck_strategy_decision_key",
        ),
        sa.CheckConstraint(
            "length(trim(strategy_version)) > 0",
            name="ck_strategy_decision_version_nonempty",
        ),
        sa.CheckConstraint(
            "consolidation_minutes >= 0",
            name="ck_strategy_decision_consolidation_nonnegative",
        ),
        sa.CheckConstraint(
            "source_content_revision > 0",
            name="ck_strategy_decision_source_revision_positive",
        ),
        sa.CheckConstraint(
            "state_schema_version > 0",
            name="ck_strategy_decision_state_schema_positive",
        ),
        sa.ForeignKeyConstraint(
            ["source_price_id"],
            ["prices.price_id"],
            name="fk_strategy_decision_source_price",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["strategies.strategy_id"],
            name="fk_strategy_decision_strategy",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["worker_id"],
            ["strategy_runtime_states.worker_id"],
            name="fk_strategy_decision_runtime",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("decision_key"),
        sa.UniqueConstraint(
            "worker_id",
            "symbol",
            "strategy_bar_ts",
            "source_content_revision",
            name="uq_strategy_decision_bar_revision",
        ),
    )
    op.create_index(
        "ix_strategy_decisions_worker_id",
        "strategy_decisions",
        ["worker_id"],
    )
    op.create_index(
        "ix_strategy_decision_source",
        "strategy_decisions",
        ["source_price_id", "source_content_revision"],
    )

    if op.get_bind().dialect.name == "postgresql":
        _enable_postgresql_runtime_access()


def downgrade() -> None:
    bind = op.get_bind()
    retained_decisions = int(
        bind.execute(sa.text("SELECT count(*) FROM strategy_decisions")).scalar_one()
    )
    retained_states = int(
        bind.execute(sa.text("SELECT count(*) FROM strategy_runtime_states")).scalar_one()
    )
    if retained_decisions or retained_states:
        msg = (
            "Cannot remove durable strategy runtime history while "
            f"{retained_states} state row(s) and {retained_decisions} decision row(s) exist"
        )
        raise RuntimeError(msg)

    if bind.dialect.name == "postgresql":
        for role, commands in {
            **_OUTBOX_ROLES,
            _INDICATOR: ("SELECT", "INSERT", "UPDATE"),
        }.items():
            for command in commands:
                name = f"outbox_events_{role.removeprefix('vm_')}_{command.lower()}"
                op.execute(f"DROP POLICY IF EXISTS {name} ON outbox_events")
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.outbox_events FROM {_INDICATOR}")
        op.execute("ALTER TABLE outbox_events DISABLE ROW LEVEL SECURITY")

        for table in ("strategy_decisions", "strategy_runtime_states"):
            for command in ("SELECT", "INSERT", "UPDATE"):
                name = f"{table}_{_INDICATOR.removeprefix('vm_')}_{command.lower()}"
                op.execute(f"DROP POLICY IF EXISTS {name} ON {table}")
            op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.{table} FROM {_INDICATOR}")

    op.drop_index("ix_strategy_decision_source", table_name="strategy_decisions")
    op.drop_index("ix_strategy_decisions_worker_id", table_name="strategy_decisions")
    op.drop_table("strategy_decisions")
    op.drop_index("ix_strategy_runtime_identity", table_name="strategy_runtime_states")
    op.drop_index("ix_strategy_runtime_states_strategy_id", table_name="strategy_runtime_states")
    op.drop_table("strategy_runtime_states")
