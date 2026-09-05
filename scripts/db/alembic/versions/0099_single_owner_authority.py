"""Add explicit deployment ownership and stable owner-relative account keys.

Existing users and accounts retain their identifiers and attribution. No row is
selected automatically; designation is a separate maintenance operation.

Revision ID: 0099_single_owner_authority
Revises: 0098_retire_liquidity_leaders
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0099_single_owner_authority"
down_revision = "0098_retire_liquidity_leaders"
branch_labels = None
depends_on = None

_SERVICE_ROLES = (
    "vm_backend",
    "vm_scoring",
    "vm_execution",
    "vm_feedback",
    "vm_market_data",
    "vm_indicator",
)


def _install_discovery() -> None:
    # The migration identity owns this function and users, and can bypass RLS.
    # No caller-controlled identifiers, search path, or tenant settings enter SQL.
    op.execute(
        """
        CREATE FUNCTION public.vm_deployment_owner_id()
        RETURNS text
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $function$
            SELECT CASE WHEN count(*) = 1 AND bool_and(u.status = 'active')
                        THEN min(u.user_id)::text ELSE NULL END
            FROM public.users AS u
            WHERE u.is_deployment_owner IS TRUE
        $function$;
        REVOKE ALL ON FUNCTION public.vm_deployment_owner_id() FROM PUBLIC;
        """
    )
    for role in _SERVICE_ROLES:
        # Existing groups have no users write grants. Enforce that boundary
        # explicitly before later migrations add reviewed profile-column grants.
        op.execute(f"REVOKE INSERT, UPDATE ON TABLE public.users FROM {role}")
        op.execute(
            f"REVOKE INSERT (is_deployment_owner), UPDATE (is_deployment_owner) "
            f"ON TABLE public.users FROM {role}"
        )
        op.execute(f"GRANT EXECUTE ON FUNCTION public.vm_deployment_owner_id() TO {role}")


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_deployment_owner", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "uq_users_deployment_owner",
        "users",
        ["is_deployment_owner"],
        unique=True,
        postgresql_where=sa.text("is_deployment_owner"),
        sqlite_where=sa.text("is_deployment_owner = 1"),
    )
    with op.batch_alter_table("linked_broker_accounts") as batch:
        batch.add_column(sa.Column("config_key", sa.String(100), nullable=True))
        batch.create_check_constraint(
            "ck_account_config_key",
            "config_key IS NULL OR length(trim(config_key, ' \t\n\r\v\f')) > 0",
        )
        batch.create_unique_constraint("uq_account_owner_config_key", ["user_id", "config_key"])
    if op.get_bind().dialect.name == "postgresql":
        _install_discovery()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "LOCK TABLE public.users, public.linked_broker_accounts IN ACCESS EXCLUSIVE MODE"
        )
    configured = bind.execute(
        sa.text(
            "SELECT (SELECT count(*) FROM users WHERE is_deployment_owner IS TRUE) + "
            "(SELECT count(*) FROM linked_broker_accounts WHERE config_key IS NOT NULL)"
        )
    ).scalar_one()
    if configured:
        message = (
            "Cannot downgrade configured deployment authority; explicit backup and "
            "disposition of owner designation and account keys are required."
        )
        raise RuntimeError(message)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION public.vm_deployment_owner_id()")
    with op.batch_alter_table("linked_broker_accounts") as batch:
        batch.drop_constraint("uq_account_owner_config_key", type_="unique")
        batch.drop_constraint("ck_account_config_key", type_="check")
        batch.drop_column("config_key")
    op.drop_index("uq_users_deployment_owner", table_name="users")
    op.drop_column("users", "is_deployment_owner")
