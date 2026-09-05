"""Align column nullability with the ORM models (NOT NULL tightening).

61 columns are NOT NULL in the models (and therefore in the create_all schema
the app actually deploys on) but were left nullable in the migrations. This
brings the migration schema up to the models so a migration-built database
matches what the app runs on — a prerequisite for migrations-as-truth.

Nullability is a postgres-deploy concern; sqlite test fixtures build their
schema from the models via create_all, so the migration is a no-op there.

Revision ID: 0025_align_not_null
Revises: 0024_purge_lean_defaults
Create Date: 2026-06-18
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0025_align_not_null"
down_revision = "0024_purge_lean_defaults"
branch_labels = None
depends_on = None

# (table, column) pairs that are NOT NULL in the models but nullable in the
# migrations. Generated from `compare_metadata` (modify_nullable, all True->False).
_NOT_NULL_COLS = [
    ("api_audit_logs", "created_at"),
    ("backtest_results", "created_at"),
    ("broker_environments", "base_urls"),
    ("broker_environments", "rate_limits"),
    ("brokers", "capabilities"),
    ("execution_logs", "status"),
    ("execution_logs", "created_at"),
    ("execution_metrics", "created_at"),
    ("instrument_sectors", "weight"),
    ("linked_broker_accounts", "created_at"),
    ("mode_performance", "updated_at"),
    ("opp_sub_execution_bindings", "constraints"),
    ("opportunity_methods", "params"),
    ("option_strategy_template_legs", "strike_spec"),
    ("option_strategy_template_legs", "expiry_spec"),
    ("options_positions", "status"),
    ("options_positions", "opened_at"),
    ("order_intents", "created_at"),
    ("orders", "routed_at"),
    ("orgs", "created_at"),
    ("outbound_webhooks", "event_filter"),
    ("positions", "updated_at"),
    ("risk_breaches", "occurred_at"),
    ("risk_mandates", "effective_at"),
    ("scoring_rules", "aggregation_method"),
    ("scoring_rules", "min_quorum"),
    ("scoring_rules", "decay_enabled"),
    ("scoring_rules", "strategy_weights"),
    ("scoring_rules", "is_active"),
    ("scoring_rules", "created_at"),
    ("sectors", "asset_class"),
    ("sectors", "created_at"),
    ("sizing_profiles", "user_id"),
    ("sizing_profiles", "params"),
    ("sizing_profiles", "is_default"),
    ("sizing_profiles", "created_at"),
    ("strategy_consecutive_wrong_tracker", "updated_at"),
    ("strategy_coverage", "instruments"),
    ("strategy_parameter_feedback", "created_at"),
    ("strategy_versions", "default_params"),
    ("strategy_versions", "released_at"),
    ("user_budget_buckets", "rebalance_policy"),
    ("user_consents", "meta"),
    ("user_feature_flags", "variant"),
    ("user_notifications", "created_at"),
    ("user_opportunity_subscriptions", "instruments"),
    ("user_opportunity_subscriptions", "created_at"),
    ("user_option_presets", "params"),
    ("user_plan_subscriptions", "status"),
    ("user_plan_subscriptions", "started_at"),
    ("user_strategy_bindings", "execution_modes_allowed"),
    ("user_strategy_bindings", "asset_classes_allowed"),
    ("user_strategy_bindings", "created_at"),
    ("user_strategy_bindings", "updated_at"),
    ("user_strategy_configs", "is_active"),
    ("user_strategy_configs", "created_at"),
    ("user_strategy_configs", "updated_at"),
    ("user_trading_policies", "sizing_rules"),
    ("user_trading_policies", "risk_overrides"),
    ("users", "status"),
    ("users", "created_at"),
]


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    for table, column in _NOT_NULL_COLS:
        op.alter_column(table, column, nullable=False)


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    for table, column in _NOT_NULL_COLS:
        op.alter_column(table, column, nullable=True)
