"""Carry linked broker-account identity through execution and feedback.

Revision ID: 0051_account_execution_scope
Revises: 0050_retire_vol_reversal_v1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0051_account_execution_scope"
down_revision = "0050_retire_vol_reversal_v1"
branch_labels = None
depends_on = None


def _add_columns() -> None:
    op.add_column(
        "instruments",
        sa.Column("settlement_currency", sa.String(length=10), nullable=True),
    )
    op.add_column(
        "user_strategy_bindings",
        sa.Column("broker_account_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "managed_secrets",
        sa.Column("account_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "pending_orders",
        sa.Column("broker_account_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "pending_orders",
        sa.Column("canonical_order_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "pending_orders",
        sa.Column("commission_currency", sa.String(length=10), nullable=True),
    )
    op.add_column(
        "pending_orders",
        sa.Column("settlement_currency", sa.String(length=10), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("settlement_currency", sa.String(length=10), nullable=True),
    )
    op.add_column(
        "execution_logs",
        sa.Column("account_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "execution_metrics",
        sa.Column("account_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "execution_metrics",
        sa.Column("commission_currency", sa.String(length=10), nullable=True),
    )
    op.add_column(
        "options_positions",
        sa.Column("account_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "mode_performance",
        sa.Column("account_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "mode_performance",
        sa.Column("strategy_id", sa.String(length=50), nullable=True),
    )


def _backfill_postgresql() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        LOCK TABLE
            instruments,
            linked_broker_accounts,
            broker_credentials,
            managed_secrets,
            user_strategy_bindings,
            pending_orders,
            orders,
            execution_logs,
            execution_metrics,
            options_positions,
            mode_performance
        IN SHARE ROW EXCLUSIVE MODE;

        DO $$
        DECLARE
            invalid_secret_count INTEGER;
        BEGIN
            SELECT COUNT(*) INTO invalid_secret_count
            FROM (
                SELECT secret.secret_ref
                FROM managed_secrets secret
                LEFT JOIN broker_credentials credential
                  ON credential.secret_ref = secret.secret_ref
                GROUP BY secret.secret_ref
                HAVING COUNT(DISTINCT credential.account_id) <> 1
            ) invalid;

            IF invalid_secret_count > 0 THEN
                RAISE EXCEPTION
                    'Cannot scope secrets: % refs lack one broker account',
                    invalid_secret_count;
            END IF;
        END $$;

        UPDATE managed_secrets secret
        SET account_id = credential.account_id
        FROM broker_credentials credential
        WHERE credential.secret_ref = secret.secret_ref;

        UPDATE instruments
        SET settlement_currency = CASE
            WHEN upper(translate(canonical, '/-_', '')) LIKE '%USDC' THEN 'USDC'
            WHEN upper(translate(canonical, '/-_', '')) LIKE '%USDT' THEN 'USDT'
            WHEN upper(translate(canonical, '/-_', '')) LIKE '%USD' THEN 'USD'
            WHEN upper(translate(canonical, '/-_', '')) LIKE '%EUR' THEN 'EUR'
            WHEN upper(translate(canonical, '/-_', '')) LIKE '%GBP' THEN 'GBP'
            WHEN upper(translate(canonical, '/-_', '')) LIKE '%INR' THEN 'INR'
            WHEN upper(exchange) IN ('NYSE', 'NASDAQ') THEN 'USD'
            WHEN upper(exchange) IN ('NSE', 'BSE') THEN 'INR'
            ELSE NULL
        END
        WHERE settlement_currency IS NULL;

        DO $$
        DECLARE
            unknown_instrument_count INTEGER;
        BEGIN
            SELECT COUNT(*) INTO unknown_instrument_count
            FROM instruments
            WHERE settlement_currency IS NULL;

            IF unknown_instrument_count > 0 THEN
                RAISE EXCEPTION
                    'Cannot infer settlement currency for % instruments',
                    unknown_instrument_count;
            END IF;
        END $$;

        WITH candidates AS (
            SELECT
                binding.binding_id,
                account.account_id,
                COUNT(*) OVER (PARTITION BY binding.binding_id) AS candidate_count
            FROM user_strategy_bindings binding
            JOIN linked_broker_accounts account
              ON account.user_id = binding.user_id
             AND account.status = 'connected'
            JOIN brokers broker
              ON broker.broker_id = account.broker_id
            WHERE
                binding.broker_account_id IS NULL
                AND (
                    binding.allowed_brokers IS NULL
                    OR binding.allowed_brokers::JSONB = '[]'::JSONB
                    OR binding.allowed_brokers::JSONB ? broker.code
                )
        )
        UPDATE user_strategy_bindings binding
        SET broker_account_id = candidate.account_id
        FROM candidates candidate
        WHERE
            candidate.binding_id = binding.binding_id
            AND candidate.candidate_count = 1;

        DO $$
        DECLARE
            unsafe_binding_count INTEGER;
            duplicate_wildcard_count INTEGER;
        BEGIN
            SELECT COUNT(*) INTO unsafe_binding_count
            FROM user_strategy_bindings
            WHERE broker_account_id IS NULL;

            IF unsafe_binding_count > 0 THEN
                RAISE EXCEPTION
                    'Cannot scope bindings: % lack one connected broker account',
                    unsafe_binding_count;
            END IF;

            SELECT COUNT(*) INTO duplicate_wildcard_count
            FROM (
                SELECT user_id, broker_account_id
                FROM user_strategy_bindings
                WHERE strategy_id IS NULL
                GROUP BY user_id, broker_account_id
                HAVING COUNT(*) > 1
            ) duplicates;

            IF duplicate_wildcard_count > 0 THEN
                RAISE EXCEPTION
                    'Cannot scope bindings: % duplicate wildcard account groups',
                    duplicate_wildcard_count;
            END IF;
        END $$;

        WITH candidates AS (
            SELECT
                pending.order_id,
                account.account_id,
                COUNT(*) OVER (PARTITION BY pending.order_id) AS candidate_count
            FROM pending_orders pending
            JOIN linked_broker_accounts account
              ON account.user_id = pending.user_id
            JOIN brokers broker
              ON broker.broker_id = account.broker_id
            LEFT JOIN broker_credentials credential
              ON credential.account_id = account.account_id
             AND credential.secret_ref = pending.credential_ref
            WHERE
                pending.broker_account_id IS NULL
                AND (
                    credential.cred_id IS NOT NULL
                    OR (
                        pending.credential_ref IS NULL
                        AND lower(broker.code) = lower(pending.broker)
                    )
                )
        )
        UPDATE pending_orders pending
        SET broker_account_id = candidate.account_id
        FROM candidates candidate
        WHERE
            candidate.order_id = pending.order_id
            AND candidate.candidate_count = 1;

        UPDATE pending_orders pending
        SET settlement_currency = instrument.settlement_currency
        FROM instruments instrument
        WHERE
            pending.settlement_currency IS NULL
            AND pending.instr_id = instrument.instr_id;

        UPDATE pending_orders
        SET settlement_currency = CASE
            WHEN upper(translate(symbol, '/-_', '')) LIKE '%USDC' THEN 'USDC'
            WHEN upper(translate(symbol, '/-_', '')) LIKE '%USDT' THEN 'USDT'
            WHEN upper(translate(symbol, '/-_', '')) LIKE '%USD' THEN 'USD'
            WHEN upper(translate(symbol, '/-_', '')) LIKE '%EUR' THEN 'EUR'
            WHEN upper(translate(symbol, '/-_', '')) LIKE '%GBP' THEN 'GBP'
            WHEN upper(translate(symbol, '/-_', '')) LIKE '%INR' THEN 'INR'
            ELSE NULL
        END
        WHERE settlement_currency IS NULL;

        UPDATE orders target_order
        SET settlement_currency = instrument.settlement_currency
        FROM order_intents intent
        JOIN instruments instrument
          ON upper(translate(instrument.canonical, '/-_', ''))
           = upper(translate(intent.payload->>'symbol', '/-_', ''))
        WHERE
            target_order.intent_id = intent.intent_id
            AND target_order.settlement_currency IS NULL;

        DO $$
        DECLARE
            unsafe_pending_count INTEGER;
            mismatched_order_account_count INTEGER;
        BEGIN
            SELECT
                (SELECT COUNT(*) FROM pending_orders
                 WHERE broker_account_id IS NULL OR settlement_currency IS NULL)
                + (SELECT COUNT(*) FROM orders WHERE settlement_currency IS NULL)
            INTO unsafe_pending_count;

            IF unsafe_pending_count > 0 THEN
                RAISE EXCEPTION
                    'Cannot scope execution: % orders lack account or currency',
                    unsafe_pending_count;
            END IF;

            SELECT COUNT(*) INTO mismatched_order_account_count
            FROM orders canonical_order
            JOIN order_intents intent
              ON intent.intent_id = canonical_order.intent_id
            WHERE canonical_order.account_id <> intent.account_id;

            IF mismatched_order_account_count > 0 THEN
                RAISE EXCEPTION
                    'Cannot scope canonical orders: % intent/account mismatches',
                    mismatched_order_account_count;
            END IF;
        END $$;

        WITH run_accounts AS (
            SELECT run_id, user_id, MIN(broker_account_id) AS account_id
            FROM pending_orders
            WHERE run_id IS NOT NULL AND broker_account_id IS NOT NULL
            GROUP BY run_id, user_id
            HAVING COUNT(DISTINCT broker_account_id) = 1
        )
        UPDATE execution_logs log
        SET account_id = run_account.account_id
        FROM run_accounts run_account
        WHERE
            log.account_id IS NULL
            AND log.run_id = run_account.run_id
            AND log.user_id = run_account.user_id;

        WITH run_accounts AS (
            SELECT run_id, user_id, MIN(broker_account_id) AS account_id
            FROM pending_orders
            WHERE run_id IS NOT NULL AND broker_account_id IS NOT NULL
            GROUP BY run_id, user_id
            HAVING COUNT(DISTINCT broker_account_id) = 1
        )
        UPDATE execution_metrics metric
        SET account_id = run_account.account_id
        FROM run_accounts run_account
        WHERE
            metric.account_id IS NULL
            AND metric.run_id = run_account.run_id
            AND metric.user_id = run_account.user_id;

        WITH candidates AS (
            SELECT
                position.position_id,
                account.account_id,
                COUNT(*) OVER (PARTITION BY position.position_id) AS candidate_count
            FROM options_positions position
            JOIN linked_broker_accounts account
              ON account.user_id = position.user_id
        )
        UPDATE options_positions position
        SET account_id = candidate.account_id
        FROM candidates candidate
        WHERE
            candidate.position_id = position.position_id
            AND candidate.candidate_count = 1;

        DO $$
        DECLARE
            unsafe_option_count INTEGER;
        BEGIN
            SELECT
                (SELECT COUNT(*) FROM execution_logs WHERE account_id IS NULL)
                + (SELECT COUNT(*) FROM execution_metrics WHERE account_id IS NULL)
                + (SELECT COUNT(*) FROM options_positions WHERE account_id IS NULL)
            INTO unsafe_option_count;

            IF unsafe_option_count > 0 THEN
                RAISE EXCEPTION
                    'Cannot scope execution history: % rows lack one account',
                    unsafe_option_count;
            END IF;
        END $$;

        -- This table is a rebuildable aggregate, not the execution source of
        -- record. Existing rows mix tenants and signal-direction returns, so
        -- retaining them under an invented account would be materially wrong.
        DELETE FROM mode_performance;
        """
    )


def _create_constraints() -> None:
    op.create_unique_constraint(
        "uq_linked_broker_account_owner",
        "linked_broker_accounts",
        ["account_id", "user_id"],
    )
    op.drop_constraint(
        "uq_user_strategy_binding",
        "user_strategy_bindings",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_user_strategy_binding_account",
        "user_strategy_bindings",
        ["user_id", "strategy_id", "broker_account_id"],
    )
    op.create_foreign_key(
        "fk_binding_broker_account",
        "user_strategy_bindings",
        "linked_broker_accounts",
        ["broker_account_id"],
        ["account_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_binding_broker_account_owner",
        "user_strategy_bindings",
        "linked_broker_accounts",
        ["broker_account_id", "user_id"],
        ["account_id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_managed_secret_account",
        "managed_secrets",
        "linked_broker_accounts",
        ["account_id"],
        ["account_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_pending_order_broker_account",
        "pending_orders",
        "linked_broker_accounts",
        ["broker_account_id"],
        ["account_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_pending_order_broker_account_owner",
        "pending_orders",
        "linked_broker_accounts",
        ["broker_account_id", "user_id"],
        ["account_id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_order_intent_account_owner",
        "order_intents",
        "linked_broker_accounts",
        ["account_id", "user_id"],
        ["account_id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_order_intent_account",
        "order_intents",
        ["intent_id", "account_id"],
    )
    op.create_foreign_key(
        "fk_order_intent_account_match",
        "orders",
        "order_intents",
        ["intent_id", "account_id"],
        ["intent_id", "account_id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_order_account",
        "orders",
        ["order_id", "account_id"],
    )
    op.create_foreign_key(
        "fk_pending_order_canonical_order",
        "pending_orders",
        "orders",
        ["canonical_order_id"],
        ["order_id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_pending_order_canonical_order",
        "pending_orders",
        ["canonical_order_id"],
    )
    op.create_foreign_key(
        "fk_pending_order_account_match",
        "pending_orders",
        "orders",
        ["canonical_order_id", "broker_account_id"],
        ["order_id", "account_id"],
    )
    op.create_unique_constraint(
        "uq_execution_order_trade",
        "executions",
        ["order_id", "trade_id"],
    )
    op.create_foreign_key(
        "fk_execution_log_account",
        "execution_logs",
        "linked_broker_accounts",
        ["account_id"],
        ["account_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_execution_log_account_owner",
        "execution_logs",
        "linked_broker_accounts",
        ["account_id", "user_id"],
        ["account_id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_execution_metric_account",
        "execution_metrics",
        "linked_broker_accounts",
        ["account_id"],
        ["account_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_execution_metric_account_owner",
        "execution_metrics",
        "linked_broker_accounts",
        ["account_id", "user_id"],
        ["account_id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_options_position_account",
        "options_positions",
        "linked_broker_accounts",
        ["account_id"],
        ["account_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_options_position_account_owner",
        "options_positions",
        "linked_broker_accounts",
        ["account_id", "user_id"],
        ["account_id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_mode_performance_account",
        "mode_performance",
        "linked_broker_accounts",
        ["account_id"],
        ["account_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_mode_performance_strategy",
        "mode_performance",
        "strategies",
        ["strategy_id"],
        ["strategy_id"],
        ondelete="CASCADE",
    )
    op.alter_column(
        "instruments",
        "settlement_currency",
        existing_type=sa.String(length=10),
        nullable=False,
    )
    op.alter_column(
        "user_strategy_bindings",
        "broker_account_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.alter_column(
        "pending_orders",
        "broker_account_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.alter_column(
        "pending_orders",
        "settlement_currency",
        existing_type=sa.String(length=10),
        nullable=False,
    )
    op.alter_column(
        "orders",
        "settlement_currency",
        existing_type=sa.String(length=10),
        nullable=False,
    )
    op.alter_column(
        "managed_secrets",
        "account_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.alter_column(
        "execution_logs",
        "account_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.alter_column(
        "execution_metrics",
        "account_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.alter_column(
        "options_positions",
        "account_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.alter_column(
        "mode_performance",
        "account_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.alter_column(
        "mode_performance",
        "strategy_id",
        existing_type=sa.String(length=50),
        nullable=False,
    )


def _create_indexes() -> None:
    op.create_index(
        "ix_user_strategy_bindings_broker_account_id",
        "user_strategy_bindings",
        ["broker_account_id"],
        unique=False,
    )
    op.create_index(
        "uq_user_strategy_binding_wildcard_account",
        "user_strategy_bindings",
        ["user_id", "broker_account_id"],
        unique=True,
        postgresql_where=sa.text("strategy_id IS NULL"),
        sqlite_where=sa.text("strategy_id IS NULL"),
    )
    op.create_index(
        "ix_managed_secrets_account_id",
        "managed_secrets",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        "ix_pending_orders_broker_account_id",
        "pending_orders",
        ["broker_account_id"],
        unique=False,
    )
    op.create_index(
        "ix_execution_logs_account_id",
        "execution_logs",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        "ix_execution_metrics_account_id",
        "execution_metrics",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        "ix_options_positions_account_id",
        "options_positions",
        ["account_id"],
        unique=False,
    )
    op.drop_index("ix_mode_perf_lookup", table_name="mode_performance")
    op.create_index(
        "ix_mode_perf_lookup",
        "mode_performance",
        ["account_id", "strategy_id", "instr_id", "execution_mode", "horizon"],
        unique=False,
    )


def upgrade() -> None:
    _add_columns()
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        _backfill_postgresql()
        _create_constraints()
    else:
        # SQLite is used only for empty-schema unit fixtures. Batch mode is
        # required because SQLite cannot add FK/check constraints in place.
        op.execute("DELETE FROM mode_performance")
        for table in ("instruments", "pending_orders", "orders"):
            with op.batch_alter_table(table) as batch:
                batch.alter_column(
                    "settlement_currency",
                    existing_type=sa.String(length=10),
                    nullable=False,
                )
        with op.batch_alter_table("linked_broker_accounts") as batch:
            batch.create_unique_constraint(
                "uq_linked_broker_account_owner",
                ["account_id", "user_id"],
            )
        with op.batch_alter_table("managed_secrets") as batch:
            batch.alter_column("account_id", existing_type=sa.BigInteger(), nullable=False)
            batch.create_foreign_key(
                "fk_managed_secret_account",
                "linked_broker_accounts",
                ["account_id"],
                ["account_id"],
                ondelete="CASCADE",
            )
        with op.batch_alter_table("mode_performance") as batch:
            batch.alter_column("account_id", existing_type=sa.BigInteger(), nullable=False)
            batch.alter_column(
                "strategy_id",
                existing_type=sa.String(length=50),
                nullable=False,
            )
            batch.create_foreign_key(
                "fk_mode_performance_account",
                "linked_broker_accounts",
                ["account_id"],
                ["account_id"],
                ondelete="CASCADE",
            )
            batch.create_foreign_key(
                "fk_mode_performance_strategy",
                "strategies",
                ["strategy_id"],
                ["strategy_id"],
                ondelete="CASCADE",
            )
        with op.batch_alter_table("pending_orders") as batch:
            batch.alter_column(
                "broker_account_id",
                existing_type=sa.BigInteger(),
                nullable=False,
            )
        for table, column in (
            ("user_strategy_bindings", "broker_account_id"),
            ("execution_logs", "account_id"),
            ("execution_metrics", "account_id"),
            ("options_positions", "account_id"),
        ):
            with op.batch_alter_table(table) as batch:
                batch.alter_column(
                    column,
                    existing_type=sa.BigInteger(),
                    nullable=False,
                )
        for table, name, source, target, ondelete in (
            (
                "user_strategy_bindings",
                "fk_binding_broker_account",
                "broker_account_id",
                "account_id",
                "RESTRICT",
            ),
            (
                "pending_orders",
                "fk_pending_order_broker_account",
                "broker_account_id",
                "account_id",
                "RESTRICT",
            ),
            (
                "execution_logs",
                "fk_execution_log_account",
                "account_id",
                "account_id",
                "RESTRICT",
            ),
            (
                "execution_metrics",
                "fk_execution_metric_account",
                "account_id",
                "account_id",
                "RESTRICT",
            ),
            (
                "options_positions",
                "fk_options_position_account",
                "account_id",
                "account_id",
                "RESTRICT",
            ),
        ):
            with op.batch_alter_table(table) as batch:
                batch.create_foreign_key(
                    name,
                    "linked_broker_accounts",
                    [source],
                    [target],
                    ondelete=ondelete,
                )
        for table, name, account_column in (
            (
                "user_strategy_bindings",
                "fk_binding_broker_account_owner",
                "broker_account_id",
            ),
            (
                "pending_orders",
                "fk_pending_order_broker_account_owner",
                "broker_account_id",
            ),
            (
                "order_intents",
                "fk_order_intent_account_owner",
                "account_id",
            ),
            (
                "execution_logs",
                "fk_execution_log_account_owner",
                "account_id",
            ),
            (
                "execution_metrics",
                "fk_execution_metric_account_owner",
                "account_id",
            ),
            (
                "options_positions",
                "fk_options_position_account_owner",
                "account_id",
            ),
        ):
            with op.batch_alter_table(table) as batch:
                batch.create_foreign_key(
                    name,
                    "linked_broker_accounts",
                    [account_column, "user_id"],
                    ["account_id", "user_id"],
                    ondelete="RESTRICT",
                )
        with op.batch_alter_table("pending_orders") as batch:
            batch.create_foreign_key(
                "fk_pending_order_canonical_order",
                "orders",
                ["canonical_order_id"],
                ["order_id"],
                ondelete="SET NULL",
            )
            batch.create_unique_constraint(
                "uq_pending_order_canonical_order",
                ["canonical_order_id"],
            )
        with op.batch_alter_table("order_intents") as batch:
            batch.create_unique_constraint(
                "uq_order_intent_account",
                ["intent_id", "account_id"],
            )
        with op.batch_alter_table("orders") as batch:
            batch.create_foreign_key(
                "fk_order_intent_account_match",
                "order_intents",
                ["intent_id", "account_id"],
                ["intent_id", "account_id"],
                ondelete="CASCADE",
            )
            batch.create_unique_constraint(
                "uq_order_account",
                ["order_id", "account_id"],
            )
        with op.batch_alter_table("pending_orders") as batch:
            batch.create_foreign_key(
                "fk_pending_order_account_match",
                "orders",
                ["canonical_order_id", "broker_account_id"],
                ["order_id", "account_id"],
            )
        with op.batch_alter_table("executions") as batch:
            batch.create_unique_constraint(
                "uq_execution_order_trade",
                ["order_id", "trade_id"],
            )
        with op.batch_alter_table("user_strategy_bindings") as batch:
            batch.drop_constraint("uq_user_strategy_binding", type_="unique")
            batch.create_unique_constraint(
                "uq_user_strategy_binding_account",
                ["user_id", "strategy_id", "broker_account_id"],
            )
    _create_indexes()


def downgrade() -> None:
    op.drop_index(
        "uq_user_strategy_binding_wildcard_account",
        table_name="user_strategy_bindings",
    )
    op.drop_index("ix_mode_perf_lookup", table_name="mode_performance")
    op.create_index(
        "ix_mode_perf_lookup",
        "mode_performance",
        ["instr_id", "execution_mode", "horizon"],
        unique=False,
    )
    for index_name, table in (
        ("ix_options_positions_account_id", "options_positions"),
        ("ix_execution_metrics_account_id", "execution_metrics"),
        ("ix_execution_logs_account_id", "execution_logs"),
        ("ix_pending_orders_broker_account_id", "pending_orders"),
        ("ix_managed_secrets_account_id", "managed_secrets"),
        ("ix_user_strategy_bindings_broker_account_id", "user_strategy_bindings"),
    ):
        op.drop_index(index_name, table_name=table)

    constraints = (
        ("user_strategy_bindings", "uq_user_strategy_binding_account", "unique"),
        ("pending_orders", "fk_pending_order_account_match", "foreignkey"),
        ("orders", "fk_order_intent_account_match", "foreignkey"),
        ("orders", "uq_order_account", "unique"),
        ("order_intents", "uq_order_intent_account", "unique"),
        (
            "user_strategy_bindings",
            "fk_binding_broker_account_owner",
            "foreignkey",
        ),
        ("mode_performance", "fk_mode_performance_strategy", "foreignkey"),
        ("mode_performance", "fk_mode_performance_account", "foreignkey"),
        ("options_positions", "fk_options_position_account_owner", "foreignkey"),
        ("options_positions", "fk_options_position_account", "foreignkey"),
        ("execution_metrics", "fk_execution_metric_account_owner", "foreignkey"),
        ("execution_metrics", "fk_execution_metric_account", "foreignkey"),
        ("execution_logs", "fk_execution_log_account_owner", "foreignkey"),
        ("execution_logs", "fk_execution_log_account", "foreignkey"),
        ("order_intents", "fk_order_intent_account_owner", "foreignkey"),
        ("pending_orders", "uq_pending_order_canonical_order", "unique"),
        ("executions", "uq_execution_order_trade", "unique"),
        ("pending_orders", "fk_pending_order_canonical_order", "foreignkey"),
        (
            "pending_orders",
            "fk_pending_order_broker_account_owner",
            "foreignkey",
        ),
        ("pending_orders", "fk_pending_order_broker_account", "foreignkey"),
        ("managed_secrets", "fk_managed_secret_account", "foreignkey"),
        ("user_strategy_bindings", "fk_binding_broker_account", "foreignkey"),
    )
    if op.get_bind().dialect.name == "sqlite":
        for table, name, kind in constraints:
            with op.batch_alter_table(table) as batch:
                batch.drop_constraint(name, type_=kind)
    else:
        for table, name, kind in constraints:
            op.drop_constraint(name, table, type_=kind)
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("linked_broker_accounts") as batch:
            batch.drop_constraint("uq_linked_broker_account_owner", type_="unique")
    else:
        op.drop_constraint(
            "uq_linked_broker_account_owner",
            "linked_broker_accounts",
            type_="unique",
        )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("user_strategy_bindings") as batch:
            batch.create_unique_constraint(
                "uq_user_strategy_binding",
                ["user_id", "strategy_id"],
            )
    else:
        op.create_unique_constraint(
            "uq_user_strategy_binding",
            "user_strategy_bindings",
            ["user_id", "strategy_id"],
        )

    for table, column in (
        ("mode_performance", "strategy_id"),
        ("mode_performance", "account_id"),
        ("options_positions", "account_id"),
        ("execution_metrics", "commission_currency"),
        ("execution_metrics", "account_id"),
        ("execution_logs", "account_id"),
        ("pending_orders", "commission_currency"),
        ("orders", "settlement_currency"),
        ("pending_orders", "settlement_currency"),
        ("pending_orders", "canonical_order_id"),
        ("pending_orders", "broker_account_id"),
        ("managed_secrets", "account_id"),
        ("user_strategy_bindings", "broker_account_id"),
        ("instruments", "settlement_currency"),
    ):
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table(table) as batch:
                batch.drop_column(column)
        else:
            op.drop_column(table, column)
