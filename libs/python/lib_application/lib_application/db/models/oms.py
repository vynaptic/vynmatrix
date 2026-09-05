"""Section I — OMS/EMS.

Tables: ``order_intents``, ``orders``, ``child_orders``, ``option_order_legs``,
``executions``, ``positions``, ``daily_nav``, ``execution_logs``,
``execution_metrics``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from lib_common.asset_classes import NULLABLE_ASSET_CLASS_CHECK_SQL

from ._base import Base, JSONType, SQLiteBigInteger, generate_uuid


class OrderIntent(Base):
    """Canonical order requests (pre-broker translation)."""

    __tablename__ = "order_intents"

    intent_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(50), ForeignKey("users.user_id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("linked_broker_accounts.account_id"), nullable=False
    )
    strategy_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey(
            "strategies.strategy_id",
            name="fk_order_intent_strategy",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    canonical_signal_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "canonical_signals.signal_id",
            name="fk_order_intent_canonical_signal",
            ondelete="RESTRICT",
        ),
        nullable=False,
        index=True,
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    broker_environment: Mapped[str] = mapped_column(String(20), nullable=False)
    method: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[Any] = mapped_column(JSONType, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="created", server_default="created"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("intent_id", "account_id", name="uq_order_intent_account"),
        UniqueConstraint(
            "account_id",
            "idempotency_key",
            name="uq_order_intent_account_idempotency",
        ),
        CheckConstraint(
            "method IN ('SPOT', 'MARGIN', 'PERP', 'FUTURES', 'OPTIONS_SINGLE', 'OPTIONS_STRATEGY')",
            name="ck_intent_method",
        ),
        CheckConstraint(
            "status IN ('created', 'routed', 'rejected', 'canceled', 'expired')",
            name="ck_intent_status",
        ),
        CheckConstraint("side IN ('BUY', 'SELL')", name="ck_order_intent_side"),
        CheckConstraint(
            "length(trim(execution_mode)) > 0",
            name="ck_order_intent_execution_mode",
        ),
        CheckConstraint(
            "broker_environment IN ('paper', 'live')",
            name="ck_order_intent_broker_environment",
        ),
        CheckConstraint(
            "idempotency_key IS NULL OR length(trim(idempotency_key)) > 0",
            name="ck_order_intent_idempotency_key",
        ),
        ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["linked_broker_accounts.account_id", "linked_broker_accounts.user_id"],
            name="fk_order_intent_account_owner",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_order_intents_owner_strategy_environment",
            "user_id",
            "account_id",
            "strategy_id",
            "broker_environment",
        ),
    )


class Order(Base):
    """Broker orders."""

    __tablename__ = "orders"

    order_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    intent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("order_intents.intent_id", ondelete="CASCADE"),
        nullable=False,
    )
    broker_id: Mapped[int] = mapped_column(Integer, ForeignKey("brokers.broker_id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("linked_broker_accounts.account_id"), nullable=False
    )
    settlement_currency: Mapped[str] = mapped_column(String(10), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(100), nullable=False, default=generate_uuid)
    broker_order_ref: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    routed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )
    last_update: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint("order_id", "account_id", name="uq_order_account"),
        UniqueConstraint(
            "account_id",
            "client_order_id",
            name="uq_order_account_client_order_id",
        ),
        ForeignKeyConstraint(
            ["intent_id", "account_id"],
            ["order_intents.intent_id", "order_intents.account_id"],
            name="fk_order_intent_account_match",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "state IN ('new', 'submission_unknown', 'working', 'partially_filled', "
            "'filled', 'canceled', 'rejected')",
            name="ck_order_state",
        ),
        CheckConstraint(
            "length(trim(client_order_id)) > 0",
            name="ck_order_client_order_id",
        ),
    )


class ChildOrder(Base):
    """Child orders for slicing/iceberg strategies."""

    __tablename__ = "child_orders"

    child_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    broker_order_ref: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    meta: Mapped[Any | None] = mapped_column(JSONType)


class OptionOrderLeg(Base):
    """Option legs for multi-leg orders."""

    __tablename__ = "option_order_legs"

    leg_row_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False
    )
    leg_index: Mapped[int] = mapped_column(Integer, nullable=False)
    option_right: Mapped[str | None] = mapped_column(
        String(10)
    )  # Renamed from 'right' (reserved word)
    position: Mapped[str | None] = mapped_column(String(10))
    strike: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    expiry: Mapped[date | None] = mapped_column(Date)
    qty: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)

    __table_args__ = (
        CheckConstraint("option_right IN ('CALL', 'PUT')", name="ck_option_right"),
        CheckConstraint("position IN ('LONG', 'SHORT')", name="ck_option_position"),
    )


class Execution(Base):
    """Trade executions/fills."""

    __tablename__ = "executions"

    exec_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False
    )
    instr_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("instruments.instr_id"),
        nullable=False,
    )
    fill_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    fee_ccy: Mapped[str] = mapped_column(String(10), nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 8),
        nullable=False,
    )
    venue: Mapped[str] = mapped_column(String(50), nullable=False)
    trade_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_price_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("prices.price_id", ondelete="RESTRICT"),
    )
    source_content_revision: Mapped[int | None] = mapped_column(Integer)
    trigger_policy_version: Mapped[str | None] = mapped_column(String(40))

    __table_args__ = (
        UniqueConstraint("order_id", "trade_id", name="uq_execution_order_trade"),
        CheckConstraint(
            "qty > 0 AND price > 0",
            name="ck_execution_exact_fill_economics",
        ),
        CheckConstraint(
            "nullif(trim(fee_ccy), '') IS NOT NULL",
            name="ck_execution_fee_currency",
        ),
        CheckConstraint(
            "length(trim(venue)) > 0",
            name="ck_execution_venue_identity",
        ),
        CheckConstraint(
            "length(trim(trade_id)) > 0",
            name="ck_execution_trade_identity",
        ),
        CheckConstraint(
            "(source_price_id IS NULL AND source_content_revision IS NULL "
            "AND trigger_policy_version IS NULL) OR "
            "(source_price_id > 0 AND source_content_revision > 0 "
            "AND length(trim(trigger_policy_version)) > 0)",
            name="ck_execution_source_bar_provenance",
        ),
    )


class Position(Base):
    """Current positions."""

    __tablename__ = "positions"

    pos_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("linked_broker_accounts.account_id"), nullable=False
    )
    instr_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("instruments.instr_id"), nullable=False
    )
    qty: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    avg_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    last_mark: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    quantity_unit: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="asset",
        server_default="asset",
    )
    contract_multiplier: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    gross_notional: Mapped[Decimal | None] = mapped_column(Numeric(28, 8))
    notional_currency: Mapped[str | None] = mapped_column(String(10))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("account_id", "instr_id", name="uq_position"),
        CheckConstraint(
            "quantity_unit IN ('asset', 'contracts', 'quote_notional')",
            name="ck_position_quantity_unit",
        ),
        CheckConstraint(
            "contract_multiplier IS NULL OR contract_multiplier > 0",
            name="ck_position_contract_multiplier",
        ),
        CheckConstraint(
            "qty = 0 OR (gross_notional IS NOT NULL AND gross_notional > 0)",
            name="ck_position_gross_notional",
        ),
        CheckConstraint(
            "qty = 0 OR notional_currency IS NOT NULL",
            name="ck_position_notional_currency",
        ),
    )


class DailyNav(Base):
    """Daily NAV snapshots for one owned broker account."""

    __tablename__ = "daily_nav"

    nav_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(50), ForeignKey("users.user_id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "linked_broker_accounts.account_id",
            name="fk_daily_nav_account",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    nav_ccy: Mapped[str] = mapped_column(String(10), nullable=False)
    nav_value: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    ret_1d: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    drawdown: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    benchmark: Mapped[str | None] = mapped_column(String(50))
    bench_ret_1d: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))

    __table_args__ = (
        UniqueConstraint("account_id", "date", name="uq_daily_nav_account_date"),
        ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["linked_broker_accounts.account_id", "linked_broker_accounts.user_id"],
            name="fk_daily_nav_account_owner",
            ondelete="RESTRICT",
        ),
        Index("ix_daily_nav_user_account_date", "user_id", "account_id", "date"),
    )


class ExecutionLog(Base):
    """Log of execution outcomes for signals.

    The canonical_signal_id provides traceability between execution and the
    original signal that triggered it, enabling feedback loop correlation.
    """

    __tablename__ = "execution_logs"

    log_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50), ForeignKey("users.user_id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("linked_broker_accounts.account_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    strategy_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("strategies.strategy_id"), nullable=False
    )
    # FK to canonical signal for execution-signal correlation (Issue 1.5)
    canonical_signal_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("canonical_signals.signal_id"),
        # Non-signal operational audit outcomes may not have a canonical signal.
        nullable=True,
        index=True,
    )
    signal_type: Mapped[str] = mapped_column(String(10), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    execution_details: Mapped[Any | None] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    error_message: Mapped[str | None] = mapped_column(String(500))
    # Cross-container trace id (matches canonical_signals.run_id) for single-key
    # trace joins. This column is the sole execution-log run identity.
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["linked_broker_accounts.account_id", "linked_broker_accounts.user_id"],
            name="fk_execution_log_account_owner",
            ondelete="RESTRICT",
        ),
    )


class ExecutionMetric(Base):
    """Per-execution performance snapshot for user/strategy metrics."""

    __tablename__ = "execution_metrics"

    metric_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50), ForeignKey("users.user_id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("linked_broker_accounts.account_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    strategy_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("strategies.strategy_id"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    broker: Mapped[str] = mapped_column(String(20), nullable=False)
    signal_id: Mapped[str | None] = mapped_column(String(100))
    run_id: Mapped[str | None] = mapped_column(String(100))
    asset_class: Mapped[str | None] = mapped_column(String(20))

    equity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    available_cash: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    margin_used: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    orders_submitted: Mapped[int | None] = mapped_column(Integer, default=0, server_default="0")
    orders_filled: Mapped[int | None] = mapped_column(Integer, default=0, server_default="0")
    total_commission: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    commission_currency: Mapped[str | None] = mapped_column(String(10))
    peak_equity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    drawdown_duration_sec: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    max_drawdown_duration_sec: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exposure: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    leverage: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))

    metadata_json: Mapped[Any | None] = mapped_column("metadata", JSONType)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            NULLABLE_ASSET_CLASS_CHECK_SQL,
            name="ck_execution_metric_asset_class",
        ),
        ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["linked_broker_accounts.account_id", "linked_broker_accounts.user_id"],
            name="fk_execution_metric_account_owner",
            ondelete="RESTRICT",
        ),
        Index("ix_execution_metrics_symbol", "symbol"),
        Index(
            "ix_execution_metrics_user_strategy_time",
            "user_id",
            "strategy_id",
            "created_at",
        ),
        Index(
            "ix_execution_metrics_window_end",
            "user_id",
            "strategy_id",
            "symbol",
            "window_end",
        ),
    )


__all__ = [
    "ChildOrder",
    "DailyNav",
    "Execution",
    "ExecutionLog",
    "ExecutionMetric",
    "OptionOrderLeg",
    "Order",
    "OrderIntent",
    "Position",
]
