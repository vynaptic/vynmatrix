"""Enforce unambiguous broker routes and active credential ownership.

The broker catalogue is a routing authority, so a broker/environment/region
triple must identify exactly one route. Credential references must not alias
across accounts, while disabled or expired credential history remains valid.
The backend role receives the tenant-scoped UPDATE permission required by the
atomic credential-rotation endpoint.

Revision ID: 0069_broker_account_contracts
Revises: 0068_typed_broker_instrument
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0069_broker_account_contracts"
down_revision = "0068_typed_broker_instrument"
branch_labels = None
depends_on = None

_ACCOUNT_OWNED = (
    "account_id IN (SELECT account_id FROM linked_broker_accounts "
    "WHERE user_id = current_setting('app.current_tenant', true))"
)


def _require_clean_existing_data() -> None:
    bind = op.get_bind()

    invalid_region = bind.execute(
        sa.text(
            """
            SELECT broker_env_id
            FROM broker_environments
            WHERE region IS NULL OR length(trim(region)) = 0
            LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if invalid_region is not None:
        msg = (
            "broker_environments contains a null or blank region; "
            "repair the catalogue before upgrading"
        )
        raise RuntimeError(msg)

    duplicate_route = bind.execute(
        sa.text(
            """
            SELECT broker_id, environment, region
            FROM broker_environments
            GROUP BY broker_id, environment, region
            HAVING count(*) > 1
            LIMIT 1
            """
        )
    ).one_or_none()
    if duplicate_route is not None:
        msg = (
            "broker_environments contains duplicate broker/environment/region "
            f"route {tuple(duplicate_route)}"
        )
        raise RuntimeError(msg)

    invalid_secret_ref = bind.execute(
        sa.text(
            """
            SELECT cred_id
            FROM broker_credentials
            WHERE length(trim(secret_ref)) = 0
            LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if invalid_secret_ref is not None:
        msg = "broker_credentials contains a blank secret_ref; repair it before upgrading"
        raise RuntimeError(msg)

    duplicate_secret_ref = bind.execute(
        sa.text(
            """
            SELECT secret_ref
            FROM broker_credentials
            GROUP BY secret_ref
            HAVING count(*) > 1
            LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if duplicate_secret_ref is not None:
        msg = (
            "broker_credentials contains a secret_ref shared by multiple rows; "
            "split the secret ownership before upgrading"
        )
        raise RuntimeError(msg)

    duplicate_active_account = bind.execute(
        sa.text(
            """
            SELECT account_id
            FROM broker_credentials
            WHERE status = 'active'
            GROUP BY account_id
            HAVING count(*) > 1
            LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if duplicate_active_account is not None:
        msg = (
            "broker_credentials contains multiple active rows for account "
            f"{int(duplicate_active_account)}; retire all but one before upgrading"
        )
        raise RuntimeError(msg)


def _create_constraints() -> None:
    with op.batch_alter_table("broker_environments") as batch_op:
        batch_op.alter_column(
            "region",
            existing_type=sa.String(length=50),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_broker_environment_region",
            "length(trim(region)) > 0",
        )
        batch_op.create_unique_constraint(
            "uq_broker_environment_route",
            ["broker_id", "environment", "region"],
        )

    with op.batch_alter_table("broker_credentials") as batch_op:
        batch_op.create_check_constraint(
            "ck_broker_credentials_secret_ref",
            "length(trim(secret_ref)) > 0",
        )
        batch_op.create_unique_constraint(
            "uq_broker_credentials_secret_ref",
            ["secret_ref"],
        )

    op.create_index(
        "uq_broker_credentials_active_account",
        "broker_credentials",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )


def _grant_backend_rotation() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("GRANT UPDATE ON TABLE public.broker_credentials TO vm_backend")
    op.execute("DROP POLICY IF EXISTS broker_credentials_backend_update ON broker_credentials")
    op.execute(
        "CREATE POLICY broker_credentials_backend_update "
        "ON broker_credentials FOR UPDATE TO vm_backend "
        f"USING ({_ACCOUNT_OWNED}) WITH CHECK ({_ACCOUNT_OWNED})"
    )


def upgrade() -> None:
    _require_clean_existing_data()
    _create_constraints()
    _grant_backend_rotation()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS broker_credentials_backend_update ON broker_credentials")
        op.execute("REVOKE UPDATE ON TABLE public.broker_credentials FROM vm_backend")

    op.drop_index(
        "uq_broker_credentials_active_account",
        table_name="broker_credentials",
    )
    with op.batch_alter_table("broker_credentials") as batch_op:
        batch_op.drop_constraint(
            "uq_broker_credentials_secret_ref",
            type_="unique",
        )
        batch_op.drop_constraint(
            "ck_broker_credentials_secret_ref",
            type_="check",
        )
    with op.batch_alter_table("broker_environments") as batch_op:
        batch_op.drop_constraint(
            "uq_broker_environment_route",
            type_="unique",
        )
        batch_op.drop_constraint(
            "ck_broker_environment_region",
            type_="check",
        )
        batch_op.alter_column(
            "region",
            existing_type=sa.String(length=50),
            nullable=True,
        )
