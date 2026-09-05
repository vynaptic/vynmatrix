"""Immutable account-neutral portfolio rebalance persistence."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
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
    event,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, JSONType

_DIGEST_LENGTH = 64


class ModelRebalance(Base):
    """One complete, provider-neutral model portfolio decision."""

    __tablename__ = "model_rebalances"

    rebalance_id: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(50), nullable=False)
    strat_ver_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rank_snapshot_id: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    effective_session: Mapped[date] = mapped_column(Date, nullable=False)
    decision_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    execute_not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    execution_session_sha256: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    data_use_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_authority_sha256: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    configuration_sha256: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    input_snapshot_sha256: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    expected_leg_count: Mapped[int] = mapped_column(Integer, nullable=False)
    intentional_cash_slots: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["strat_ver_id", "strategy_id"],
            ["strategy_versions.strat_ver_id", "strategy_versions.strategy_id"],
            name="fk_model_rebalance_strategy_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "rank_snapshot_id",
                "strategy_id",
                "strat_ver_id",
                "effective_session",
                "decision_cutoff",
                "configuration_sha256",
                "data_use_scope",
                "provider_authority_sha256",
            ],
            [
                "equity_rank_snapshots.rank_snapshot_id",
                "equity_rank_snapshots.strategy_id",
                "equity_rank_snapshots.strat_ver_id",
                "equity_rank_snapshots.effective_session",
                "equity_rank_snapshots.cutoff_at",
                "equity_rank_snapshots.configuration_digest",
                "equity_rank_snapshots.data_use_scope",
                "equity_rank_snapshots.provider_authority_digest",
            ],
            name="fk_model_rebalance_exact_rank_lineage",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(rebalance_id) = 64 AND length(rank_snapshot_id) = 64 "
            "AND length(execution_session_sha256) = 64 "
            "AND length(provider_authority_sha256) = 64 "
            "AND length(configuration_sha256) = 64 "
            "AND length(input_snapshot_sha256) = 64 AND length(content_sha256) = 64",
            name="ck_model_rebalance_digests",
        ),
        CheckConstraint(
            "data_use_scope IN ('historical_validation', 'paper_forward', 'live_forward')",
            name="ck_model_rebalance_data_use_scope",
        ),
        CheckConstraint(
            "execute_not_before > decision_cutoff",
            name="ck_model_rebalance_execution_timing",
        ),
        CheckConstraint(
            "expected_leg_count >= 0 AND intentional_cash_slots >= 0",
            name="ck_model_rebalance_counts",
        ),
        UniqueConstraint("rebalance_id", "strategy_id", name="uq_model_rebalance_strategy"),
        UniqueConstraint("rebalance_id", "rank_snapshot_id", name="uq_model_rebalance_rank"),
        UniqueConstraint(
            "rebalance_id",
            "strategy_id",
            "data_use_scope",
            "provider_authority_sha256",
            "decision_cutoff",
            "execute_not_before",
            "execution_session_sha256",
            "expected_leg_count",
            name="uq_model_rebalance_exact_account_lineage",
        ),
        UniqueConstraint(
            "strategy_id",
            "strat_ver_id",
            "effective_session",
            name="uq_model_rebalance_strategy_version_session",
        ),
        Index(
            "ix_model_rebalance_strategy_session",
            "strategy_id",
            "effective_session",
        ),
    )


class ModelRebalanceLeg(Base):
    """One ordered canonical signal in an immutable model rebalance."""

    __tablename__ = "model_rebalance_legs"

    rebalance_id: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    leg_id: Mapped[str] = mapped_column(String(128), nullable=False)
    leg_sha256: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    signal_snapshot: Mapped[Any] = mapped_column(JSONType, nullable=False)
    signal_snapshot_sha256: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    rank_snapshot_id: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    factor_snapshot_id: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    current_rank_row_present: Mapped[bool] = mapped_column(Boolean, nullable=False)
    current_rank_instr_id: Mapped[int | None] = mapped_column(Integer)
    current_rank_factor_snapshot_id: Mapped[str | None] = mapped_column(String(_DIGEST_LENGTH))
    prior_model_rebalance_id: Mapped[str | None] = mapped_column(String(_DIGEST_LENGTH))
    prior_model_leg_id: Mapped[str | None] = mapped_column(String(128))
    membership_change_reason: Mapped[str | None] = mapped_column(String(500))
    instr_id: Mapped[int] = mapped_column(Integer, nullable=False)
    external_signal_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("canonical_signals.external_signal_id", ondelete="RESTRICT"),
        nullable=False,
    )
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    rank_position: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    allocation_hint: Mapped[Decimal] = mapped_column(Numeric(18, 12), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["rebalance_id", "rank_snapshot_id"],
            ["model_rebalances.rebalance_id", "model_rebalances.rank_snapshot_id"],
            name="fk_model_rebalance_leg_rank",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["rank_snapshot_id", "current_rank_instr_id"],
            [
                "equity_rank_snapshot_rows.rank_snapshot_id",
                "equity_rank_snapshot_rows.instr_id",
            ],
            name="fk_model_rebalance_leg_current_rank_row",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "rank_snapshot_id",
                "current_rank_instr_id",
                "current_rank_factor_snapshot_id",
            ],
            [
                "equity_rank_snapshot_rows.rank_snapshot_id",
                "equity_rank_snapshot_rows.instr_id",
                "equity_rank_snapshot_rows.factor_snapshot_id",
            ],
            name="fk_model_rebalance_leg_rank_row",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["factor_snapshot_id", "instr_id"],
            [
                "equity_factor_snapshots.factor_snapshot_id",
                "equity_factor_snapshots.instr_id",
            ],
            name="fk_model_rebalance_leg_factor",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "prior_model_rebalance_id",
                "prior_model_leg_id",
                "instr_id",
                "factor_snapshot_id",
            ],
            [
                "model_rebalance_legs.rebalance_id",
                "model_rebalance_legs.leg_id",
                "model_rebalance_legs.instr_id",
                "model_rebalance_legs.factor_snapshot_id",
            ],
            name="fk_model_rebalance_leg_prior_identity",
            ondelete="RESTRICT",
        ),
        CheckConstraint("sequence >= 0", name="ck_model_rebalance_leg_sequence"),
        CheckConstraint(
            "length(trim(leg_id)) > 0 AND length(leg_sha256) = 64 "
            "AND length(signal_snapshot_sha256) = 64 "
            "AND length(rank_snapshot_id) = 64 AND length(factor_snapshot_id) = 64 "
            "AND (current_rank_factor_snapshot_id IS NULL "
            "OR length(current_rank_factor_snapshot_id) = 64) "
            "AND (prior_model_rebalance_id IS NULL "
            "OR length(prior_model_rebalance_id) = 64)",
            name="ck_model_rebalance_leg_identity",
        ),
        CheckConstraint(
            "(current_rank_row_present AND current_rank_instr_id IS NOT NULL "
            "AND current_rank_instr_id = instr_id AND "
            "((current_rank_factor_snapshot_id IS NOT NULL "
            "AND current_rank_factor_snapshot_id = factor_snapshot_id) OR "
            "(phase = 'exit' AND current_rank_factor_snapshot_id IS NULL "
            "AND rank_position IS NULL "
            "AND membership_change_reason IS NOT NULL "
            "AND length(trim(membership_change_reason)) > 0 "
            "AND (membership_change_reason <> 'panel_excluded' OR "
            "(prior_model_rebalance_id IS NOT NULL "
            "AND prior_model_leg_id IS NOT NULL))))) OR "
            "(NOT current_rank_row_present AND current_rank_instr_id IS NULL "
            "AND phase = 'exit' AND current_rank_factor_snapshot_id IS NULL "
            "AND rank_position IS NULL AND prior_model_rebalance_id IS NOT NULL "
            "AND prior_model_leg_id IS NOT NULL "
            "AND membership_change_reason IS NOT NULL "
            "AND membership_change_reason = 'left_effective_universe')",
            name="ck_model_rebalance_leg_current_or_prior_lineage",
        ),
        CheckConstraint(
            "phase IN ('exit', 'reduce', 'hold', 'entry')",
            name="ck_model_rebalance_leg_phase",
        ),
        CheckConstraint(
            "action IN ('long', 'hold', 'flat')",
            name="ck_model_rebalance_leg_action",
        ),
        CheckConstraint(
            "(phase = 'entry' AND action = 'long') OR "
            "(phase IN ('hold', 'reduce') AND action = 'hold') OR "
            "(phase = 'exit' AND action = 'flat')",
            name="ck_model_rebalance_leg_phase_action",
        ),
        CheckConstraint(
            "rank_position IS NULL OR rank_position > 0",
            name="ck_model_rebalance_leg_rank",
        ),
        CheckConstraint(
            "allocation_hint >= 0 AND allocation_hint <= 1 "
            "AND (phase <> 'exit' OR allocation_hint = 0)",
            name="ck_model_rebalance_leg_allocation",
        ),
        UniqueConstraint("rebalance_id", "leg_id", name="uq_model_rebalance_leg_id"),
        UniqueConstraint(
            "rebalance_id",
            "leg_id",
            "instr_id",
            "factor_snapshot_id",
            name="uq_model_rebalance_leg_prior_identity",
        ),
        UniqueConstraint(
            "rebalance_id", "external_signal_id", name="uq_model_rebalance_leg_signal"
        ),
        UniqueConstraint(
            "rebalance_id",
            "sequence",
            "leg_id",
            "instr_id",
            "external_signal_id",
            "signal_snapshot_sha256",
            name="uq_model_rebalance_leg_exact_account_identity",
        ),
        UniqueConstraint(
            "rebalance_id",
            "sequence",
            "leg_id",
            "instr_id",
            "external_signal_id",
            "signal_snapshot_sha256",
            "rank_position",
            name="uq_model_rebalance_leg_exact_account_rank",
        ),
        Index("ix_model_rebalance_leg_instrument", "instr_id"),
    )


def _reject_immutable_change(_mapper: Any, _connection: Any, target: Any) -> None:
    message = f"{target.__class__.__name__} rows are immutable"
    raise ValueError(message)


for _immutable_model in (ModelRebalance, ModelRebalanceLeg):
    event.listen(_immutable_model, "before_update", _reject_immutable_change)
    event.listen(_immutable_model, "before_delete", _reject_immutable_change)


__all__ = ["ModelRebalance", "ModelRebalanceLeg"]
