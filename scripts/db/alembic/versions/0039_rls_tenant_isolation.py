"""Multi-tenant hardening (H-8): non-superuser app role + RLS tenant isolation.

Lands the tenant-isolation SEAM described in docs/SCALING.md §H-8 — the DB-level
backstop behind the application-layer ``WHERE user_id = ...`` filters.

What this migration does (Postgres only; no-ops on sqlite test fixtures):
  * Creates the non-superuser ``vm_app`` role (LOGIN, NOSUPERUSER, NOBYPASSRLS) that
    the *services* will connect as. Migrations keep running as the owner/superuser.
    The role's password is set operationally at deploy (never in a migration).
  * Grants ``vm_app`` CRUD on all tables + sequence usage (incl. default privileges
    for future tables), so it can run the app without owning anything.
  * Enables RLS + a tenant-isolation policy on every table with a ``user_id`` column.
    The policy scopes rows to ``current_setting('app.current_tenant')`` (set per
    unit-of-work via ``lib_application.db.session.tenant_scope``), with a deliberate,
    documented cross-tenant escape for the scoring fan-out
    (``current_setting('app.cross_tenant') = 'on'``) which reads *all* tenants'
    bindings. Writes always require ``user_id`` == the current tenant (WITH CHECK).

SAFETY / staging: RLS is enabled but INERT while services still connect as the
superuser ``trader`` (owner + BYPASSRLS bypasses every policy). It only takes effect
once ``DATABASE_URL`` is flipped to the ``vm_app`` credential — a separate, verified
rollout step (per-service ``tenant_scope`` wiring + e2e), NOT this migration.

Revision ID: 0039_rls_tenant_isolation
Revises: 0038_managed_secrets
Create Date: 2026-07-02
"""

from __future__ import annotations

from alembic import op

revision = "0039_rls_tenant_isolation"
down_revision = "0038_managed_secrets"
branch_labels = None
depends_on = None

# Tables with a direct ``user_id`` column — the tenant-scoped tables. Tables scoped
# indirectly (via account_id -> linked_broker_accounts.user_id: executions, orders,
# positions, broker_credentials, ...) need subquery policies and are a documented
# follow-up (see docs/SCALING.md §H-8).
_TENANT_TABLES = (
    "api_audit_logs",
    "daily_nav",
    "execution_decision_logs",
    "execution_logs",
    "execution_metrics",
    "linked_broker_accounts",
    "options_positions",
    "order_intents",
    "outbound_webhooks",
    "pending_orders",
    "sizing_profiles",
    "user_budget_buckets",
    "user_consents",
    "user_feature_flags",
    "user_notifications",
    "user_opportunity_subscriptions",
    "user_option_presets",
    "user_plan_subscriptions",
    "user_roles",
    "user_strategy_bindings",
    "user_strategy_configs",
    "user_suitability_responses",
    "user_trading_policies",
    "users",
)

# Read visibility: own rows, OR the sanctioned cross-tenant read (scoring fan-out).
_USING = (
    "user_id = current_setting('app.current_tenant', true) "
    "OR current_setting('app.cross_tenant', true) = 'on'"
)
# Writes are always self-scoped — no cross-tenant inserts/updates even under the flag.
_WITH_CHECK = "user_id = current_setting('app.current_tenant', true)"


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return

    # 1. Non-superuser app role (idempotent; password set operationally at deploy).
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vm_app') THEN "
        "CREATE ROLE vm_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS; "
        "END IF; END $$;"
    )

    # 2. Grants so vm_app can run the app without owning anything.
    op.execute("GRANT USAGE ON SCHEMA public TO vm_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO vm_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO vm_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO vm_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO vm_app"
    )

    # 3. RLS + tenant-isolation policy per tenant table.
    for table in _TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            f"FOR ALL USING ({_USING}) WITH CHECK ({_WITH_CHECK})"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    for table in _TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    # Revoke grants before dropping the role (Postgres refuses to drop a role that
    # still owns grants).
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM vm_app")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM vm_app")
    op.execute("REVOKE USAGE ON SCHEMA public FROM vm_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM vm_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM vm_app")
    op.execute("DROP ROLE IF EXISTS vm_app")
