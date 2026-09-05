"""Tenant/account-scoped frozen rebalance plans and execution progress."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, JSONType

_DIGEST_LENGTH = 64


class AccountRebalancePlan(Base):
    """Frozen account plan plus fenced mutable execution progress."""

    __tablename__ = "account_rebalance_plans"

    account_plan_id: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), primary_key=True)
    model_rebalance_id: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    user_id: Mapped[str] = mapped_column(String(50), nullable=False)
    binding_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    broker_account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(50), nullable=False)
    data_use_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_authority_sha256: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    decision_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    execute_not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    execution_session_sha256: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    execution_policy: Mapped[Any] = mapped_column(JSONType, nullable=False)
    broker_route: Mapped[Any] = mapped_column(JSONType, nullable=False)
    execution_policy_sha256: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    broker_route_sha256: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    model_leg_count: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_leg_count: Mapped[int] = mapped_column(Integer, nullable=False)
    intentional_cash_slots: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(
        String(24), nullable=False, default="exit", server_default="exit"
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    fence_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    completion_account_generation: Mapped[int | None] = mapped_column(BigInteger)
    terminal_targets_sha256: Mapped[str | None] = mapped_column(String(_DIGEST_LENGTH))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        onupdate=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            [
                "model_rebalance_id",
                "strategy_id",
                "data_use_scope",
                "provider_authority_sha256",
                "decision_cutoff",
                "execute_not_before",
                "execution_session_sha256",
                "model_leg_count",
            ],
            [
                "model_rebalances.rebalance_id",
                "model_rebalances.strategy_id",
                "model_rebalances.data_use_scope",
                "model_rebalances.provider_authority_sha256",
                "model_rebalances.decision_cutoff",
                "model_rebalances.execute_not_before",
                "model_rebalances.execution_session_sha256",
                "model_rebalances.expected_leg_count",
            ],
            name="fk_account_rebalance_plan_exact_model_lineage",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["binding_id", "user_id", "broker_account_id", "strategy_id"],
            [
                "user_strategy_bindings.binding_id",
                "user_strategy_bindings.user_id",
                "user_strategy_bindings.broker_account_id",
                "user_strategy_bindings.strategy_id",
            ],
            name="fk_account_rebalance_plan_binding_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["broker_account_id", "user_id"],
            ["linked_broker_accounts.account_id", "linked_broker_accounts.user_id"],
            name="fk_account_rebalance_plan_account_owner",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(account_plan_id) = 64 AND length(model_rebalance_id) = 64 "
            "AND length(provider_authority_sha256) = 64 "
            "AND length(execution_session_sha256) = 64 "
            "AND length(execution_policy_sha256) = 64 "
            "AND length(broker_route_sha256) = 64 AND length(content_sha256) = 64",
            name="ck_account_rebalance_plan_digests",
        ),
        CheckConstraint(
            "data_use_scope = 'paper_forward'",
            name="ck_account_rebalance_plan_data_use_scope",
        ),
        CheckConstraint(
            "decision_cutoff < execute_not_before AND execute_not_before < expires_at",
            name="ck_account_rebalance_plan_timing",
        ),
        CheckConstraint(
            "execute_not_before < progress_deadline_at AND progress_deadline_at <= expires_at",
            name="ck_account_rebalance_plan_progress_timing",
        ),
        CheckConstraint(
            "model_leg_count >= 0 AND execution_leg_count >= 0 "
            "AND execution_leg_count <= model_leg_count "
            "AND intentional_cash_slots >= 0",
            name="ck_account_rebalance_plan_counts",
        ),
        CheckConstraint(
            "phase IN ('exit', 'account_refresh', 'entry', 'complete', 'failed', 'expired')",
            name="ck_account_rebalance_plan_phase",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'blocked', 'completed', 'degraded', "
            "'failed', 'expired', 'cancelled')",
            name="ck_account_rebalance_plan_status",
        ),
        CheckConstraint("fence_generation >= 0", name="ck_account_rebalance_plan_fence"),
        CheckConstraint(
            "(completion_account_generation IS NULL AND terminal_targets_sha256 IS NULL) OR "
            "(completion_account_generation IS NOT NULL "
            "AND terminal_targets_sha256 IS NOT NULL "
            "AND completion_account_generation > 0 "
            "AND length(terminal_targets_sha256) = 64)",
            name="ck_account_rebalance_plan_terminal_fence",
        ),
        UniqueConstraint(
            "account_plan_id",
            "user_id",
            "broker_account_id",
            name="uq_account_rebalance_plan_owner",
        ),
        UniqueConstraint(
            "account_plan_id",
            "user_id",
            "broker_account_id",
            "status",
            name="uq_account_rebalance_plan_owner_status",
        ),
        UniqueConstraint(
            "model_rebalance_id",
            "binding_id",
            "user_id",
            "broker_account_id",
            name="uq_account_rebalance_plan_binding",
        ),
        Index(
            "ix_account_rebalance_plan_partition",
            "user_id",
            "broker_account_id",
            "status",
            "execute_not_before",
        ),
        Index(
            "ix_account_rebalance_plan_progress",
            "status",
            "progress_deadline_at",
        ),
    )


class AccountRebalancePlanLeg(Base):
    """One selected or rejected account-specific model target."""

    __tablename__ = "account_rebalance_plan_legs"

    account_plan_id: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    broker_account_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    model_sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    command_sequence: Mapped[int | None] = mapped_column(Integer)
    model_rebalance_id: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    model_leg_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_signal_snapshot_sha256: Mapped[str] = mapped_column(
        String(_DIGEST_LENGTH), nullable=False
    )
    plan_leg_id: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    instr_id: Mapped[int] = mapped_column(Integer, nullable=False)
    external_signal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    disposition: Mapped[str] = mapped_column(String(16), nullable=False)
    rank_position: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    allocation_hint: Mapped[Decimal] = mapped_column(Numeric(18, 12), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    depends_on_sequences: Mapped[Any] = mapped_column(JSONType, nullable=False, default=list)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    frozen_target_quantity: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    target_account_generation: Mapped[int | None] = mapped_column(BigInteger)
    target_frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_plan_id", "user_id", "broker_account_id"],
            [
                "account_rebalance_plans.account_plan_id",
                "account_rebalance_plans.user_id",
                "account_rebalance_plans.broker_account_id",
            ],
            name="fk_account_rebalance_leg_plan_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "model_rebalance_id",
                "model_sequence",
                "model_leg_id",
                "instr_id",
                "external_signal_id",
                "model_signal_snapshot_sha256",
            ],
            [
                "model_rebalance_legs.rebalance_id",
                "model_rebalance_legs.sequence",
                "model_rebalance_legs.leg_id",
                "model_rebalance_legs.instr_id",
                "model_rebalance_legs.external_signal_id",
                "model_rebalance_legs.signal_snapshot_sha256",
            ],
            name="fk_account_rebalance_leg_exact_model_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "model_rebalance_id",
                "model_sequence",
                "model_leg_id",
                "instr_id",
                "external_signal_id",
                "model_signal_snapshot_sha256",
                "rank_position",
            ],
            [
                "model_rebalance_legs.rebalance_id",
                "model_rebalance_legs.sequence",
                "model_rebalance_legs.leg_id",
                "model_rebalance_legs.instr_id",
                "model_rebalance_legs.external_signal_id",
                "model_rebalance_legs.signal_snapshot_sha256",
                "model_rebalance_legs.rank_position",
            ],
            name="fk_account_rebalance_leg_exact_model_rank",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "model_sequence >= 0 AND (command_sequence IS NULL OR command_sequence >= 0)",
            name="ck_account_rebalance_leg_sequence",
        ),
        CheckConstraint(
            "length(plan_leg_id) = 64 AND length(trim(model_leg_id)) > 0 "
            "AND length(model_signal_snapshot_sha256) = 64 "
            "AND length(trim(external_signal_id)) > 0 AND length(trim(symbol)) > 0",
            name="ck_account_rebalance_leg_identity",
        ),
        CheckConstraint(
            "phase IN ('exit', 'reduce', 'entry')",
            name="ck_account_rebalance_leg_phase",
        ),
        CheckConstraint(
            "action IN ('exit', 'reduce', 'entry', 'reject')",
            name="ck_account_rebalance_leg_action",
        ),
        CheckConstraint(
            "disposition IN ('selected', 'rejected')",
            name="ck_account_rebalance_leg_disposition",
        ),
        CheckConstraint(
            "(disposition = 'selected' AND required "
            "AND command_sequence IS NOT NULL AND action = phase) OR "
            "(disposition = 'rejected' AND NOT required "
            "AND command_sequence IS NULL AND action = 'reject')",
            name="ck_account_rebalance_leg_selection",
        ),
        CheckConstraint(
            "rank_position IS NULL OR rank_position > 0",
            name="ck_account_rebalance_leg_rank",
        ),
        CheckConstraint(
            "allocation_hint >= 0 AND allocation_hint <= 1",
            name="ck_account_rebalance_leg_allocation",
        ),
        CheckConstraint(
            "length(trim(reason_code)) > 0",
            name="ck_account_rebalance_leg_reason",
        ),
        CheckConstraint(
            "status IN ('pending', 'submitted', 'partial', 'confirmed', 'rejected', "
            "'failed', 'skipped')",
            name="ck_account_rebalance_leg_status",
        ),
        CheckConstraint(
            "(frozen_target_quantity IS NULL AND target_account_generation IS NULL "
            "AND target_frozen_at IS NULL) OR "
            "(frozen_target_quantity IS NOT NULL AND target_account_generation IS NOT NULL "
            "AND target_frozen_at IS NOT NULL AND frozen_target_quantity >= 0 "
            "AND target_account_generation > 0)",
            name="ck_account_rebalance_leg_target_fence",
        ),
        UniqueConstraint("account_plan_id", "plan_leg_id", name="uq_account_rebalance_leg_id"),
        UniqueConstraint(
            "account_plan_id", "command_sequence", name="uq_account_rebalance_command_sequence"
        ),
        Index(
            "ix_account_rebalance_leg_status",
            "account_plan_id",
            "phase",
            "status",
        ),
    )


class AccountRebalancePlanResolution(Base):
    """Append-only operator resolution of one terminal failed account plan."""

    __tablename__ = "account_rebalance_plan_resolutions"

    resolution_id: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), primary_key=True)
    account_plan_id: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    user_id: Mapped[str] = mapped_column(String(50), nullable=False)
    broker_account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    plan_status: Mapped[str] = mapped_column(String(24), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(100))
    resolution_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    resolved_by: Mapped[str] = mapped_column(String(100), nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_plan_id", "user_id", "broker_account_id", "plan_status"],
            [
                "account_rebalance_plans.account_plan_id",
                "account_rebalance_plans.user_id",
                "account_rebalance_plans.broker_account_id",
                "account_rebalance_plans.status",
            ],
            name="fk_account_rebalance_resolution_exact_failed_plan",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(resolution_id) = 64 AND length(account_plan_id) = 64",
            name="ck_account_rebalance_resolution_digests",
        ),
        CheckConstraint(
            "plan_status = 'failed'",
            name="ck_account_rebalance_resolution_plan_status",
        ),
        CheckConstraint(
            "resolution_type IN ('acknowledged', 'reconciled', 'remediated')",
            name="ck_account_rebalance_resolution_type",
        ),
        CheckConstraint(
            "length(trim(reason)) > 0 AND length(trim(resolved_by)) > 0",
            name="ck_account_rebalance_resolution_attribution",
        ),
        Index(
            "ix_account_rebalance_resolution_plan",
            "account_plan_id",
            "resolved_at",
            "resolution_id",
        ),
        Index(
            "ix_account_rebalance_resolution_owner",
            "user_id",
            "broker_account_id",
            "resolved_at",
        ),
    )


class AccountExecutionGeneration(Base):
    """Current account-wide writer generation guarded by a DB advisory lock."""

    __tablename__ = "account_execution_generations"

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    broker_account_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    active_owner: Mapped[str | None] = mapped_column(String(100))
    active_writer_kind: Mapped[str | None] = mapped_column(String(24))
    active_account_plan_id: Mapped[str | None] = mapped_column(String(_DIGEST_LENGTH))
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        onupdate=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["broker_account_id", "user_id"],
            ["linked_broker_accounts.account_id", "linked_broker_accounts.user_id"],
            name="fk_account_execution_generation_owner",
            ondelete="RESTRICT",
        ),
        CheckConstraint("generation > 0", name="ck_account_execution_generation_positive"),
        CheckConstraint(
            "active_writer_kind IS NULL OR active_writer_kind IN "
            "('ordinary', 'manual', 'rebalance', 'historical_replay', "
            "'paper_lifecycle', 'reconciliation')",
            name="ck_account_execution_generation_writer_kind",
        ),
        CheckConstraint(
            "active_account_plan_id IS NULL OR length(active_account_plan_id) = 64",
            name="ck_account_execution_generation_plan_digest",
        ),
        CheckConstraint(
            "(active_owner IS NULL AND active_writer_kind IS NULL "
            "AND active_account_plan_id IS NULL AND acquired_at IS NULL) OR "
            "(length(trim(active_owner)) > 0 AND active_writer_kind IS NOT NULL "
            "AND acquired_at IS NOT NULL)",
            name="ck_account_execution_generation_active",
        ),
        Index(
            "ix_account_execution_generation_active",
            "active_owner",
            "updated_at",
        ),
    )


__all__ = [
    "AccountExecutionGeneration",
    "AccountRebalancePlan",
    "AccountRebalancePlanLeg",
    "AccountRebalancePlanResolution",
]
