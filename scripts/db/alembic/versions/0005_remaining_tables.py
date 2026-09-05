"""Add remaining tables from models.py that were missing from earlier migrations.

Note: The existing migrations use different types than the models.py:
- user_id: String(50) in migrations, BigInteger in models
- strategy_id: String(50) in migrations, BigInteger in models
- broker_id: Integer in migrations, BigInteger in models
- instr_id: Integer in migrations, BigInteger in models
- sector_id: Integer in migrations, BigInteger in models

This migration uses the types from existing migrations for FK compatibility.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0005_remaining_tables"
down_revision = "0003_aliases_and_entry_price"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # =========================================================================
    # B) Legal, Consents & Suitability
    # =========================================================================

    op.create_table(
        "user_consents",
        sa.Column("consent_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("consent_code", sa.String(length=100), nullable=False),
        sa.Column("version", sa.String(length=20), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column("granted_at", sa.DateTime(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=True),
    )

    op.create_table(
        "suitability_questionnaires",
        sa.Column("questionnaire_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("version", sa.String(length=20), nullable=False),
        sa.Column("spec_json", sa.JSON(), nullable=False),
    )

    op.create_table(
        "user_suitability_responses",
        sa.Column("resp_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column(
            "questionnaire_id",
            sa.BigInteger(),
            sa.ForeignKey("suitability_questionnaires.questionnaire_id"),
            nullable=False,
        ),
        sa.Column("answers_json", sa.JSON(), nullable=False),
        sa.Column("derived_profile", sa.String(length=50), nullable=False),
        sa.Column("valid_from", sa.DateTime(), nullable=False),
        sa.Column("valid_to", sa.DateTime(), nullable=True),
    )

    # =========================================================================
    # C) User-Linked Broker Accounts
    # =========================================================================

    op.create_table(
        "linked_broker_accounts",
        sa.Column("account_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("broker_id", sa.Integer(), sa.ForeignKey("brokers.broker_id"), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("external_ref", sa.String(length=255), nullable=True),
        sa.Column("base_ccy", sa.String(length=10), server_default="USD"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="connected"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("environment IN ('paper', 'live')", name="ck_account_env"),
        sa.CheckConstraint("status IN ('connected', 'revoked', 'error')", name="ck_account_status"),
    )

    op.create_table(
        "broker_credentials",
        sa.Column("cred_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "account_id",
            sa.BigInteger(),
            sa.ForeignKey("linked_broker_accounts.account_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("secret_ref", sa.String(length=500), nullable=False),
        sa.Column("last_rotated_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'expired')", name="ck_credential_status"
        ),
    )

    # =========================================================================
    # E) Strategy Versions & Coverage
    # =========================================================================

    op.create_table(
        "strategy_versions",
        sa.Column("strat_ver_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "strategy_id",
            sa.String(length=50),
            sa.ForeignKey("strategies.strategy_id"),
            nullable=False,
        ),
        sa.Column("semver", sa.String(length=20), nullable=False),
        sa.Column("engine_kind", sa.String(length=50), nullable=False),
        sa.Column("docker_image", sa.String(length=500), nullable=True),
        sa.Column("git_repo", sa.String(length=500), nullable=True),
        sa.Column("git_commit", sa.String(length=100), nullable=True),
        sa.Column("mlflow_run_id", sa.String(length=100), nullable=True),
        sa.Column("rl_policy_uri", sa.String(length=500), nullable=True),
        sa.Column("agent_graph", sa.JSON(), nullable=True),
        sa.Column("param_schema", sa.JSON(), nullable=False),
        sa.Column("default_params", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("released_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "engine_kind IN ('lean', 'python_service', 'spark', 'ray')", name="ck_engine_kind"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'deprecated', 'pulled')", name="ck_version_status"
        ),
    )

    op.create_table(
        "strategy_coverage",
        sa.Column("strat_cov_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "strat_ver_id",
            sa.BigInteger(),
            sa.ForeignKey("strategy_versions.strat_ver_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("asset_class", sa.String(length=20), nullable=False),
        sa.Column("horizons", sa.JSON(), nullable=False),
        sa.Column("methods", sa.JSON(), nullable=False),
        sa.Column("instruments", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "asset_class IN ('crypto', 'equity', 'futures', 'options', 'fx')",
            name="ck_coverage_asset_class",
        ),
    )

    # =========================================================================
    # F) Decision Engine
    # =========================================================================

    op.create_table(
        "trade_signals",
        sa.Column("signal_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "strat_ver_id",
            sa.BigInteger(),
            sa.ForeignKey("strategy_versions.strat_ver_id"),
            nullable=False,
        ),
        sa.Column("instr_id", sa.Integer(), nullable=False),
        sa.Column("horizon", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False, index=True),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("score", sa.Numeric(10, 4), nullable=True),
        sa.Column("features", sa.JSON(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "horizon IN ('intraday', 'swing', 'long_term')", name="ck_signal_horizon"
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
        sa.Column("instr_id", sa.Integer(), nullable=False),
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
            "horizon IN ('intraday', 'swing', 'long_term')", name="ck_decision_horizon"
        ),
        sa.CheckConstraint("decision IN ('go_long', 'go_short', 'no_trade')", name="ck_decision"),
    )
    op.create_index("ix_decision_instr_ts", "decision_runs", ["instr_id", "decided_at"])

    op.create_table(
        "decision_run_members",
        sa.Column("member_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.BigInteger(),
            sa.ForeignKey("decision_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "signal_id", sa.BigInteger(), sa.ForeignKey("trade_signals.signal_id"), nullable=False
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
            sa.ForeignKey("decision_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("instr_id", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("horizons", sa.JSON(), nullable=False),
        sa.Column("valid_from", sa.DateTime(), nullable=False),
        sa.Column("valid_to", sa.DateTime(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("consensus_score", sa.Numeric(10, 4), nullable=True),
        sa.CheckConstraint("direction IN ('long', 'short')", name="ck_opp_direction"),
    )
    op.create_index("ix_opportunity_valid", "opportunities", ["valid_from", "valid_to"])

    op.create_table(
        "opportunity_methods",
        sa.Column("opp_method_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "opp_id",
            sa.BigInteger(),
            sa.ForeignKey("opportunities.opp_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("method", sa.String(length=30), nullable=False),
        sa.Column("params", sa.JSON(), nullable=True),
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
            sa.ForeignKey("opportunities.opp_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rationale_md", sa.Text(), nullable=True),
        sa.Column("top_features", sa.JSON(), nullable=True),
        sa.Column("risk_notes_md", sa.Text(), nullable=True),
    )

    # =========================================================================
    # G) User Control Plane
    # =========================================================================

    op.create_table(
        "user_trading_policies",
        sa.Column("policy_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("asset_class", sa.String(length=20), nullable=False),
        sa.Column("horizon", sa.String(length=20), nullable=False),
        sa.Column("methods_allowed", sa.JSON(), nullable=False),
        sa.Column("default_method", sa.String(length=30), nullable=True),
        sa.Column("sizing_rules", sa.JSON(), nullable=True),
        sa.Column("risk_overrides", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "asset_class IN ('crypto', 'equity', 'futures', 'options', 'fx')",
            name="ck_policy_asset_class",
        ),
        sa.CheckConstraint(
            "horizon IN ('intraday', 'swing', 'long_term')", name="ck_policy_horizon"
        ),
        sa.UniqueConstraint("user_id", "asset_class", "horizon", name="uq_user_policy"),
    )

    op.create_table(
        "user_budget_buckets",
        sa.Column("bucket_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("asset_class", sa.String(length=20), nullable=False),
        sa.Column("horizon", sa.String(length=20), nullable=False),
        sa.Column("ccy", sa.String(length=10), nullable=False, server_default="USD"),
        sa.Column("target_alloc_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("hard_cap", sa.Numeric(20, 2), nullable=True),
        sa.Column("rebalance_policy", sa.JSON(), nullable=True),
        sa.UniqueConstraint("user_id", "asset_class", "horizon", name="uq_user_budget"),
    )

    op.create_table(
        "user_opportunity_subscriptions",
        sa.Column("opp_sub_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("asset_class", sa.String(length=20), nullable=False),
        sa.Column("horizons", sa.JSON(), nullable=False),
        sa.Column("instruments", sa.JSON(), nullable=True),
        sa.Column("autopilot", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("approval_mode", sa.String(length=20), nullable=False, server_default="instant"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "approval_mode IN ('instant', 'require_approval')", name="ck_approval_mode"
        ),
    )

    op.create_table(
        "opp_sub_execution_bindings",
        sa.Column("binding_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "opp_sub_id",
            sa.BigInteger(),
            sa.ForeignKey("user_opportunity_subscriptions.opp_sub_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.BigInteger(),
            sa.ForeignKey("linked_broker_accounts.account_id"),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("scope", sa.String(length=50), nullable=False, server_default="all"),
        sa.Column("constraints", sa.JSON(), nullable=True),
    )

    # =========================================================================
    # H) User Option Presets
    # =========================================================================

    op.create_table(
        "user_option_presets",
        sa.Column("preset_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column(
            "template_id",
            sa.Integer(),
            sa.ForeignKey("option_strategy_templates.template_id"),
            nullable=False,
        ),
        sa.Column("underlier", sa.String(length=50), nullable=False),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
    )

    # =========================================================================
    # I) OMS/EMS
    # =========================================================================

    op.create_table(
        "order_intents",
        sa.Column("intent_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("opp_id", sa.BigInteger(), sa.ForeignKey("opportunities.opp_id"), nullable=True),
        sa.Column(
            "account_id",
            sa.BigInteger(),
            sa.ForeignKey("linked_broker_accounts.account_id"),
            nullable=False,
        ),
        sa.Column("method", sa.String(length=30), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="created"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "method IN ('SPOT', 'MARGIN', 'PERP', 'FUTURES', 'OPTIONS_SINGLE', 'OPTIONS_STRATEGY')",
            name="ck_intent_method",
        ),
        sa.CheckConstraint(
            "status IN ('created', 'routed', 'rejected', 'canceled', 'expired')",
            name="ck_intent_status",
        ),
    )

    op.create_table(
        "orders",
        sa.Column("order_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "intent_id",
            sa.BigInteger(),
            sa.ForeignKey("order_intents.intent_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("broker_id", sa.Integer(), sa.ForeignKey("brokers.broker_id"), nullable=False),
        sa.Column(
            "account_id",
            sa.BigInteger(),
            sa.ForeignKey("linked_broker_accounts.account_id"),
            nullable=False,
        ),
        sa.Column("broker_order_ref", sa.String(length=255), nullable=True),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("routed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_update", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "state IN ('new', 'working', 'partially_filled', 'filled', 'canceled', 'rejected')",
            name="ck_order_state",
        ),
    )

    op.create_table(
        "child_orders",
        sa.Column("child_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "order_id",
            sa.BigInteger(),
            sa.ForeignKey("orders.order_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("broker_order_ref", sa.String(length=255), nullable=True),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=True),
    )

    op.create_table(
        "option_order_legs",
        sa.Column("leg_row_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "order_id",
            sa.BigInteger(),
            sa.ForeignKey("orders.order_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("leg_index", sa.Integer(), nullable=False),
        sa.Column("option_right", sa.String(length=10), nullable=True),
        sa.Column("position", sa.String(length=10), nullable=True),
        sa.Column("strike", sa.Numeric(20, 8), nullable=True),
        sa.Column("expiry", sa.Date(), nullable=True),
        sa.Column("qty", sa.Numeric(20, 8), nullable=False),
        sa.CheckConstraint("option_right IN ('CALL', 'PUT')", name="ck_option_right"),
        sa.CheckConstraint("position IN ('LONG', 'SHORT')", name="ck_option_position"),
    )

    op.create_table(
        "executions",
        sa.Column("exec_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "order_id",
            sa.BigInteger(),
            sa.ForeignKey("orders.order_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("instr_id", sa.Integer(), sa.ForeignKey("instruments.instr_id"), nullable=True),
        sa.Column("fill_ts", sa.DateTime(), nullable=False),
        sa.Column("qty", sa.Numeric(20, 8), nullable=False),
        sa.Column("price", sa.Numeric(20, 8), nullable=False),
        sa.Column("fee_ccy", sa.String(length=10), nullable=True),
        sa.Column("fee_amount", sa.Numeric(20, 8), nullable=True),
        sa.Column("venue", sa.String(length=50), nullable=True),
        sa.Column("trade_id", sa.String(length=255), nullable=True),
    )

    op.create_table(
        "positions",
        sa.Column("pos_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "account_id",
            sa.BigInteger(),
            sa.ForeignKey("linked_broker_accounts.account_id"),
            nullable=False,
        ),
        sa.Column("instr_id", sa.Integer(), sa.ForeignKey("instruments.instr_id"), nullable=False),
        sa.Column("qty", sa.Numeric(20, 8), nullable=False),
        sa.Column("avg_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("last_mark", sa.Numeric(20, 8), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("account_id", "instr_id", name="uq_position"),
    )

    op.create_table(
        "daily_nav",
        sa.Column("nav_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("nav_ccy", sa.String(length=10), nullable=False),
        sa.Column("nav_value", sa.Numeric(20, 2), nullable=False),
        sa.Column("ret_1d", sa.Numeric(10, 6), nullable=True),
        sa.Column("drawdown", sa.Numeric(10, 6), nullable=True),
        sa.Column("benchmark", sa.String(length=50), nullable=True),
        sa.Column("bench_ret_1d", sa.Numeric(10, 6), nullable=True),
        sa.UniqueConstraint("user_id", "date", name="uq_daily_nav"),
    )

    # =========================================================================
    # J) Risk, Audit & Notifications
    # =========================================================================

    op.create_table(
        "risk_breaches",
        sa.Column("breach_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("owner_type", sa.String(length=20), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("rule_code", sa.String(length=100), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.CheckConstraint("severity IN ('info', 'warn', 'block')", name="ck_severity"),
    )

    op.create_table(
        "api_audit_logs",
        sa.Column("audit_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=True),
        sa.Column(
            "account_id",
            sa.BigInteger(),
            sa.ForeignKey("linked_broker_accounts.account_id"),
            nullable=True,
        ),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("req", sa.JSON(), nullable=False),
        sa.Column("resp", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), index=True
        ),
        sa.CheckConstraint("status IN ('ok', 'error')", name="ck_audit_status"),
    )

    op.create_table(
        "user_notifications",
        sa.Column("notif_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("event_code", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("channel IN ('email', 'sms', 'push', 'webhook')", name="ck_channel"),
    )

    op.create_table(
        "outbound_webhooks",
        sa.Column("hook_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=True),
        sa.Column("url", sa.String(length=500), nullable=False),
        sa.Column("secret_ref", sa.String(length=500), nullable=True),
        sa.Column("event_filter", sa.JSON(), nullable=True),
    )

    # =========================================================================
    # K) Scoring Engine - Scores & Bindings
    # =========================================================================

    op.create_table(
        "asset_scores",
        sa.Column("score_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("instr_id", sa.Integer(), sa.ForeignKey("instruments.instr_id"), nullable=False),
        sa.Column("score_value", sa.Numeric(10, 4), nullable=False),
        sa.Column("components", sa.JSON(), nullable=False),
        sa.Column("weights_applied", sa.JSON(), nullable=False),
        sa.Column(
            "aggregation_method",
            sa.String(length=50),
            nullable=False,
            server_default="weighted_average",
        ),
        sa.Column("confidence", sa.Numeric(10, 4), nullable=True),
        sa.Column("decay_factor", sa.Numeric(10, 4), server_default="1.0"),
        sa.Column("signals_count", sa.Integer(), server_default="0"),
        sa.Column("ts", sa.DateTime(), nullable=False, index=True),
        sa.Column("valid_until", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_asset_score_instr_ts", "asset_scores", ["instr_id", "ts"])

    op.create_table(
        "sector_scores",
        sa.Column("score_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("sector_id", sa.Integer(), sa.ForeignKey("sectors.sector_id"), nullable=False),
        sa.Column("score_value", sa.Numeric(10, 4), nullable=False),
        sa.Column("constituent_scores", sa.JSON(), nullable=False),
        sa.Column(
            "aggregation_method",
            sa.String(length=50),
            nullable=False,
            server_default="weighted_average",
        ),
        sa.Column("ts", sa.DateTime(), nullable=False, index=True),
    )
    op.create_index("ix_sector_score_ts", "sector_scores", ["sector_id", "ts"])

    op.create_table(
        "market_scores",
        sa.Column("score_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("asset_class", sa.String(length=20), nullable=False),
        sa.Column("score_value", sa.Numeric(10, 4), nullable=False),
        sa.Column("sector_scores", sa.JSON(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False, index=True),
        sa.CheckConstraint(
            "asset_class IN ('crypto', 'equity', 'futures', 'options', 'fx', 'commodities')",
            name="ck_market_score_asset_class",
        ),
    )

    op.create_table(
        "user_strategy_bindings",
        sa.Column("binding_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column(
            "strategy_id",
            sa.String(length=50),
            sa.ForeignKey("strategies.strategy_id"),
            nullable=True,
        ),
        sa.Column(
            "asset_score_threshold", sa.Numeric(10, 4), nullable=False, server_default="0.60"
        ),
        sa.Column("sector_score_threshold", sa.Numeric(10, 4), nullable=True),
        sa.Column("market_score_threshold", sa.Numeric(10, 4), nullable=True),
        sa.Column("execution_modes_allowed", sa.JSON(), nullable=True),
        sa.Column("preferred_mode", sa.String(length=30), nullable=True),
        sa.Column(
            "mode_selection_policy", sa.String(length=30), nullable=False, server_default="fixed"
        ),
        sa.Column("asset_classes_allowed", sa.JSON(), nullable=True),
        sa.Column("instruments_allowed", sa.JSON(), nullable=True),
        sa.Column("sectors_allowed", sa.JSON(), nullable=True),
        sa.Column(
            "sizing_profile_id",
            sa.Integer(),
            sa.ForeignKey("sizing_profiles.profile_id"),
            nullable=True,
        ),
        sa.Column("max_position_pct", sa.Numeric(10, 4), nullable=False, server_default="0.10"),
        sa.Column("max_daily_loss_pct", sa.Numeric(10, 4), nullable=False, server_default="0.05"),
        sa.Column("max_open_positions", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("autopilot", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.CheckConstraint(
            "mode_selection_policy IN "
            "('fixed', 'best_return', 'lowest_risk', 'highest_sharpe', 'user_rotating')",
            name="ck_mode_selection_policy",
        ),
        sa.UniqueConstraint("user_id", "strategy_id", name="uq_user_strategy_binding"),
    )

    op.create_table(
        "mode_performance",
        sa.Column("perf_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("instr_id", sa.Integer(), sa.ForeignKey("instruments.instr_id"), nullable=True),
        sa.Column("sector_id", sa.Integer(), sa.ForeignKey("sectors.sector_id"), nullable=True),
        sa.Column("asset_class", sa.String(length=20), nullable=True),
        sa.Column("execution_mode", sa.String(length=30), nullable=False),
        sa.Column("horizon", sa.String(length=20), nullable=False),
        sa.Column("period_start", sa.DateTime(), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("total_return", sa.Numeric(10, 4), nullable=True),
        sa.Column("sharpe_ratio", sa.Numeric(10, 4), nullable=True),
        sa.Column("sortino_ratio", sa.Numeric(10, 4), nullable=True),
        sa.Column("win_rate", sa.Numeric(10, 4), nullable=True),
        sa.Column("avg_win", sa.Numeric(10, 4), nullable=True),
        sa.Column("avg_loss", sa.Numeric(10, 4), nullable=True),
        sa.Column("max_drawdown", sa.Numeric(10, 4), nullable=True),
        sa.Column("sample_size", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "execution_mode IN ('spot', 'margin', 'perpetual', 'futures', "
            "'bull_call', 'bear_put', 'bull_put', 'bear_call', "
            "'iron_condor', 'straddle', 'strangle', 'options_single')",
            name="ck_mode_perf_exec_mode",
        ),
        sa.CheckConstraint(
            "horizon IN ('intraday', 'swing', 'long_term')", name="ck_mode_perf_horizon"
        ),
    )
    op.create_index(
        "ix_mode_perf_lookup", "mode_performance", ["instr_id", "execution_mode", "horizon"]
    )

    op.create_table(
        "execution_decision_logs",
        sa.Column("decision_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column(
            "binding_id",
            sa.BigInteger(),
            sa.ForeignKey("user_strategy_bindings.binding_id"),
            nullable=True,
        ),
        sa.Column("instr_id", sa.Integer(), sa.ForeignKey("instruments.instr_id"), nullable=False),
        sa.Column("asset_score", sa.Numeric(10, 4), nullable=False),
        sa.Column("sector_score", sa.Numeric(10, 4), nullable=True),
        sa.Column("market_score", sa.Numeric(10, 4), nullable=True),
        sa.Column("should_execute", sa.Boolean(), nullable=False),
        sa.Column("execution_mode", sa.String(length=30), nullable=True),
        sa.Column("direction", sa.String(length=10), nullable=True),
        sa.Column("position_size_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("position_size_usd", sa.Numeric(20, 2), nullable=True),
        sa.Column("execution_id", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("reasoning", sa.JSON(), nullable=True),
        sa.Column("ts", sa.DateTime(), nullable=False, index=True),
        sa.CheckConstraint(
            "status IN ('pending', 'executed', 'rejected', 'failed', 'skipped')",
            name="ck_decision_status",
        ),
    )

    # =========================================================================
    # L) Signal Performance & Feedback Loop
    # =========================================================================

    op.create_table(
        "signal_performance",
        sa.Column("perf_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "signal_id",
            sa.BigInteger(),
            sa.ForeignKey("canonical_signals.signal_id"),
            nullable=False,
        ),
        sa.Column(
            "strategy_id",
            sa.String(length=50),
            sa.ForeignKey("strategies.strategy_id"),
            nullable=False,
        ),
        sa.Column(
            "strat_ver_id",
            sa.BigInteger(),
            sa.ForeignKey("strategy_versions.strat_ver_id"),
            nullable=True,
        ),
        sa.Column("instr_id", sa.Integer(), sa.ForeignKey("instruments.instr_id"), nullable=False),
        sa.Column("predicted_direction", sa.String(length=10), nullable=False),
        sa.Column("confidence", sa.Numeric(10, 4), nullable=True),
        sa.Column("signal_ts", sa.DateTime(), nullable=False),
        sa.Column("entry_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("evaluation_horizon", sa.String(length=20), nullable=False),
        sa.Column("evaluation_ts", sa.DateTime(), nullable=True),
        sa.Column("exit_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("actual_direction", sa.String(length=10), nullable=True),
        sa.Column("price_change_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("is_correct", sa.Boolean(), nullable=True),
        sa.Column("pnl_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("consecutive_wrong_count", sa.Integer(), server_default="0"),
        sa.Column("needs_optimization", sa.Boolean(), server_default="false"),
        sa.Column("evaluated_at", sa.DateTime(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "predicted_direction IN ('long', 'short')", name="ck_signal_perf_predicted"
        ),
        sa.CheckConstraint(
            "actual_direction IN ('long', 'short', 'flat')", name="ck_signal_perf_actual"
        ),
        sa.CheckConstraint(
            "evaluation_horizon IN ('1h', '4h', '1d', '1w', '2w', '1m')",
            name="ck_signal_perf_horizon",
        ),
    )
    op.create_index("ix_signal_perf_strategy", "signal_performance", ["strategy_id", "signal_ts"])
    op.create_index(
        "ix_signal_perf_needs_opt", "signal_performance", ["needs_optimization", "strategy_id"]
    )

    op.create_table(
        "strategy_parameter_feedback",
        sa.Column("feedback_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "strategy_id",
            sa.String(length=50),
            sa.ForeignKey("strategies.strategy_id"),
            nullable=False,
        ),
        sa.Column(
            "strat_ver_id",
            sa.BigInteger(),
            sa.ForeignKey("strategy_versions.strat_ver_id"),
            nullable=True,
        ),
        sa.Column("instr_id", sa.Integer(), sa.ForeignKey("instruments.instr_id"), nullable=True),
        sa.Column("trigger_reason", sa.String(length=100), nullable=False),
        sa.Column("consecutive_wrong_signals", sa.Integer(), server_default="0"),
        sa.Column("accuracy_window_days", sa.Integer(), nullable=True),
        sa.Column("accuracy_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("current_params", sa.JSON(), nullable=False),
        sa.Column("suggested_params", sa.JSON(), nullable=False),
        sa.Column("optimization_method", sa.String(length=50), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("supporting_data", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column(
            "reviewed_by", sa.String(length=50), sa.ForeignKey("users.user_id"), nullable=True
        ),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("config_file_path", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'applied', 'expired')",
            name="ck_param_feedback_status",
        ),
        sa.CheckConstraint(
            "trigger_reason IN ('consecutive_wrong', 'low_accuracy', "
            "'high_drawdown', 'manual_review', 'scheduled')",
            name="ck_param_feedback_trigger",
        ),
    )
    op.create_index(
        "ix_param_feedback_strategy_status",
        "strategy_parameter_feedback",
        ["strategy_id", "status"],
    )

    op.create_table(
        "backtest_results",
        sa.Column("result_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "strategy_id",
            sa.String(length=50),
            sa.ForeignKey("strategies.strategy_id"),
            nullable=False,
        ),
        sa.Column(
            "strat_ver_id",
            sa.BigInteger(),
            sa.ForeignKey("strategy_versions.strat_ver_id"),
            nullable=True,
        ),
        sa.Column("backtest_id", sa.String(length=100), unique=True, nullable=False),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("asset_class", sa.String(length=20), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("initial_capital", sa.Numeric(20, 2), nullable=False),
        sa.Column("timeframe", sa.String(length=20), nullable=True),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("total_return_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("cagr_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("sharpe_ratio", sa.Numeric(10, 4), nullable=True),
        sa.Column("sortino_ratio", sa.Numeric(10, 4), nullable=True),
        sa.Column("max_drawdown_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("win_rate_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("profit_factor", sa.Numeric(10, 4), nullable=True),
        sa.Column("avg_win_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("avg_loss_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("total_trades", sa.Integer(), nullable=True),
        sa.Column("winning_trades", sa.Integer(), nullable=True),
        sa.Column("losing_trades", sa.Integer(), nullable=True),
        sa.Column("total_signals", sa.Integer(), nullable=True),
        sa.Column("long_signals", sa.Integer(), nullable=True),
        sa.Column("short_signals", sa.Integer(), nullable=True),
        sa.Column("correct_predictions", sa.Integer(), nullable=True),
        sa.Column("prediction_accuracy_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("final_equity", sa.Numeric(20, 2), nullable=True),
        sa.Column("peak_equity", sa.Numeric(20, 2), nullable=True),
        sa.Column("fees_paid", sa.Numeric(20, 2), nullable=True),
        sa.Column("engine", sa.String(length=50), nullable=False, server_default="lean"),
        sa.Column("log_file_path", sa.String(length=500), nullable=True),
        sa.Column("trades_json", sa.JSON(), nullable=True),
        sa.Column("equity_curve", sa.JSON(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "asset_class IN ('crypto', 'equity', 'futures', 'options', 'fx')",
            name="ck_backtest_asset_class",
        ),
        sa.CheckConstraint(
            "engine IN ('lean', 'backtrader', 'zipline', 'custom')", name="ck_backtest_engine"
        ),
    )
    op.create_index("ix_backtest_strategy_date", "backtest_results", ["strategy_id", "end_date"])

    op.create_table(
        "strategy_consecutive_wrong_tracker",
        sa.Column("tracker_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "strategy_id",
            sa.String(length=50),
            sa.ForeignKey("strategies.strategy_id"),
            nullable=False,
        ),
        sa.Column(
            "strat_ver_id",
            sa.BigInteger(),
            sa.ForeignKey("strategy_versions.strat_ver_id"),
            nullable=True,
        ),
        sa.Column("instr_id", sa.Integer(), sa.ForeignKey("instruments.instr_id"), nullable=False),
        sa.Column("consecutive_wrong_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "last_signal_id",
            sa.BigInteger(),
            sa.ForeignKey("canonical_signals.signal_id"),
            nullable=True,
        ),
        sa.Column("last_signal_ts", sa.DateTime(), nullable=True),
        sa.Column("last_evaluation_ts", sa.DateTime(), nullable=True),
        sa.Column("wrong_threshold", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("threshold_reached", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("threshold_reached_at", sa.DateTime(), nullable=True),
        sa.Column(
            "feedback_id",
            sa.BigInteger(),
            sa.ForeignKey("strategy_parameter_feedback.feedback_id"),
            nullable=True,
        ),
        sa.Column("last_reset_ts", sa.DateTime(), nullable=True),
        sa.Column("reset_reason", sa.String(length=100), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("strategy_id", "instr_id", name="uq_strategy_instr_tracker"),
    )
    op.create_index(
        "ix_tracker_threshold",
        "strategy_consecutive_wrong_tracker",
        ["threshold_reached", "strategy_id"],
    )


def downgrade() -> None:
    # Drop tables in reverse order of creation (respect FK dependencies)

    # L) Signal Performance & Feedback Loop
    op.drop_index("ix_tracker_threshold", table_name="strategy_consecutive_wrong_tracker")
    op.drop_table("strategy_consecutive_wrong_tracker")
    op.drop_index("ix_backtest_strategy_date", table_name="backtest_results")
    op.drop_table("backtest_results")
    op.drop_index("ix_param_feedback_strategy_status", table_name="strategy_parameter_feedback")
    op.drop_table("strategy_parameter_feedback")
    op.drop_index("ix_signal_perf_needs_opt", table_name="signal_performance")
    op.drop_index("ix_signal_perf_strategy", table_name="signal_performance")
    op.drop_table("signal_performance")

    # K) Scoring Engine
    op.drop_table("execution_decision_logs")
    op.drop_index("ix_mode_perf_lookup", table_name="mode_performance")
    op.drop_table("mode_performance")
    op.drop_table("user_strategy_bindings")
    op.drop_table("market_scores")
    op.drop_index("ix_sector_score_ts", table_name="sector_scores")
    op.drop_table("sector_scores")
    op.drop_index("ix_asset_score_instr_ts", table_name="asset_scores")
    op.drop_table("asset_scores")

    # J) Risk, Audit & Notifications
    op.drop_table("outbound_webhooks")
    op.drop_table("user_notifications")
    op.drop_table("api_audit_logs")
    op.drop_table("risk_breaches")

    # I) OMS/EMS
    op.drop_table("daily_nav")
    op.drop_table("positions")
    op.drop_table("executions")
    op.drop_table("option_order_legs")
    op.drop_table("child_orders")
    op.drop_table("orders")
    op.drop_table("order_intents")

    # H) User Option Presets
    op.drop_table("user_option_presets")

    # G) User Control Plane
    op.drop_table("opp_sub_execution_bindings")
    op.drop_table("user_opportunity_subscriptions")
    op.drop_table("user_budget_buckets")
    op.drop_table("user_trading_policies")

    # F) Decision Engine
    op.drop_table("opportunity_explanations")
    op.drop_table("opportunity_methods")
    op.drop_index("ix_opportunity_valid", table_name="opportunities")
    op.drop_table("opportunities")
    op.drop_table("decision_run_members")
    op.drop_index("ix_decision_instr_ts", table_name="decision_runs")
    op.drop_table("decision_runs")
    op.drop_index("ix_signal_instr_ts", table_name="trade_signals")
    op.drop_table("trade_signals")

    # E) Strategy Versions & Coverage
    op.drop_table("strategy_coverage")
    op.drop_table("strategy_versions")

    # C) User-Linked Broker Accounts
    op.drop_table("broker_credentials")
    op.drop_table("linked_broker_accounts")

    # B) Legal, Consents & Suitability
    op.drop_table("user_suitability_responses")
    op.drop_table("suitability_questionnaires")
    op.drop_table("user_consents")
