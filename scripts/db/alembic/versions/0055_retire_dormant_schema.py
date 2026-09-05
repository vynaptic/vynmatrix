"""Retire the unused decision and opportunity schema.

The production path is canonical_signals -> scores -> execution_decision_logs
-> outbox_events -> order/fill persistence.  The tables removed here have no
production reader or writer.  The migration refuses to discard rows so an
unexpected historical-data dependency is surfaced before any schema mutation.

Revision ID: 0055_retire_dormant_schema
Revises: 0054_observed_fx_rates
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055_retire_dormant_schema"
down_revision = "0054_observed_fx_rates"
branch_labels = None
depends_on = None

_RETIRED_TABLES = (
    "trade_signals",
    "decision_runs",
    "decision_run_members",
    "opportunities",
    "opportunity_methods",
    "opportunity_explanations",
    "user_opportunity_subscriptions",
    "opp_sub_execution_bindings",
    "strategy_coverage",
)

_DROP_ORDER = (
    "opp_sub_execution_bindings",
    "user_opportunity_subscriptions",
    "opportunity_explanations",
    "opportunity_methods",
    "opportunities",
    "decision_run_members",
    "decision_runs",
    "trade_signals",
    "strategy_coverage",
)


def _assert_retired_state_is_empty() -> None:
    """Abort before DDL when dormant state unexpectedly contains data."""
    bind = op.get_bind()
    populated = {
        table: int(bind.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one())
        for table in _RETIRED_TABLES
    }
    populated = {table: count for table, count in populated.items() if count}
    linked_intents = int(
        bind.execute(
            sa.text("SELECT count(*) FROM order_intents WHERE opp_id IS NOT NULL")
        ).scalar_one()
    )
    if linked_intents:
        populated["order_intents.opp_id"] = linked_intents
    if populated:
        details = ", ".join(f"{table}={count}" for table, count in sorted(populated.items()))
        msg = (
            "Dormant decision-schema retirement blocked because rows still exist: "
            f"{details}. Export or explicitly remove the historical rows before retrying."
        )
        raise RuntimeError(msg)


def _drop_order_intent_opportunity_reference() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # SQLite stores inline foreign keys without a useful constraint name.
        # Batch mode rebuilds the table without the removed column and FK.
        with op.batch_alter_table("order_intents") as batch_op:
            batch_op.drop_column("opp_id")
        return

    op.drop_constraint(
        "order_intents_opp_id_fkey",
        "order_intents",
        type_="foreignkey",
    )
    op.drop_column("order_intents", "opp_id")


def upgrade() -> None:
    _assert_retired_state_is_empty()
    _drop_order_intent_opportunity_reference()
    for table in _DROP_ORDER:
        # Deliberately no CASCADE: an uninventoryed dependency must block the
        # migration instead of being deleted implicitly.
        op.drop_table(table)


def _restore_strategy_coverage() -> None:
    op.create_table(
        "strategy_coverage",
        sa.Column("strat_cov_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "strat_ver_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "strategy_versions.strat_ver_id",
                name="strategy_coverage_strat_ver_id_fkey",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("asset_class", sa.String(length=20), nullable=False),
        sa.Column("horizons", sa.JSON(), nullable=False),
        sa.Column("methods", sa.JSON(), nullable=False),
        sa.Column("instruments", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "asset_class IN ('crypto', 'equity', 'futures', 'options', 'fx')",
            name="ck_coverage_asset_class",
        ),
    )


def _restore_decision_tables() -> None:
    op.create_table(
        "trade_signals",
        sa.Column("signal_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "strat_ver_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "strategy_versions.strat_ver_id",
                name="trade_signals_strat_ver_id_fkey",
            ),
            nullable=False,
        ),
        sa.Column(
            "instr_id",
            sa.Integer(),
            sa.ForeignKey("instruments.instr_id", name="fk_trade_signals_instr"),
            nullable=False,
        ),
        sa.Column("horizon", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False, index=True),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("score", sa.Numeric(10, 4), nullable=True),
        sa.Column("features", sa.JSON(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "horizon IN ('intraday', 'swing', 'long_term')",
            name="ck_signal_horizon",
        ),
        sa.CheckConstraint(
            "action IN ('long', 'short', 'flat', 'open_spread', 'close_spread')",
            name="ck_signal_action",
        ),
    )
    op.create_index("ix_signal_instr_ts", "trade_signals", ["instr_id", "ts"])

    op.create_table(
        "decision_runs",
        sa.Column("run_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("asset_class", sa.String(length=20), nullable=False),
        sa.Column(
            "instr_id",
            sa.Integer(),
            sa.ForeignKey("instruments.instr_id", name="fk_decision_runs_instr"),
            nullable=False,
        ),
        sa.Column("horizon", sa.String(length=20), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=False),
        sa.Column("method", sa.String(length=50), nullable=False),
        sa.Column("quorum", sa.Integer(), nullable=True),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("consensus_score", sa.Numeric(10, 4), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "asset_class IN ('crypto', 'equity', 'futures', 'options', 'fx')",
            name="ck_decision_asset_class",
        ),
        sa.CheckConstraint(
            "horizon IN ('intraday', 'swing', 'long_term')",
            name="ck_decision_horizon",
        ),
        sa.CheckConstraint(
            "decision IN ('go_long', 'go_short', 'no_trade')",
            name="ck_decision",
        ),
    )
    op.create_index("ix_decision_instr_ts", "decision_runs", ["instr_id", "decided_at"])

    op.create_table(
        "decision_run_members",
        sa.Column("member_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "decision_runs.run_id",
                name="decision_run_members_run_id_fkey",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "signal_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "trade_signals.signal_id",
                name="decision_run_members_signal_id_fkey",
            ),
            nullable=False,
        ),
        sa.Column("vote", sa.String(length=20), nullable=False),
        sa.Column("weight", sa.Numeric(10, 4), server_default="1.0"),
        sa.CheckConstraint("vote IN ('long', 'short', 'flat')", name="ck_vote"),
    )

    op.create_table(
        "opportunities",
        sa.Column("opp_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "decision_runs.run_id",
                name="opportunities_run_id_fkey",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "instr_id",
            sa.Integer(),
            sa.ForeignKey("instruments.instr_id", name="fk_opportunities_instr"),
            nullable=False,
        ),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("horizons", sa.JSON(), nullable=False),
        sa.Column("valid_from", sa.DateTime(), nullable=False),
        sa.Column("valid_to", sa.DateTime(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("consensus_score", sa.Numeric(10, 4), nullable=True),
        sa.CheckConstraint(
            "direction IN ('long', 'short')",
            name="ck_opp_direction",
        ),
    )
    op.create_index("ix_opportunity_valid", "opportunities", ["valid_from", "valid_to"])

    op.create_table(
        "opportunity_methods",
        sa.Column("opp_method_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "opp_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "opportunities.opp_id",
                name="opportunity_methods_opp_id_fkey",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("method", sa.String(length=30), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "method IN ('SPOT', 'PERP', 'FUTURES', 'OPTIONS_SINGLE', 'OPTIONS_STRATEGY')",
            name="ck_method",
        ),
    )

    op.create_table(
        "opportunity_explanations",
        sa.Column("opp_expl_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "opp_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "opportunities.opp_id",
                name="opportunity_explanations_opp_id_fkey",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("rationale_md", sa.Text(), nullable=True),
        sa.Column("top_features", sa.JSON(), nullable=True),
        sa.Column("risk_notes_md", sa.Text(), nullable=True),
    )


def _restore_opportunity_subscriptions() -> None:
    op.create_table(
        "user_opportunity_subscriptions",
        sa.Column("opp_sub_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(length=50),
            sa.ForeignKey("users.user_id", name="user_opportunity_subscriptions_user_id_fkey"),
            nullable=False,
        ),
        sa.Column("asset_class", sa.String(length=20), nullable=False),
        sa.Column("horizons", sa.JSON(), nullable=False),
        sa.Column("instruments", sa.JSON(), nullable=False),
        sa.Column("autopilot", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "approval_mode",
            sa.String(length=20),
            nullable=False,
            server_default="instant",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "approval_mode IN ('instant', 'require_approval')",
            name="ck_approval_mode",
        ),
    )

    op.create_table(
        "opp_sub_execution_bindings",
        sa.Column("binding_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "opp_sub_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "user_opportunity_subscriptions.opp_sub_id",
                name="opp_sub_execution_bindings_opp_sub_id_fkey",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "linked_broker_accounts.account_id",
                name="opp_sub_execution_bindings_account_id_fkey",
            ),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("scope", sa.String(length=50), nullable=False, server_default="all"),
        sa.Column("constraints", sa.JSON(), nullable=False),
    )


def _restore_order_intent_opportunity_reference() -> None:
    column = sa.Column("opp_id", sa.BigInteger(), nullable=True)
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("order_intents") as batch_op:
            batch_op.add_column(column)
            batch_op.create_foreign_key(
                "order_intents_opp_id_fkey",
                "opportunities",
                ["opp_id"],
                ["opp_id"],
            )
        return

    op.add_column("order_intents", column)
    op.create_foreign_key(
        "order_intents_opp_id_fkey",
        "order_intents",
        "opportunities",
        ["opp_id"],
        ["opp_id"],
    )


def downgrade() -> None:
    _restore_strategy_coverage()
    _restore_decision_tables()
    _restore_opportunity_subscriptions()
    _restore_order_intent_opportunity_reference()

    if op.get_bind().dialect.name == "postgresql":
        # At 0054 these two tenant-bearing tables had RLS enabled with no
        # runtime-service policy, making them default-deny to service roles.
        op.execute("ALTER TABLE user_opportunity_subscriptions ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE opp_sub_execution_bindings ENABLE ROW LEVEL SECURITY")
