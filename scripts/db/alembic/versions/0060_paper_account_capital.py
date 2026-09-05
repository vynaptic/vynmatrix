"""Persist explicit initial equity and cash for local paper accounts.

Local paper execution previously inherited an in-process 100,000 USD default.
That silently invented both capital and currency when an account was missing
configuration. Existing paper accounts must be removed and explicitly
re-onboarded with capital before this revision can proceed; the migration never
guesses their financial state. Live accounts cannot carry local-paper capital.
Downgrade is available only after every paper account has been explicitly
retired or migrated, because removing these fields would discard financial
state.

Revision ID: 0060_paper_account_capital
Revises: 0059_fail_closed_onboarding
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0060_paper_account_capital"
down_revision = "0059_fail_closed_onboarding"
branch_labels = None
depends_on = None


def _lock_accounts() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE linked_broker_accounts IN ACCESS EXCLUSIVE MODE")


def _assert_no_paper_accounts_for_downgrade() -> None:
    bind = op.get_bind()
    paper_accounts = int(
        bind.execute(
            sa.text(
                """
                SELECT COUNT(*)
                FROM linked_broker_accounts
                WHERE environment = 'paper'
                """
            )
        ).scalar_one()
    )
    if paper_accounts:
        msg = (
            "Cannot downgrade explicit paper-account capital while "
            f"{paper_accounts} paper account(s) exist; removing the persisted "
            "capital would make the legacy runtime invent financial state. "
            "Retire or migrate those accounts explicitly before downgrading."
        )
        raise RuntimeError(msg)


def upgrade() -> None:
    op.add_column(
        "linked_broker_accounts",
        sa.Column("paper_initial_equity", sa.Numeric(20, 8)),
    )
    op.add_column(
        "linked_broker_accounts",
        sa.Column("paper_initial_cash", sa.Numeric(20, 8)),
    )
    connection = op.get_bind()
    unresolved = connection.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM linked_broker_accounts
            WHERE environment = 'paper'
              AND (paper_initial_equity IS NULL OR paper_initial_cash IS NULL)
            """
        )
    ).scalar_one()
    if unresolved:
        msg = (
            f"0060 requires explicit capital for {unresolved} existing paper account(s); "
            "remove and re-onboard those accounts with paper_initial_equity and "
            "paper_initial_cash before upgrading"
        )
        raise RuntimeError(msg)

    with op.batch_alter_table("linked_broker_accounts") as batch_op:
        batch_op.create_check_constraint(
            "ck_account_paper_capital_by_environment",
            "(environment = 'paper' AND paper_initial_equity IS NOT NULL "
            "AND paper_initial_cash IS NOT NULL) OR "
            "(environment = 'live' AND paper_initial_equity IS NULL "
            "AND paper_initial_cash IS NULL)",
        )
        batch_op.create_check_constraint(
            "ck_account_paper_equity_positive",
            "paper_initial_equity IS NULL OR paper_initial_equity > 0",
        )
        batch_op.create_check_constraint(
            "ck_account_paper_cash_range",
            "paper_initial_cash IS NULL OR "
            "(paper_initial_cash >= 0 AND paper_initial_cash <= paper_initial_equity)",
        )


def downgrade() -> None:
    _lock_accounts()
    _assert_no_paper_accounts_for_downgrade()

    with op.batch_alter_table("linked_broker_accounts") as batch_op:
        batch_op.drop_constraint("ck_account_paper_cash_range", type_="check")
        batch_op.drop_constraint("ck_account_paper_equity_positive", type_="check")
        batch_op.drop_constraint("ck_account_paper_capital_by_environment", type_="check")
        batch_op.drop_column("paper_initial_cash")
        batch_op.drop_column("paper_initial_equity")
