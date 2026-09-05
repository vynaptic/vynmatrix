"""Make the account position ledger explicit and retire the parallel options tracker.

The generic ``positions`` ledger is the restart source for broker position
state.  It must preserve whether quantity means assets or contracts and the
exact broker-valued notional used by risk controls.  The retired
``options_positions`` table had no production reader, represented exactly two
legs, and duplicated canonical order/fill state.

Existing rows cannot be backfilled safely because the old schema did not
record contract semantics.  Upgrade therefore refuses to guess or discard
state when either legacy position table is populated.

Revision ID: 0056_position_valuation
Revises: 0055_retire_dormant_schema
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0056_position_valuation"
down_revision = "0055_retire_dormant_schema"
branch_labels = None
depends_on = None


def _assert_legacy_position_state_empty() -> None:
    bind = op.get_bind()
    populated = {
        table: int(bind.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one())
        for table in ("positions", "options_positions")
    }
    populated = {table: count for table, count in populated.items() if count}
    if populated:
        details = ", ".join(f"{table}={count}" for table, count in sorted(populated.items()))
        msg = (
            "Canonical position valuation migration blocked because legacy rows "
            f"cannot be valued without guessing contract semantics: {details}. "
            "Reconcile/export the broker positions, clear the legacy snapshots, "
            "and rerun the migration."
        )
        raise RuntimeError(msg)


def upgrade() -> None:
    _assert_legacy_position_state_empty()

    with op.batch_alter_table("positions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "quantity_unit",
                sa.String(length=20),
                nullable=False,
                server_default="asset",
            )
        )
        batch_op.add_column(sa.Column("contract_multiplier", sa.Numeric(20, 8)))
        batch_op.add_column(sa.Column("gross_notional", sa.Numeric(28, 8)))
        batch_op.add_column(sa.Column("notional_currency", sa.String(length=10)))
        batch_op.create_check_constraint(
            "ck_position_quantity_unit",
            "quantity_unit IN ('asset', 'contracts', 'quote_notional')",
        )
        batch_op.create_check_constraint(
            "ck_position_contract_multiplier",
            "contract_multiplier IS NULL OR contract_multiplier > 0",
        )
        batch_op.create_check_constraint(
            "ck_position_gross_notional",
            "qty = 0 OR (gross_notional IS NOT NULL AND gross_notional > 0)",
        )
        batch_op.create_check_constraint(
            "ck_position_notional_currency",
            "qty = 0 OR notional_currency IS NOT NULL",
        )

    # No CASCADE: any uninventoryed dependency must stop the migration.
    op.drop_table("options_positions")


def _restore_options_positions() -> None:
    op.create_table(
        "options_positions",
        sa.Column("position_id", sa.String(length=50), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=50),
            sa.ForeignKey("users.user_id", name="options_positions_user_id_fkey"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "linked_broker_accounts.account_id",
                name="fk_options_position_account",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "strategy_id",
            sa.String(length=50),
            sa.ForeignKey("strategies.strategy_id", name="options_positions_strategy_id_fkey"),
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
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("close_reason", sa.String(length=100)),
        sa.ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["linked_broker_accounts.account_id", "linked_broker_accounts.user_id"],
            name="fk_options_position_account_owner",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_options_positions_account_id",
        "options_positions",
        ["account_id"],
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "GRANT SELECT, INSERT, UPDATE ON TABLE options_positions TO vm_execution"
        )
        op.execute("ALTER TABLE options_positions ENABLE ROW LEVEL SECURITY")
        for command in ("SELECT", "INSERT", "UPDATE"):
            name = f"options_positions_execution_{command.lower()}"
            if command == "SELECT":
                clause = "FOR SELECT TO vm_execution USING (true)"
            elif command == "INSERT":
                clause = "FOR INSERT TO vm_execution WITH CHECK (true)"
            else:
                clause = (
                    "FOR UPDATE TO vm_execution USING (true) WITH CHECK (true)"
                )
            op.execute(f"CREATE POLICY {name} ON options_positions {clause}")


def downgrade() -> None:
    _restore_options_positions()

    with op.batch_alter_table("positions") as batch_op:
        batch_op.drop_constraint("ck_position_notional_currency", type_="check")
        batch_op.drop_constraint("ck_position_gross_notional", type_="check")
        batch_op.drop_constraint("ck_position_contract_multiplier", type_="check")
        batch_op.drop_constraint("ck_position_quantity_unit", type_="check")
        batch_op.drop_column("notional_currency")
        batch_op.drop_column("gross_notional")
        batch_op.drop_column("contract_multiplier")
        batch_op.drop_column("quantity_unit")
