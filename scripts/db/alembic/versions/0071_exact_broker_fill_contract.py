"""Require complete broker-reported economics for every canonical execution.

Canonical accounting no longer accepts cumulative order-status deltas or
fee-only adjustments. Every row is one exact broker/paper trade with a stable
trade identity, actual timestamp, positive quantity and price, explicit signed
fee amount/currency (including maker rebates), and venue.

Revision ID: 0071_exact_broker_fill
Revises: 0070_canonical_asset_taxonomy
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0071_exact_broker_fill"
down_revision = "0070_canonical_asset_taxonomy"
branch_labels = None
depends_on = None


def _assert_exact_fill_contract() -> None:
    """Refuse to invent economics for legacy execution rows."""
    invalid_count = int(
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT count(*)
                FROM executions
                WHERE fill_ts IS NULL
                   OR qty IS NULL
                   OR qty <= 0
                   OR price IS NULL
                   OR price <= 0
                   OR fee_amount IS NULL
                   OR fee_ccy IS NULL
                   OR trim(fee_ccy) = ''
                   OR venue IS NULL
                   OR trim(venue) = ''
                   OR trade_id IS NULL
                   OR trim(trade_id) = ''
                """
            )
        )
        .scalar_one()
    )
    if invalid_count:
        msg = (
            "Exact-fill migration found "
            f"{invalid_count} incomplete execution rows. Reconcile them from "
            "authoritative broker trade records or remove them before upgrade; "
            "the migration will not infer timestamps, fees, currencies, venues, "
            "or trade identities."
        )
        raise RuntimeError(msg)


def upgrade() -> None:
    _assert_exact_fill_contract()
    with op.batch_alter_table("executions") as batch:
        batch.drop_constraint("ck_execution_incremental_economics", type_="check")
        batch.drop_constraint("ck_execution_nonempty_delta", type_="check")
        batch.drop_constraint("ck_execution_fee_currency", type_="check")
        batch.alter_column(
            "fee_ccy",
            existing_type=sa.String(length=10),
            nullable=False,
        )
        batch.alter_column(
            "venue",
            existing_type=sa.String(length=50),
            nullable=False,
        )
        batch.create_check_constraint(
            "ck_execution_exact_fill_economics",
            "qty > 0 AND price > 0",
        )
        batch.create_check_constraint(
            "ck_execution_fee_currency",
            "nullif(trim(fee_ccy), '') IS NOT NULL",
        )
        batch.create_check_constraint(
            "ck_execution_venue_identity",
            "length(trim(venue)) > 0",
        )


def _assert_downgrade_compatible() -> None:
    rebate_count = int(
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT count(*)
                FROM executions
                WHERE fee_amount < 0
                """
            )
        )
        .scalar_one()
    )
    if rebate_count:
        msg = (
            f"Cannot downgrade exact-fill schema: {rebate_count} execution rows "
            "contain signed maker rebates that the predecessor constraint cannot "
            "represent."
        )
        raise RuntimeError(msg)


def downgrade() -> None:
    _assert_downgrade_compatible()
    with op.batch_alter_table("executions") as batch:
        batch.drop_constraint("ck_execution_venue_identity", type_="check")
        batch.drop_constraint("ck_execution_fee_currency", type_="check")
        batch.drop_constraint("ck_execution_exact_fill_economics", type_="check")
        batch.alter_column(
            "venue",
            existing_type=sa.String(length=50),
            nullable=True,
        )
        batch.alter_column(
            "fee_ccy",
            existing_type=sa.String(length=10),
            nullable=True,
        )
        batch.create_check_constraint(
            "ck_execution_incremental_economics",
            "qty >= 0 AND price > 0 AND fee_amount >= 0",
        )
        batch.create_check_constraint(
            "ck_execution_nonempty_delta",
            "qty > 0 OR fee_amount > 0",
        )
        batch.create_check_constraint(
            "ck_execution_fee_currency",
            "fee_amount = 0 OR nullif(trim(fee_ccy), '') IS NOT NULL",
        )
