"""Retire empty commercial tenancy without changing historical trading identity.

Populated metadata requires explicit archival/disposition outside this revision.
Downgrade restores the empty schema only; it cannot recover deleted records.

Revision ID: 0103_remove_commercial_tenancy
Revises: 0102_owner_control_plane
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0103_remove_commercial_tenancy"
down_revision = "0102_owner_control_plane"
branch_labels = None
depends_on = None

_TABLES = ("user_plan_subscriptions", "user_roles", "plans", "orgs")


def _assert_empty() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # A restricted connection must error instead of counting only visible
        # rows. The supported maintenance connection can inspect every row.
        op.execute("SET LOCAL row_security = off")
        op.execute("SET LOCAL search_path = public, pg_temp")
        # Hold a write/DDL fence through commit so a commercial row or org link
        # cannot appear after the preflight has approved an empty schema.
        op.execute(
            "LOCK TABLE users, orgs, plans, user_roles, user_plan_subscriptions "
            "IN ACCESS EXCLUSIVE MODE"
        )
    populated = {
        table: int(bind.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one())
        for table in _TABLES
    }
    populated["users.org_id"] = int(
        bind.execute(sa.text("SELECT count(*) FROM users WHERE org_id IS NOT NULL")).scalar_one()
    )
    details = ", ".join(f"{name}={count}" for name, count in sorted(populated.items()) if count)
    if details:
        msg = (
            "Commercial tenancy retirement blocked by populated state: "
            f"{details}. A backup and explicit archival/disposition are required before retrying; "
            "this migration does not delete historical records."
        )
        raise RuntimeError(msg)


def upgrade() -> None:
    _assert_empty()
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("users") as batch:
            batch.drop_column("org_id")
    else:
        op.drop_constraint("users_org_id_fkey", "users", type_="foreignkey")
        op.drop_column("users", "org_id")
    for table in _TABLES:
        # No cascading DDL: unexpected dependents must refuse the migration.
        op.drop_table(table)


def downgrade() -> None:
    # Restore the 0102 schema, including its original names and nullability.
    op.create_table(
        "orgs",
        sa.Column("org_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_orgs_name", "orgs", ["name"], unique=True)
    op.create_table(
        "plans",
        sa.Column("plan_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("features", sa.JSON(), nullable=False),
        sa.UniqueConstraint("code", name="plans_code_key"),
    )
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("org_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("users_org_id_fkey", "orgs", ["org_id"], ["org_id"])
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.String(50), nullable=False),
        sa.Column("role", sa.String(50), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "role", name="user_roles_pkey"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], name="user_roles_user_id_fkey"),
    )
    op.create_table(
        "user_plan_subscriptions",
        sa.Column("sub_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(50), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.user_id"], name="user_plan_subscriptions_user_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.plan_id"], name="user_plan_subscriptions_plan_id_fkey"
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        # 0052 removed all commercial runtime grants/policies. Restore its
        # enabled, deny-by-default RLS state without reviving old entitlements.
        for table in ("user_roles", "user_plan_subscriptions"):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
