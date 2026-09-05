"""Core app schema for orgs/plans/users/brokers/instruments/etc."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_core_app_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Organizations
    op.create_table(
        "orgs",
        sa.Column("org_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=255), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Plans
    op.create_table(
        "plans",
        sa.Column("plan_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("features", sa.JSON(), nullable=False),
    )

    # Users (string PK to stay compatible with lib_infrastructure models; seeds can use ints)
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(length=50), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("orgs.org_id"), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=False, unique=True),
        sa.Column("full_name", sa.String(length=255)),
        sa.Column("name", sa.String(length=100)),  # compatibility with lib_infrastructure
        sa.Column("tz", sa.String(length=64)),
        sa.Column("base_ccy", sa.String(length=10)),
        sa.Column("status", sa.String(length=20), server_default="active"),
        sa.Column("risk_profile", sa.String(length=20), server_default="moderate"),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )

    op.create_table(
        "user_roles",
        sa.Column(
            "user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), primary_key=True
        ),
        sa.Column("role", sa.String(length=50), primary_key=True),
    )

    op.create_table(
        "user_plan_subscriptions",
        sa.Column("sub_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.plan_id"), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
    )

    # Strategies (core definitions)
    op.create_table(
        "strategies",
        sa.Column("strategy_id", sa.String(length=50), primary_key=True),
        sa.Column("strategy_name", sa.String(length=255), nullable=False),
        sa.Column("asset_class", sa.String(length=50)),
        sa.Column("description", sa.String(length=500)),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )

    # Brokers
    op.create_table(
        "brokers",
        sa.Column("broker_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=True),
    )

    # Broker environments
    op.create_table(
        "broker_environments",
        sa.Column("broker_env_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("broker_id", sa.Integer(), sa.ForeignKey("brokers.broker_id"), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("region", sa.String(length=50)),
        sa.Column("base_urls", sa.JSON(), nullable=True),
        sa.Column("rate_limits", sa.JSON(), nullable=True),
    )

    # Instruments
    op.create_table(
        "instruments",
        sa.Column("instr_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("asset_class", sa.String(length=50), nullable=False),
        sa.Column("canonical", sa.String(length=100), nullable=False),
        sa.Column("exchange", sa.String(length=50)),
        sa.Column("tick_size", sa.Numeric(20, 8)),
        sa.Column("lot_size", sa.Numeric(20, 8)),
    )

    op.create_table(
        "instrument_broker_symbols",
        sa.Column(
            "instr_id", sa.Integer(), sa.ForeignKey("instruments.instr_id"), primary_key=True
        ),
        sa.Column("broker_id", sa.Integer(), sa.ForeignKey("brokers.broker_id"), primary_key=True),
        sa.Column("broker_symbol", sa.String(length=100), nullable=False),
    )

    # Sectors (hierarchical)
    op.create_table(
        "sectors",
        sa.Column("sector_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=100), unique=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("asset_class", sa.String(length=50)),
        sa.Column("parent_sector_id", sa.Integer(), sa.ForeignKey("sectors.sector_id")),
        sa.Column("description", sa.String(length=500)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "instrument_sectors",
        sa.Column(
            "instr_id", sa.Integer(), sa.ForeignKey("instruments.instr_id"), primary_key=True
        ),
        sa.Column("sector_id", sa.Integer(), sa.ForeignKey("sectors.sector_id"), primary_key=True),
        sa.Column("weight", sa.Numeric(10, 4)),
    )

    # Strategy configuration per user
    op.create_table(
        "user_strategy_configs",
        sa.Column("config_id", sa.String(length=50), primary_key=True),
        sa.Column("user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column(
            "strategy_id",
            sa.String(length=50),
            sa.ForeignKey("strategies.strategy_id"),
            nullable=False,
        ),
        sa.Column("execution_mode", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("parameters", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", "strategy_id", name="uq_user_strategy"),
    )

    # Execution logs
    op.create_table(
        "execution_logs",
        sa.Column("log_id", sa.String(length=50), primary_key=True),
        sa.Column("user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column(
            "strategy_id",
            sa.String(length=50),
            sa.ForeignKey("strategies.strategy_id"),
            nullable=False,
        ),
        sa.Column("signal_type", sa.String(length=10), nullable=False),
        sa.Column("execution_mode", sa.String(length=20), nullable=False),
        sa.Column("execution_details", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="pending"),
        sa.Column("error_message", sa.String(length=500)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Options positions
    op.create_table(
        "options_positions",
        sa.Column("position_id", sa.String(length=50), primary_key=True),
        sa.Column("user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column(
            "strategy_id",
            sa.String(length=50),
            sa.ForeignKey("strategies.strategy_id"),
            nullable=False,
        ),
        sa.Column("spread_type", sa.String(length=20), nullable=False),
        sa.Column("underlying_symbol", sa.String(length=20), nullable=False),
        sa.Column("underlying_entry_price", sa.Numeric(20, 8)),
        sa.Column("underlying_current_price", sa.Numeric(20, 8)),
        sa.Column("expiry_date", sa.Date(), nullable=False),
        sa.Column("leg1_strike", sa.Numeric(20, 8), nullable=False),
        sa.Column("leg1_option_type", sa.String(length=10), nullable=False),
        sa.Column("leg1_side", sa.String(length=10), nullable=False),
        sa.Column("leg1_quantity", sa.Integer(), nullable=False),
        sa.Column("leg1_premium", sa.Numeric(20, 8), nullable=False),
        sa.Column("leg2_strike", sa.Numeric(20, 8), nullable=False),
        sa.Column("leg2_option_type", sa.String(length=10), nullable=False),
        sa.Column("leg2_side", sa.String(length=10), nullable=False),
        sa.Column("leg2_quantity", sa.Integer(), nullable=False),
        sa.Column("leg2_premium", sa.Numeric(20, 8), nullable=False),
        sa.Column("net_debit_credit", sa.Numeric(20, 8), nullable=False),
        sa.Column("max_profit", sa.Numeric(20, 8)),
        sa.Column("max_loss", sa.Numeric(20, 8)),
        sa.Column("breakeven", sa.Numeric(20, 8)),
        sa.Column("current_pnl", sa.Numeric(20, 8)),
        sa.Column("status", sa.String(length=20), server_default="open"),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("close_reason", sa.String(length=100)),
    )

    # Option strategy templates
    op.create_table(
        "option_strategy_templates",
        sa.Column("template_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=50), unique=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=500)),
    )
    op.create_table(
        "option_strategy_template_legs",
        sa.Column("leg_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "template_id",
            sa.Integer(),
            sa.ForeignKey("option_strategy_templates.template_id"),
            nullable=False,
        ),
        sa.Column("leg_index", sa.Integer(), nullable=False),
        sa.Column("position", sa.String(length=10), nullable=False),
        sa.Column("option_right", sa.String(length=10), nullable=False),
        sa.Column("ratio", sa.Integer(), nullable=False),
        sa.Column("strike_spec", sa.JSON(), nullable=True),
        sa.Column("expiry_spec", sa.JSON(), nullable=True),
    )

    # Feature flags
    op.create_table(
        "feature_flags",
        sa.Column("flag_code", sa.String(length=50), primary_key=True),
        sa.Column("description", sa.String(length=500)),
    )
    op.create_table(
        "user_feature_flags",
        sa.Column(
            "user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), primary_key=True
        ),
        sa.Column(
            "flag_code",
            sa.String(length=50),
            sa.ForeignKey("feature_flags.flag_code"),
            primary_key=True,
        ),
        sa.Column("variant", sa.String(length=50), nullable=True),
    )

    # Scoring rules
    op.create_table(
        "scoring_rules",
        sa.Column("rule_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=100), unique=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=500)),
        sa.Column("target_type", sa.String(length=50), nullable=False),
        sa.Column("aggregation_method", sa.String(length=50)),
        sa.Column("min_quorum", sa.Integer()),
        sa.Column("decay_enabled", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("decay_half_life_hours", sa.Float()),
        sa.Column("strategy_weights", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Sizing profiles
    op.create_table(
        "sizing_profiles",
        sa.Column("profile_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("method", sa.String(length=50), nullable=False),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Risk mandates
    op.create_table(
        "risk_mandates",
        sa.Column("mandate_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_type", sa.String(length=50), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=True),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("risk_mandates")
    op.drop_table("sizing_profiles")
    op.drop_table("scoring_rules")
    op.drop_table("user_feature_flags")
    op.drop_table("feature_flags")
    op.drop_table("option_strategy_template_legs")
    op.drop_table("option_strategy_templates")
    op.drop_table("options_positions")
    op.drop_table("execution_logs")
    op.drop_table("user_strategy_configs")
    op.drop_table("instrument_sectors")
    op.drop_table("sectors")
    op.drop_table("instrument_broker_symbols")
    op.drop_table("instruments")
    op.drop_table("broker_environments")
    op.drop_table("brokers")
    op.drop_table("strategies")
    op.drop_table("user_plan_subscriptions")
    op.drop_table("user_roles")
    op.drop_table("users")
    op.drop_table("plans")
    op.drop_table("orgs")
