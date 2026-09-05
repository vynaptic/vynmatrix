"""Durable synchronized-panel preparation and decision journal."""

from __future__ import annotations

from datetime import UTC, date, datetime
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
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, JSONType


class StrategyPanelInputRevision(Base):
    """Append-only data-plane submission consumed by panel-capable workers."""

    __tablename__ = "strategy_panel_input_revisions"

    input_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("strategies.strategy_id", ondelete="RESTRICT"),
        nullable=False,
    )
    strategy_version: Mapped[str] = mapped_column(String(20), nullable=False)
    universe_code: Mapped[str] = mapped_column(String(20), nullable=False)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    official_session_date: Mapped[date] = mapped_column(Date, nullable=False)
    execute_not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    data_use_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    entitlement_owner_user_id: Mapped[str | None] = mapped_column(
        String(50),
        ForeignKey(
            "users.user_id",
            name="fk_strategy_panel_input_owner",
            ondelete="RESTRICT",
        ),
    )
    provider_authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    membership_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    factor_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    panel_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_validator_id: Mapped[str] = mapped_column(String(100), nullable=False)
    strategy_validator_version: Mapped[str] = mapped_column(String(100), nullable=False)
    strategy_input_authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_input_authority_payload: Mapped[Any] = mapped_column(JSONType, nullable=False)
    panel_payload: Mapped[Any] = mapped_column(JSONType, nullable=False)
    strategy_input_payload: Mapped[Any] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "length(input_sha256) = 64 AND length(provider_authority_sha256) = 64 "
            "AND length(membership_sha256) = 64 "
            "AND length(factor_snapshot_sha256) = 64 AND length(panel_sha256) = 64 "
            "AND length(strategy_input_authority_sha256) = 64",
            name="ck_strategy_panel_input_digests",
        ),
        CheckConstraint(
            "execute_not_before > cutoff_at",
            name="ck_strategy_panel_input_execution_timing",
        ),
        CheckConstraint(
            "data_use_scope IN ('historical_validation', 'paper_forward', 'live_forward')",
            name="ck_strategy_panel_input_data_use_scope",
        ),
        CheckConstraint(
            "entitlement_owner_user_id IS NULL OR length(trim(entitlement_owner_user_id)) > 0",
            name="ck_strategy_panel_input_owner_nonempty",
        ),
        CheckConstraint(
            "length(trim(strategy_version)) > 0 AND length(trim(universe_code)) > 0",
            name="ck_strategy_panel_input_version_nonempty",
        ),
        CheckConstraint(
            "length(trim(strategy_validator_id)) > 0 "
            "AND length(trim(strategy_validator_version)) > 0",
            name="ck_strategy_panel_input_validator_nonempty",
        ),
        UniqueConstraint(
            "strategy_id",
            "strategy_version",
            "cutoff_at",
            "input_sha256",
            name="uq_strategy_panel_input_revision",
        ),
        UniqueConstraint(
            "input_sha256",
            "strategy_id",
            "strategy_version",
            "cutoff_at",
            "entitlement_owner_user_id",
            name="uq_strategy_panel_input_identity",
        ),
        Index(
            "ix_strategy_panel_input_poll",
            "strategy_id",
            "strategy_version",
            "data_use_scope",
            "entitlement_owner_user_id",
            "cutoff_at",
        ),
    )


class StrategyPanelDecision(Base):
    """Immutable panel input plus its atomic model-state/signal transition.

    A ``prepared`` row pins the exact effective universe and every observation
    revision before the core may evaluate it. Only runtime-owned output fields
    can advance once to ``completed``. A different input for an already pinned
    worker/official session is retained as a terminal ``rejected_correction``
    row even when its knowledge cutoff is later.
    """

    __tablename__ = "strategy_panel_decisions"

    decision_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    worker_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("strategy_runtime_states.worker_id", ondelete="RESTRICT"),
        nullable=False,
    )
    strategy_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("strategies.strategy_id", ondelete="RESTRICT"),
        nullable=False,
    )
    strategy_version: Mapped[str] = mapped_column(String(20), nullable=False)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    official_session_date: Mapped[date] = mapped_column(Date, nullable=False)
    official_session_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    execute_not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    execution_session_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    data_use_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    entitlement_owner_user_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey(
            "users.user_id",
            name="fk_strategy_panel_owner",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    runtime_environment: Mapped[str] = mapped_column(String(32), nullable=False)
    activation_cutoff: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    provider_authority_policy: Mapped[Any] = mapped_column(JSONType, nullable=False)
    provider_authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    membership_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    factor_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    panel_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    exclusion_count: Mapped[int] = mapped_column(Integer, nullable=False)
    maximum_content_revision: Mapped[int | None] = mapped_column(Integer)
    panel_payload: Mapped[Any] = mapped_column(JSONType, nullable=False)
    strategy_input_payload: Mapped[Any] = mapped_column(JSONType, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="prepared",
        server_default="prepared",
    )
    correction_of_decision_key: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("strategy_panel_decisions.decision_key", ondelete="RESTRICT"),
    )
    rejection_reason: Mapped[str | None] = mapped_column(String(300))
    state_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    prepared_state_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    prepared_state_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state_generation: Mapped[int | None] = mapped_column(BigInteger)
    state_sha256: Mapped[str | None] = mapped_column(String(64))
    state_payload: Mapped[Any | None] = mapped_column(JSONType)
    rank_snapshot_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("equity_rank_snapshots.rank_snapshot_id", ondelete="RESTRICT"),
    )
    evaluation_audit_sha256: Mapped[str | None] = mapped_column(String(64))
    evaluation_audit_payload: Mapped[Any | None] = mapped_column(JSONType)
    signal_envelopes: Mapped[Any] = mapped_column(JSONType, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "length(decision_key) = 64 "
            "AND length(official_session_sha256) = 64 "
            "AND length(execution_session_sha256) = 64 "
            "AND length(provider_authority_sha256) = 64 "
            "AND length(membership_sha256) = 64 "
            "AND length(factor_snapshot_sha256) = 64 "
            "AND length(panel_sha256) = 64 "
            "AND length(strategy_input_sha256) = 64 "
            "AND length(prepared_state_sha256) = 64 "
            "AND (state_sha256 IS NULL OR length(state_sha256) = 64) "
            "AND (rank_snapshot_id IS NULL OR length(rank_snapshot_id) = 64) "
            "AND (evaluation_audit_sha256 IS NULL "
            "OR length(evaluation_audit_sha256) = 64)",
            name="ck_strategy_panel_digests",
        ),
        CheckConstraint(
            "execute_not_before > cutoff_at",
            name="ck_strategy_panel_execution_timing",
        ),
        CheckConstraint(
            "data_use_scope IN ('historical_validation', 'paper_forward', 'live_forward')",
            name="ck_strategy_panel_data_use_scope",
        ),
        CheckConstraint(
            "length(trim(strategy_version)) > 0",
            name="ck_strategy_panel_version_nonempty",
        ),
        CheckConstraint(
            "member_count > 0 AND observation_count >= 0 AND exclusion_count >= 0 "
            "AND observation_count + exclusion_count = member_count",
            name="ck_strategy_panel_member_counts",
        ),
        CheckConstraint(
            "maximum_content_revision IS NULL OR maximum_content_revision > 0",
            name="ck_strategy_panel_content_revision_positive",
        ),
        CheckConstraint(
            "state_schema_version > 0 AND prepared_state_generation > 0",
            name="ck_strategy_panel_prepared_state",
        ),
        CheckConstraint(
            "status IN ('prepared', 'completed', 'rejected_correction', 'skipped', 'expired')",
            name="ck_strategy_panel_status",
        ),
        CheckConstraint(
            "data_use_scope = 'paper_forward' "
            "AND length(trim(entitlement_owner_user_id)) > 0 "
            "AND length(trim(runtime_environment)) > 0 "
            "AND activation_cutoff IS NOT NULL",
            name="ck_strategy_panel_runtime_binding",
        ),
        CheckConstraint(
            "(status = 'prepared' AND completed_at IS NULL "
            "AND correction_of_decision_key IS NULL AND rejection_reason IS NULL "
            "AND state_generation IS NULL AND state_sha256 IS NULL "
            "AND state_payload IS NULL AND rank_snapshot_id IS NULL "
            "AND evaluation_audit_sha256 IS NULL "
            "AND evaluation_audit_payload IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL "
            "AND correction_of_decision_key IS NULL AND rejection_reason IS NULL "
            "AND state_generation IS NOT NULL AND state_generation > 0 "
            "AND state_sha256 IS NOT NULL AND state_payload IS NOT NULL "
            "AND rank_snapshot_id IS NOT NULL "
            "AND evaluation_audit_sha256 IS NOT NULL "
            "AND evaluation_audit_payload IS NOT NULL) OR "
            "(status IN ('skipped', 'expired') AND completed_at IS NOT NULL "
            "AND correction_of_decision_key IS NULL "
            "AND rejection_reason IS NOT NULL "
            "AND length(trim(rejection_reason)) > 0 "
            "AND state_generation IS NULL AND state_sha256 IS NULL "
            "AND state_payload IS NULL AND rank_snapshot_id IS NULL "
            "AND evaluation_audit_sha256 IS NULL "
            "AND evaluation_audit_payload IS NULL) OR "
            "(status = 'rejected_correction' AND completed_at IS NOT NULL "
            "AND correction_of_decision_key IS NOT NULL "
            "AND rejection_reason IS NOT NULL "
            "AND length(trim(rejection_reason)) > 0 "
            "AND state_generation IS NULL AND state_sha256 IS NULL "
            "AND state_payload IS NULL AND rank_snapshot_id IS NULL "
            "AND evaluation_audit_sha256 IS NULL "
            "AND evaluation_audit_payload IS NULL)",
            name="ck_strategy_panel_completion",
        ),
        ForeignKeyConstraint(
            [
                "strategy_input_sha256",
                "strategy_id",
                "strategy_version",
                "cutoff_at",
                "entitlement_owner_user_id",
            ],
            [
                "strategy_panel_input_revisions.input_sha256",
                "strategy_panel_input_revisions.strategy_id",
                "strategy_panel_input_revisions.strategy_version",
                "strategy_panel_input_revisions.cutoff_at",
                "strategy_panel_input_revisions.entitlement_owner_user_id",
            ],
            name="fk_strategy_panel_registered_input",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "worker_id",
            "entitlement_owner_user_id",
            "runtime_environment",
            "cutoff_at",
            "strategy_input_sha256",
            name="uq_strategy_panel_worker_cutoff_input",
        ),
        Index(
            "uq_strategy_panel_authoritative_cutoff",
            "worker_id",
            "entitlement_owner_user_id",
            "runtime_environment",
            "cutoff_at",
            unique=True,
            postgresql_where=text("status IN ('prepared', 'completed')"),
            sqlite_where=text("status IN ('prepared', 'completed')"),
        ),
        Index(
            "ix_strategy_panel_pending",
            "worker_id",
            "entitlement_owner_user_id",
            "runtime_environment",
            "status",
            "cutoff_at",
        ),
        Index("ix_strategy_panel_worker_id", "worker_id"),
    )


__all__ = ["StrategyPanelDecision", "StrategyPanelInputRevision"]
