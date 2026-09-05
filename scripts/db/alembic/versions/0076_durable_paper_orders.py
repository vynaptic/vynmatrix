"""Persist durable local-paper order lifecycle state.

Revision ID: 0076_durable_paper_orders
Revises: 0075_durable_strategy_runtime
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0076_durable_paper_orders"
down_revision = "0075_durable_strategy_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pending_orders", sa.Column("client_order_id", sa.String(100)))
    op.add_column("pending_orders", sa.Column("trigger_price", sa.Numeric(20, 8)))
    op.add_column("pending_orders", sa.Column("purpose", sa.String(30)))
    op.add_column(
        "pending_orders",
        sa.Column("time_in_force", sa.String(10), nullable=False, server_default="gtc"),
    )
    op.add_column(
        "pending_orders",
        sa.Column("reduce_only", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("pending_orders", sa.Column("parent_order_id", sa.String(100)))
    op.add_column("pending_orders", sa.Column("oco_group_id", sa.String(100)))
    op.add_column(
        "pending_orders",
        sa.Column(
            "cumulative_filled_quantity",
            sa.Numeric(20, 8),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column("pending_orders", sa.Column("market_data_source", sa.String(50)))
    op.add_column("pending_orders", sa.Column("market_data_timeframe", sa.String(20)))
    op.add_column(
        "pending_orders",
        sa.Column("last_market_data_ts", sa.DateTime(timezone=True)),
    )
    op.add_column("pending_orders", sa.Column("last_market_data_revision", sa.Integer()))
    op.add_column(
        "pending_orders",
        sa.Column(
            "trigger_policy_version",
            sa.String(40),
            nullable=False,
            server_default="ohlc-conservative-v1",
        ),
    )
    op.execute(
        "UPDATE pending_orders SET client_order_id = order_id, "
        "cumulative_filled_quantity = COALESCE(filled_quantity, 0)"
    )

    op.add_column("orders", sa.Column("client_order_id", sa.String(100)))
    bind = op.get_bind()
    intents = sa.table(
        "order_intents",
        sa.column("intent_id", sa.BigInteger()),
        sa.column("payload", sa.JSON()),
    )
    orders = sa.table(
        "orders",
        sa.column("order_id", sa.BigInteger()),
        sa.column("intent_id", sa.BigInteger()),
        sa.column("client_order_id", sa.String()),
    )
    payloads = {
        int(intent_id): payload
        for intent_id, payload in bind.execute(sa.select(intents.c.intent_id, intents.c.payload))
    }
    for order_id, intent_id in bind.execute(sa.select(orders.c.order_id, orders.c.intent_id)):
        payload = payloads.get(int(intent_id))
        local_id = payload.get("local_order_id") if isinstance(payload, dict) else None
        client_order_id = str(local_id or f"canonical:{order_id}").strip()
        bind.execute(
            orders.update()
            .where(orders.c.order_id == order_id)
            .values(client_order_id=client_order_id)
        )

    op.add_column(
        "executions",
        sa.Column("source_price_id", sa.BigInteger()),
    )
    op.add_column("executions", sa.Column("source_content_revision", sa.Integer()))
    op.add_column("executions", sa.Column("trigger_policy_version", sa.String(40)))

    with op.batch_alter_table("pending_orders") as batch:
        batch.alter_column("client_order_id", nullable=False)
        batch.drop_constraint("ck_pending_order_status", type_="check")
        batch.create_check_constraint(
            "ck_pending_order_status",
            "status IN ('pending', 'submission_unknown', 'submitted', 'working', "
            "'filled', 'partially_filled', 'cancelled', 'expired', 'rejected')",
        )
        batch.create_check_constraint(
            "ck_pending_order_client_order_id",
            "length(trim(client_order_id)) > 0",
        )
        batch.create_check_constraint(
            "ck_pending_order_cumulative_fill",
            "quantity > 0 AND cumulative_filled_quantity >= 0 "
            "AND cumulative_filled_quantity <= quantity",
        )
        batch.create_check_constraint(
            "ck_pending_order_market_watermark",
            "(last_market_data_ts IS NULL AND last_market_data_revision IS NULL) "
            "OR (last_market_data_ts IS NOT NULL AND last_market_data_revision IS NOT NULL)",
        )
        batch.create_unique_constraint(
            "uq_pending_order_account_client_order_id",
            ["broker_account_id", "client_order_id"],
        )
        batch.create_index("ix_pending_orders_oco_group_id", ["oco_group_id"])

    with op.batch_alter_table("orders") as batch:
        batch.alter_column("client_order_id", nullable=False)
        batch.drop_constraint("ck_order_state", type_="check")
        batch.create_check_constraint(
            "ck_order_state",
            "state IN ('new', 'submission_unknown', 'working', 'partially_filled', "
            "'filled', 'canceled', 'rejected')",
        )
        batch.create_check_constraint(
            "ck_order_client_order_id",
            "length(trim(client_order_id)) > 0",
        )
        batch.create_unique_constraint(
            "uq_order_account_client_order_id",
            ["account_id", "client_order_id"],
        )

    with op.batch_alter_table("executions") as batch:
        batch.create_foreign_key(
            "fk_execution_source_price",
            "prices",
            ["source_price_id"],
            ["price_id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_execution_source_bar_provenance",
            "(source_price_id IS NULL AND source_content_revision IS NULL "
            "AND trigger_policy_version IS NULL) OR "
            "(source_price_id > 0 AND source_content_revision > 0 "
            "AND length(trim(trigger_policy_version)) > 0)",
        )

    if bind.dialect.name == "postgresql":
        op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE public.pending_orders TO vm_execution")
        op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE public.orders TO vm_execution")
        op.execute("GRANT SELECT, INSERT ON TABLE public.executions TO vm_execution")


def downgrade() -> None:
    with op.batch_alter_table("executions") as batch:
        batch.drop_constraint("ck_execution_source_bar_provenance", type_="check")
        batch.drop_constraint("fk_execution_source_price", type_="foreignkey")
    op.drop_column("executions", "trigger_policy_version")
    op.drop_column("executions", "source_content_revision")
    op.drop_column("executions", "source_price_id")

    with op.batch_alter_table("orders") as batch:
        batch.drop_constraint("uq_order_account_client_order_id", type_="unique")
        batch.drop_constraint("ck_order_client_order_id", type_="check")
        batch.drop_constraint("ck_order_state", type_="check")
        batch.create_check_constraint(
            "ck_order_state",
            "state IN ('new', 'working', 'partially_filled', 'filled', 'canceled', 'rejected')",
        )
    op.drop_column("orders", "client_order_id")

    with op.batch_alter_table("pending_orders") as batch:
        batch.drop_index("ix_pending_orders_oco_group_id")
        batch.drop_constraint("uq_pending_order_account_client_order_id", type_="unique")
        batch.drop_constraint("ck_pending_order_market_watermark", type_="check")
        batch.drop_constraint("ck_pending_order_cumulative_fill", type_="check")
        batch.drop_constraint("ck_pending_order_client_order_id", type_="check")
        batch.drop_constraint("ck_pending_order_status", type_="check")
        batch.create_check_constraint(
            "ck_pending_order_status",
            "status IN ('pending', 'submitted', 'filled', 'partially_filled', "
            "'cancelled', 'expired', 'rejected')",
        )
    for column in (
        "trigger_policy_version",
        "last_market_data_revision",
        "last_market_data_ts",
        "market_data_timeframe",
        "market_data_source",
        "cumulative_filled_quantity",
        "oco_group_id",
        "parent_order_id",
        "reduce_only",
        "time_in_force",
        "purpose",
        "trigger_price",
        "client_order_id",
    ):
        op.drop_column("pending_orders", column)
