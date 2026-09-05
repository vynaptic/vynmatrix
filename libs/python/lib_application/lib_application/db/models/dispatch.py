"""Section K (dispatch) — Scoring → Execution Dispatch & Outbox.

Tables: ``execution_decision_logs``, ``pending_orders``, ``outbox_events``.

Extracted from :mod:`.scoring` (Section K) to keep each domain submodule under
the audit LOC cap. These are the OMS-edge audit/durability tables plus the
transactional outbox the scoring engine writes to during the
scoring → execution handoff. They register against the same shared ``Base`` and
carry no ORM relationships to the core scoring tables, so the split is purely
file-level.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, JSONType, SQLiteBigInteger, generate_uuid


class ExecutionDecisionLog(Base):
    """Audit log of execution decisions made by the scoring engine."""

    __tablename__ = "execution_decision_logs"

    decision_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(50), ForeignKey("users.user_id"), nullable=False)
    binding_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("user_strategy_bindings.binding_id")
    )
    instr_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("instruments.instr_id"))
    # Rows written before exact lineage may remain NULL. Every current writer
    # sets lineage_schema_version=v1 together with both exact identities.
    canonical_signal_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "canonical_signals.signal_id",
            name="fk_execution_decision_canonical_signal",
            ondelete="RESTRICT",
        ),
    )
    broker_account_id: Mapped[int | None] = mapped_column(BigInteger)
    lineage_schema_version: Mapped[str | None] = mapped_column(String(10))

    # Scores that triggered
    asset_score: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    sector_score: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    market_score: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))

    # Decision
    should_execute: Mapped[bool | None] = mapped_column(Boolean)
    execution_mode: Mapped[str | None] = mapped_column(String(30))
    direction: Mapped[str | None] = mapped_column(String(10))  # long, short

    # Sizing
    position_size_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    position_size_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))

    # Outcome
    # Committed ExecutionLog.log_id. Nullable only when execution succeeds but
    # best-effort execution-log persistence fails.
    execution_id: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending", server_default="pending"
    )

    # Deduplication support
    idempotency_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
    )
    # NOTE: This is the string-based Signal.signal_id (UUID from the Signal
    # dataclass), NOT the BigInteger CanonicalSignal.signal_id autoincrement PK.
    # These are intentionally different identifiers at different architectural
    # layers (domain Signal UUID vs DB autoincrement).  Do NOT add a FK here.
    signal_id: Mapped[str | None] = mapped_column(String(100))
    action: Mapped[str | None] = mapped_column(String(20))
    # Cross-container trace id (matches canonical_signals.run_id) so RCA can join
    # the decision row into the run chain with a single key instead of hopping
    # via the string signal_id (DB-1). Not a FK — it is a trace id, not an entity.
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)

    # Metadata
    reasoning: Mapped[Any | None] = mapped_column(JSONType)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(tz=UTC), index=True
    )

    # Policy snapshots frozen at decision time for audit reproducibility.
    # Stores the user binding configuration and broker routing used for this decision,
    # so execution is reproducible even if user config later changes.
    binding_config_snapshot: Mapped[Any | None] = mapped_column(JSONType)
    broker_route_snapshot: Mapped[Any | None] = mapped_column(JSONType)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'executing', 'executed', 'rejected', 'failed', 'skipped')",
            name="ck_decision_status",
        ),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_execution_decision_idempotency_key",
        ),
        CheckConstraint(
            "lineage_schema_version IS NULL OR "
            "(lineage_schema_version = 'v1' "
            "AND canonical_signal_id IS NOT NULL "
            "AND broker_account_id IS NOT NULL)",
            name="ck_execution_decision_exact_lineage",
        ),
        ForeignKeyConstraint(
            ["broker_account_id", "user_id"],
            ["linked_broker_accounts.account_id", "linked_broker_accounts.user_id"],
            name="fk_execution_decision_account_owner",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_execution_decision_signal_account",
            "canonical_signal_id",
            "broker_account_id",
        ),
    )


class PendingOrder(Base):
    """Durable store for orders submitted to brokers but not yet confirmed.

    Persisted **before** broker submission so that a crash between submit and
    confirmation does not lose order state.  Reconciliation workers query
    this table to detect orphaned orders.

    Lifecycle:
        pending → submission_unknown / working → partially_filled → filled
        working → cancelled / expired / rejected
    """

    __tablename__ = "pending_orders"

    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50), ForeignKey("users.user_id"), nullable=False)
    signal_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    instr_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("instruments.instr_id"))
    broker_account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("linked_broker_accounts.account_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    canonical_order_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("orders.order_id", ondelete="SET NULL"),
        unique=True,
    )
    client_order_id: Mapped[str] = mapped_column(String(100), nullable=False, default=generate_uuid)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    settlement_currency: Mapped[str] = mapped_column(String(10), nullable=False)

    # Order details
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # BUY, SELL
    order_type: Mapped[str] = mapped_column(String(20), nullable=False)  # market, limit
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    trigger_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    purpose: Mapped[str | None] = mapped_column(String(30))
    time_in_force: Mapped[str] = mapped_column(
        String(10), nullable=False, default="gtc", server_default="gtc"
    )
    reduce_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    parent_order_id: Mapped[str | None] = mapped_column(String(100))
    oco_group_id: Mapped[str | None] = mapped_column(String(100), index=True)
    execution_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    broker: Mapped[str] = mapped_column(String(30), nullable=False)
    broker_environment: Mapped[str | None] = mapped_column(String(20))
    credential_ref: Mapped[str | None] = mapped_column(String(100))
    breaker_key: Mapped[str | None] = mapped_column(String(255), index=True)

    # State tracking
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    broker_order_id: Mapped[str | None] = mapped_column(
        String(100)
    )  # Populated after broker acknowledges
    filled_quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), default=0)
    cumulative_filled_quantity: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False, default=0, server_default="0"
    )
    filled_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    commission: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    commission_currency: Mapped[str | None] = mapped_column(String(10))
    error_message: Mapped[str | None] = mapped_column(Text)

    # Last committed real-market bar consumed by the durable local-paper
    # lifecycle. Corrections at or behind this watermark never rewrite
    # economic history.
    market_data_source: Mapped[str | None] = mapped_column(String(50))
    market_data_timeframe: Mapped[str | None] = mapped_column(String(20))
    last_market_data_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_market_data_revision: Mapped[int | None] = mapped_column(Integer)
    # Exact canonical fill whose rebuildable observability projections still
    # need to be committed. The lifecycle clears this only after
    # execution_metrics/positions/NAV refresh succeeds, so a crash cannot
    # silently advance to another candle with stale P&L observability.
    pending_projection_exec_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("executions.exec_id", ondelete="RESTRICT"),
        index=True,
    )
    trigger_policy_version: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="ohlc-conservative-v1",
        server_default="ohlc-conservative-v1",
    )

    # Cross-container tracing
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)

    # Strategy context
    strategy_id: Mapped[str | None] = mapped_column(
        String(50), ForeignKey("strategies.strategy_id")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        index=True,
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["canonical_order_id", "broker_account_id"],
            ["orders.order_id", "orders.account_id"],
            name="fk_pending_order_account_match",
        ),
        ForeignKeyConstraint(
            ["broker_account_id", "user_id"],
            ["linked_broker_accounts.account_id", "linked_broker_accounts.user_id"],
            name="fk_pending_order_broker_account_owner",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('pending', 'submission_unknown', 'submitted', 'working', "
            "'filled', 'partially_filled', 'cancelled', 'expired', 'rejected')",
            name="ck_pending_order_status",
        ),
        CheckConstraint(
            "length(trim(client_order_id)) > 0",
            name="ck_pending_order_client_order_id",
        ),
        CheckConstraint(
            "quantity > 0 AND cumulative_filled_quantity >= 0 "
            "AND cumulative_filled_quantity <= quantity",
            name="ck_pending_order_cumulative_fill",
        ),
        CheckConstraint(
            "(last_market_data_ts IS NULL AND last_market_data_revision IS NULL) "
            "OR (last_market_data_ts IS NOT NULL AND last_market_data_revision IS NOT NULL)",
            name="ck_pending_order_market_watermark",
        ),
        UniqueConstraint(
            "broker_account_id",
            "client_order_id",
            name="uq_pending_order_account_client_order_id",
        ),
        Index("ix_pending_order_breaker_status", "breaker_key", "status"),
        Index("ix_pending_order_user_status", "user_id", "status"),
    )


class OutboxEvent(Base):
    """Transactional outbox for reliable internal event delivery."""

    __tablename__ = "outbox_events"

    event_id: Mapped[str] = mapped_column(String(50), primary_key=True, default=generate_uuid)
    topic: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False, default="v1")
    aggregate_type: Mapped[str | None] = mapped_column(String(50))
    aggregate_id: Mapped[str | None] = mapped_column(String(100))
    event_key: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)
    ordering_key: Mapped[str | None] = mapped_column(String(128))

    payload: Mapped[Any] = mapped_column(JSONType, nullable=False)
    headers: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    delivery_metadata: Mapped[Any | None] = mapped_column(JSONType)
    failure_class: Mapped[str | None] = mapped_column(String(20))
    redrive_generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    redrive_audit: Mapped[Any] = mapped_column(
        JSONType,
        nullable=False,
        default=list,
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        index=True,
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_owner: Mapped[str | None] = mapped_column(String(100))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        onupdate=lambda: datetime.now(tz=UTC),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'published', 'failed', 'dead_letter')",
            name="ck_outbox_status",
        ),
        CheckConstraint(
            "failure_class IS NULL OR failure_class IN ('transient', 'permanent')",
            name="ck_outbox_failure_class",
        ),
        CheckConstraint(
            "redrive_generation >= 0",
            name="ck_outbox_redrive_generation",
        ),
        Index(
            "ix_outbox_topic_failure_class",
            "topic",
            "status",
            "failure_class",
        ),
        Index("ix_outbox_topic_status_available", "topic", "status", "available_at"),
        Index("ix_outbox_aggregate", "aggregate_type", "aggregate_id"),
    )


__all__ = [
    "ExecutionDecisionLog",
    "OutboxEvent",
    "PendingOrder",
]
