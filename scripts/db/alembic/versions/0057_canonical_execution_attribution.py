"""Put immutable trade attribution on canonical order intents.

The append-only ``executions`` table is the economic fill ledger.  Its owning
user/account, strategy, side, execution mode, and broker environment are
properties of the parent order intent and must be queryable without reading
the cumulative ``pending_orders`` economics.  Existing attribution is copied
from the one-to-one pending-order link; ambiguous or unattributed intents stop
the migration instead of inventing ownership.

Revision ID: 0057_execution_attribution
Revises: 0056_position_valuation
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0057_execution_attribution"
down_revision = "0056_position_valuation"
branch_labels = None
depends_on = None


def _add_columns() -> None:
    op.add_column(
        "order_intents",
        sa.Column("strategy_id", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "order_intents",
        sa.Column("canonical_signal_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "order_intents",
        sa.Column("side", sa.String(length=10), nullable=True),
    )
    op.add_column(
        "order_intents",
        sa.Column("execution_mode", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "order_intents",
        sa.Column("broker_environment", sa.String(length=20), nullable=True),
    )


def _backfill_postgresql() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        LOCK TABLE
            order_intents,
            orders,
            executions,
            pending_orders,
            linked_broker_accounts,
            canonical_signals
        IN SHARE ROW EXCLUSIVE MODE;

        DO $$
        DECLARE
            ambiguous_count INTEGER;
        BEGIN
            SELECT COUNT(*) INTO ambiguous_count
            FROM (
                SELECT canonical_order.intent_id
                FROM orders canonical_order
                JOIN pending_orders pending
                  ON pending.canonical_order_id = canonical_order.order_id
                JOIN linked_broker_accounts account
                  ON account.account_id = pending.broker_account_id
                 AND account.user_id = pending.user_id
                GROUP BY canonical_order.intent_id
                HAVING
                    COUNT(DISTINCT pending.strategy_id) <> 1
                    OR COUNT(DISTINCT upper(trim(pending.side))) <> 1
                    OR COUNT(DISTINCT lower(trim(pending.execution_mode))) <> 1
                    OR COUNT(
                        DISTINCT lower(
                            COALESCE(
                                nullif(trim(pending.broker_environment), ''),
                                account.environment
                            )
                        )
                    ) <> 1
            ) ambiguous;

            IF ambiguous_count > 0 THEN
                RAISE EXCEPTION
                    'Cannot attribute canonical executions: % order intents have ambiguous lineage',
                    ambiguous_count;
            END IF;
        END $$;

        WITH attribution AS (
            SELECT
                canonical_order.intent_id,
                min(pending.strategy_id) AS strategy_id,
                min(upper(trim(pending.side))) AS side,
                min(lower(trim(pending.execution_mode))) AS execution_mode,
                min(
                    lower(
                        COALESCE(
                            nullif(trim(pending.broker_environment), ''),
                            account.environment
                        )
                    )
                ) AS broker_environment,
                min(signal.signal_id) AS canonical_signal_id
            FROM orders canonical_order
            JOIN pending_orders pending
              ON pending.canonical_order_id = canonical_order.order_id
            JOIN linked_broker_accounts account
              ON account.account_id = pending.broker_account_id
             AND account.user_id = pending.user_id
            LEFT JOIN canonical_signals signal
              ON signal.external_signal_id = pending.signal_id
             AND signal.strategy_id = pending.strategy_id
            GROUP BY canonical_order.intent_id
        )
        UPDATE order_intents intent
        SET
            strategy_id = attribution.strategy_id,
            canonical_signal_id = attribution.canonical_signal_id,
            side = attribution.side,
            execution_mode = attribution.execution_mode,
            broker_environment = attribution.broker_environment
        FROM attribution
        WHERE intent.intent_id = attribution.intent_id;

        DO $$
        DECLARE
            invalid_economic_count INTEGER;
            invalid_intent_count INTEGER;
        BEGIN
            SELECT COUNT(*) INTO invalid_economic_count
            FROM executions execution
            JOIN orders canonical_order
              ON canonical_order.order_id = execution.order_id
            JOIN order_intents intent
              ON intent.intent_id = canonical_order.intent_id
            WHERE
                execution.instr_id IS NULL
                OR execution.qty < 0
                OR execution.price <= 0
                OR execution.fee_amount IS NULL
                OR execution.fee_amount < 0
                OR (execution.qty = 0 AND execution.fee_amount = 0)
                OR (
                    execution.fee_amount > 0
                    AND nullif(trim(execution.fee_ccy), '') IS NULL
                )
                OR intent.strategy_id IS NULL
                OR intent.side NOT IN ('BUY', 'SELL')
                OR nullif(trim(intent.execution_mode), '') IS NULL
                OR intent.broker_environment NOT IN ('paper', 'live');

            IF invalid_economic_count > 0 THEN
                RAISE EXCEPTION
                    'Cannot attribute canonical executions: % economic fills lack complete lineage',
                    invalid_economic_count;
            END IF;

            SELECT COUNT(*) INTO invalid_intent_count
            FROM order_intents
            WHERE
                strategy_id IS NULL
                OR side NOT IN ('BUY', 'SELL')
                OR nullif(trim(execution_mode), '') IS NULL
                OR broker_environment NOT IN ('paper', 'live');

            IF invalid_intent_count > 0 THEN
                RAISE EXCEPTION
                    'Cannot enforce canonical order attribution: '
                    '% intents lack one pending-order lineage',
                    invalid_intent_count;
            END IF;
        END $$;
        """
    )


def _enforce_schema() -> None:
    with op.batch_alter_table("order_intents") as batch_op:
        batch_op.alter_column(
            "strategy_id",
            existing_type=sa.String(length=50),
            nullable=False,
        )
        batch_op.alter_column(
            "side",
            existing_type=sa.String(length=10),
            nullable=False,
        )
        batch_op.alter_column(
            "execution_mode",
            existing_type=sa.String(length=30),
            nullable=False,
        )
        batch_op.alter_column(
            "broker_environment",
            existing_type=sa.String(length=20),
            nullable=False,
        )
        batch_op.create_foreign_key(
            "fk_order_intent_strategy",
            "strategies",
            ["strategy_id"],
            ["strategy_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_order_intent_canonical_signal",
            "canonical_signals",
            ["canonical_signal_id"],
            ["signal_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_order_intent_side",
            "side IN ('BUY', 'SELL')",
        )
        batch_op.create_check_constraint(
            "ck_order_intent_execution_mode",
            "length(trim(execution_mode)) > 0",
        )
        batch_op.create_check_constraint(
            "ck_order_intent_broker_environment",
            "broker_environment IN ('paper', 'live')",
        )

    op.create_index(
        "ix_order_intents_canonical_signal_id",
        "order_intents",
        ["canonical_signal_id"],
    )
    op.create_index(
        "ix_order_intents_owner_strategy_environment",
        "order_intents",
        ["user_id", "account_id", "strategy_id", "broker_environment"],
    )

    with op.batch_alter_table("executions") as batch_op:
        batch_op.alter_column(
            "instr_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch_op.alter_column(
            "fee_amount",
            existing_type=sa.Numeric(20, 8),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_execution_incremental_economics",
            "qty >= 0 AND price > 0 AND fee_amount >= 0",
        )
        batch_op.create_check_constraint(
            "ck_execution_nonempty_delta",
            "qty > 0 OR fee_amount > 0",
        )
        batch_op.create_check_constraint(
            "ck_execution_fee_currency",
            "fee_amount = 0 OR nullif(trim(fee_ccy), '') IS NOT NULL",
        )


def upgrade() -> None:
    _add_columns()
    if op.get_bind().dialect.name == "postgresql":
        _backfill_postgresql()
    _enforce_schema()


def downgrade() -> None:
    with op.batch_alter_table("executions") as batch_op:
        batch_op.drop_constraint("ck_execution_fee_currency", type_="check")
        batch_op.drop_constraint("ck_execution_nonempty_delta", type_="check")
        batch_op.drop_constraint(
            "ck_execution_incremental_economics",
            type_="check",
        )
        batch_op.alter_column(
            "fee_amount",
            existing_type=sa.Numeric(20, 8),
            nullable=True,
        )
        batch_op.alter_column(
            "instr_id",
            existing_type=sa.Integer(),
            nullable=True,
        )

    op.drop_index(
        "ix_order_intents_owner_strategy_environment",
        table_name="order_intents",
    )
    op.drop_index(
        "ix_order_intents_canonical_signal_id",
        table_name="order_intents",
    )
    with op.batch_alter_table("order_intents") as batch_op:
        batch_op.drop_constraint(
            "ck_order_intent_broker_environment",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_order_intent_execution_mode",
            type_="check",
        )
        batch_op.drop_constraint("ck_order_intent_side", type_="check")
        batch_op.drop_constraint(
            "fk_order_intent_canonical_signal",
            type_="foreignkey",
        )
        batch_op.drop_constraint("fk_order_intent_strategy", type_="foreignkey")
        batch_op.drop_column("broker_environment")
        batch_op.drop_column("execution_mode")
        batch_op.drop_column("side")
        batch_op.drop_column("canonical_signal_id")
        batch_op.drop_column("strategy_id")
