"""Scope every daily NAV observation to one owned broker account.

The legacy table was unique by ``(user_id, date)`` and therefore mixed the
capital and currencies of every broker account owned by the same user. Existing
rows have no account identity from which a safe attribution can be derived, so
the upgrade refuses to proceed when any are present instead of guessing.

Revision ID: 0061_account_scoped_daily_nav
Revises: 0060_paper_account_capital
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0061_account_scoped_daily_nav"
down_revision = "0060_paper_account_capital"
branch_labels = None
depends_on = None


def _lock_daily_nav() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.execute("LOCK TABLE daily_nav IN ACCESS EXCLUSIVE MODE")


def _row_count(bind: Any) -> int:
    return int(bind.execute(sa.text("SELECT count(*) FROM daily_nav")).scalar_one())


def _assert_daily_nav_empty() -> None:
    bind = op.get_bind()
    existing_rows = _row_count(bind)
    if existing_rows:
        msg = (
            "Cannot account-scope daily_nav safely: "
            f"{existing_rows} existing row(s) have no broker account attribution. "
            "Delete and regenerate these pre-production snapshots from persisted "
            "account NAV observations before retrying the migration."
        )
        raise RuntimeError(msg)


def _assert_downgrade_unambiguous() -> None:
    bind = op.get_bind()
    duplicate = bind.execute(
        sa.text(
            """
            SELECT user_id, date, count(*) AS row_count
            FROM daily_nav
            GROUP BY user_id, date
            HAVING count(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate is not None:
        msg = (
            "Cannot downgrade account-scoped daily_nav: at least one user/date "
            "contains multiple broker-account observations and cannot be collapsed "
            "without losing financial state."
        )
        raise RuntimeError(msg)


def upgrade() -> None:
    _lock_daily_nav()
    _assert_daily_nav_empty()

    with op.batch_alter_table("daily_nav") as batch_op:
        batch_op.drop_constraint("uq_daily_nav", type_="unique")
        batch_op.add_column(sa.Column("account_id", sa.BigInteger(), nullable=False))
        batch_op.create_foreign_key(
            "fk_daily_nav_account",
            "linked_broker_accounts",
            ["account_id"],
            ["account_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_daily_nav_account_owner",
            "linked_broker_accounts",
            ["account_id", "user_id"],
            ["account_id", "user_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_daily_nav_account_date",
            ["account_id", "date"],
        )
        batch_op.create_index(
            "ix_daily_nav_user_account_date",
            ["user_id", "account_id", "date"],
            unique=False,
        )


def downgrade() -> None:
    _lock_daily_nav()
    _assert_downgrade_unambiguous()

    with op.batch_alter_table("daily_nav") as batch_op:
        batch_op.drop_index("ix_daily_nav_user_account_date")
        batch_op.drop_constraint("uq_daily_nav_account_date", type_="unique")
        batch_op.drop_constraint("fk_daily_nav_account_owner", type_="foreignkey")
        batch_op.drop_constraint("fk_daily_nav_account", type_="foreignkey")
        batch_op.drop_column("account_id")
        batch_op.create_unique_constraint("uq_daily_nav", ["user_id", "date"])
