"""Section O — immutable point-in-time equity factor and rank evidence.

These shared research/model records intentionally carry no tenant RLS owner.
Every result is content-addressed and append-only, with exact observation and
strategy-version lineage instead of opaque mutable snapshots.
"""

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


class EquityFactorSnapshot(Base):
    """Immutable factor result for one instrument at one decision cutoff."""

    __tablename__ = "equity_factor_snapshots"

    factor_snapshot_id: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(50), nullable=False)
    strat_ver_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    instr_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("instruments.instr_id", ondelete="RESTRICT"),
        nullable=False,
    )
    effective_session: Mapped[date] = mapped_column(Date, nullable=False)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    calculation_version: Mapped[str] = mapped_column(String(100), nullable=False)
    configuration_digest: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    source_contract_registry_sha256: Mapped[str] = mapped_column(
        String(_DIGEST_LENGTH),
        nullable=False,
    )
    peer_taxonomy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    peer_group: Mapped[str] = mapped_column(String(150), nullable=False)
    completeness_status: Mapped[str] = mapped_column(String(20), nullable=False)
    expected_factor_count: Mapped[int] = mapped_column(Integer, nullable=False)
    available_factor_count: Mapped[int] = mapped_column(Integer, nullable=False)
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
            name="fk_equity_factor_strategy_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(factor_snapshot_id) = 64 AND length(configuration_digest) = 64 "
            "AND length(source_contract_registry_sha256) = 64 "
            "AND length(content_sha256) = 64",
            name="ck_equity_factor_digests",
        ),
        CheckConstraint(
            "factor_snapshot_id = content_sha256",
            name="ck_equity_factor_content_identity",
        ),
        CheckConstraint(
            "completeness_status IN ('complete', 'incomplete', 'ineligible')",
            name="ck_equity_factor_completeness",
        ),
        CheckConstraint(
            "expected_factor_count >= 0 AND available_factor_count >= 0 "
            "AND available_factor_count <= expected_factor_count "
            "AND (completeness_status <> 'complete' "
            "OR available_factor_count = expected_factor_count)",
            name="ck_equity_factor_counts",
        ),
        CheckConstraint(
            "length(trim(calculation_version)) > 0 "
            "AND length(trim(peer_taxonomy_version)) > 0 "
            "AND length(trim(peer_group)) > 0",
            name="ck_equity_factor_identity",
        ),
        UniqueConstraint(
            "factor_snapshot_id",
            "instr_id",
            name="uq_equity_factor_snapshot_instrument",
        ),
        UniqueConstraint(
            "strategy_id",
            "strat_ver_id",
            "instr_id",
            "effective_session",
            "cutoff_at",
            name="uq_equity_factor_snapshot_cutoff",
        ),
        Index(
            "ix_equity_factor_strategy_session",
            "strategy_id",
            "effective_session",
        ),
        Index("ix_equity_factor_instr_cutoff", "instr_id", "cutoff_at"),
    )


class EquityFactorSnapshotDetail(Base):
    """One complete, disabled, or failed factor in a factor snapshot."""

    __tablename__ = "equity_factor_snapshot_details"

    factor_snapshot_id: Mapped[str] = mapped_column(
        String(_DIGEST_LENGTH),
        ForeignKey("equity_factor_snapshots.factor_snapshot_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    factor_name: Mapped[str] = mapped_column(String(100), primary_key=True)
    sleeve_name: Mapped[str] = mapped_column(String(100), nullable=False)
    factor_version: Mapped[str] = mapped_column(String(100), nullable=False)
    direction: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    raw_value: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    peer_group: Mapped[str | None] = mapped_column(String(150))
    peer_count: Mapped[int | None] = mapped_column(Integer)
    peer_center: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    peer_scale: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    peer_scale_method: Mapped[str | None] = mapped_column(String(40))
    unbounded_normalized_value: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    normalized_value: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    factor_rank: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    weight: Mapped[Decimal] = mapped_column(Numeric(18, 12), nullable=False)
    contribution: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    missing_reason: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        CheckConstraint(
            "length(trim(factor_name)) > 0 AND length(trim(sleeve_name)) > 0 "
            "AND length(trim(factor_version)) > 0",
            name="ck_equity_factor_detail_identity",
        ),
        CheckConstraint(
            "direction IN ('higher_is_better', 'lower_is_better')",
            name="ck_equity_factor_detail_direction",
        ),
        CheckConstraint(
            "state IN ('complete', 'disabled', 'missing', 'stale', 'ambiguous', "
            "'ineligible', 'insufficient_peers')",
            name="ck_equity_factor_detail_state",
        ),
        CheckConstraint(
            "weight >= 0 AND weight <= 1",
            name="ck_equity_factor_detail_weight",
        ),
        CheckConstraint(
            "(enabled AND state <> 'disabled') OR "
            "(NOT enabled AND state = 'disabled' AND weight = 0 "
            "AND raw_value IS NULL AND peer_group IS NULL AND peer_count IS NULL "
            "AND peer_center IS NULL AND peer_scale IS NULL "
            "AND peer_scale_method IS NULL AND unbounded_normalized_value IS NULL "
            "AND normalized_value IS NULL AND factor_rank IS NULL "
            "AND contribution IS NULL)",
            name="ck_equity_disabled_factor_zero",
        ),
        CheckConstraint(
            "state <> 'complete' OR (enabled AND raw_value IS NOT NULL "
            "AND peer_group IS NOT NULL AND peer_count IS NOT NULL AND peer_count > 0 "
            "AND peer_center IS NOT NULL AND peer_scale IS NOT NULL AND peer_scale >= 0 "
            "AND peer_scale_method IS NOT NULL "
            "AND unbounded_normalized_value IS NOT NULL "
            "AND normalized_value IS NOT NULL "
            "AND factor_rank IS NOT NULL AND factor_rank >= 1 "
            "AND contribution IS NOT NULL)",
            name="ck_equity_complete_factor_values",
        ),
        CheckConstraint(
            "peer_scale_method IS NULL OR peer_scale_method IN "
            "('constant', 'median_absolute_deviation', 'standard_deviation_fallback')",
            name="ck_equity_factor_scale_method",
        ),
        CheckConstraint(
            "contribution IS NULL OR abs(contribution - weight * normalized_value) "
            "<= 0.000000000001",
            name="ck_equity_factor_contribution_arithmetic",
        ),
        CheckConstraint(
            "(state IN ('complete', 'disabled') AND missing_reason IS NULL) OR "
            "(state NOT IN ('complete', 'disabled') "
            "AND missing_reason IS NOT NULL AND length(trim(missing_reason)) > 0)",
            name="ck_equity_factor_missing_reason",
        ),
        Index("ix_equity_factor_detail_sleeve", "sleeve_name", "state"),
    )


class EquityFactorEvidence(Base):
    """Exact source observation supporting one factor detail."""

    __tablename__ = "equity_factor_evidence"

    factor_snapshot_id: Mapped[str] = mapped_column(
        String(_DIGEST_LENGTH),
        primary_key=True,
    )
    factor_name: Mapped[str] = mapped_column(String(100), primary_key=True)
    observation_id: Mapped[str] = mapped_column(
        String(_DIGEST_LENGTH),
        ForeignKey("equity_observations.observation_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    evidence_role: Mapped[str] = mapped_column(String(20), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["factor_snapshot_id", "factor_name"],
            [
                "equity_factor_snapshot_details.factor_snapshot_id",
                "equity_factor_snapshot_details.factor_name",
            ],
            name="fk_equity_factor_evidence_detail",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "evidence_role IN ('primary', 'context', 'peer', 'benchmark', "
            "'calendar', 'membership')",
            name="ck_equity_factor_evidence_role",
        ),
        Index("ix_equity_factor_evidence_observation", "observation_id"),
    )


class EquityRankSnapshot(Base):
    """Immutable identity for one complete cross-sectional ranking."""

    __tablename__ = "equity_rank_snapshots"

    rank_snapshot_id: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(50), nullable=False)
    strat_ver_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_session: Mapped[date] = mapped_column(Date, nullable=False)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    configuration_digest: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    panel_revision_digest: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    factor_content_digest: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    data_use_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_authority_digest: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    provider_authority_policy: Mapped[Any] = mapped_column(JSONType, nullable=False)
    peer_taxonomy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    completeness_status: Mapped[str] = mapped_column(String(20), nullable=False)
    expected_instrument_count: Mapped[int] = mapped_column(Integer, nullable=False)
    included_instrument_count: Mapped[int] = mapped_column(Integer, nullable=False)
    excluded_instrument_count: Mapped[int] = mapped_column(Integer, nullable=False)
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
            name="fk_equity_rank_strategy_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(rank_snapshot_id) = 64 "
            "AND length(configuration_digest) = 64 "
            "AND length(panel_revision_digest) = 64 "
            "AND length(factor_content_digest) = 64 "
            "AND length(provider_authority_digest) = 64 "
            "AND length(content_sha256) = 64",
            name="ck_equity_rank_digests",
        ),
        CheckConstraint(
            "rank_snapshot_id = content_sha256",
            name="ck_equity_rank_content_identity",
        ),
        CheckConstraint(
            "data_use_scope IN ('historical_validation', 'paper_forward', 'live_forward')",
            name="ck_equity_rank_data_use_scope",
        ),
        CheckConstraint(
            "completeness_status IN ('complete', 'incomplete', 'ineligible')",
            name="ck_equity_rank_completeness",
        ),
        CheckConstraint(
            "expected_instrument_count >= 0 AND included_instrument_count >= 0 "
            "AND excluded_instrument_count >= 0 "
            "AND included_instrument_count + excluded_instrument_count "
            "= expected_instrument_count",
            name="ck_equity_rank_counts",
        ),
        CheckConstraint(
            "length(trim(peer_taxonomy_version)) > 0",
            name="ck_equity_rank_peer_taxonomy",
        ),
        UniqueConstraint(
            "rank_snapshot_id",
            "strategy_id",
            "strat_ver_id",
            name="uq_equity_rank_strategy_version",
        ),
        UniqueConstraint(
            "rank_snapshot_id",
            "strategy_id",
            "strat_ver_id",
            "effective_session",
            "cutoff_at",
            "configuration_digest",
            "data_use_scope",
            "provider_authority_digest",
            name="uq_equity_rank_exact_model_lineage",
        ),
        UniqueConstraint(
            "strategy_id",
            "strat_ver_id",
            "effective_session",
            "cutoff_at",
            "configuration_digest",
            "factor_content_digest",
            "data_use_scope",
            "provider_authority_digest",
            name="uq_equity_rank_factor_lineage",
        ),
        Index(
            "ix_equity_rank_strategy_session",
            "strategy_id",
            "effective_session",
        ),
    )


class EquityRankSnapshotRow(Base):
    """One effective-universe member's eligibility and ordered rank result."""

    __tablename__ = "equity_rank_snapshot_rows"

    rank_snapshot_id: Mapped[str] = mapped_column(
        String(_DIGEST_LENGTH),
        ForeignKey("equity_rank_snapshots.rank_snapshot_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    instr_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("instruments.instr_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    factor_snapshot_id: Mapped[str | None] = mapped_column(String(_DIGEST_LENGTH))
    # ``eligible`` records rank completeness. A fully ranked instrument can
    # still fail a strategy hard gate, recorded separately below.
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    strategy_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    row_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    rank_position: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    composite_score: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    target_allocation_hint: Mapped[Decimal | None] = mapped_column(Numeric(18, 12))
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    incumbent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    exclusion_reason: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        ForeignKeyConstraint(
            ["factor_snapshot_id", "instr_id"],
            [
                "equity_factor_snapshots.factor_snapshot_id",
                "equity_factor_snapshots.instr_id",
            ],
            name="fk_equity_rank_row_factor",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "decision IN ('selected', 'hold', 'eligible', 'excluded', 'exit')",
            name="ck_equity_rank_row_decision",
        ),
        CheckConstraint("row_ordinal >= 0", name="ck_equity_rank_row_ordinal"),
        CheckConstraint(
            "(eligible AND factor_snapshot_id IS NOT NULL "
            "AND rank_position IS NOT NULL AND rank_position > 0 "
            "AND composite_score IS NOT NULL) OR "
            "(NOT eligible AND factor_snapshot_id IS NULL AND rank_position IS NULL "
            "AND composite_score IS NULL)",
            name="ck_equity_rank_row_eligibility",
        ),
        CheckConstraint(
            "(strategy_eligible AND eligible "
            "AND decision IN ('selected', 'hold', 'eligible') "
            "AND exclusion_reason IS NULL) OR "
            "(NOT strategy_eligible AND decision IN ('excluded', 'exit') "
            "AND target_allocation_hint IS NULL "
            "AND exclusion_reason IS NOT NULL)",
            name="ck_equity_rank_row_strategy_eligibility",
        ),
        CheckConstraint(
            "target_allocation_hint IS NULL OR "
            "(target_allocation_hint >= 0 AND target_allocation_hint <= 1)",
            name="ck_equity_rank_row_allocation",
        ),
        UniqueConstraint(
            "rank_snapshot_id",
            "row_ordinal",
            name="uq_equity_rank_row_ordinal",
        ),
        UniqueConstraint(
            "rank_snapshot_id",
            "instr_id",
            "factor_snapshot_id",
            name="uq_equity_rank_row_factor",
        ),
        Index("ix_equity_rank_row_factor", "factor_snapshot_id"),
        Index("ix_equity_rank_row_instrument", "instr_id"),
        Index(
            "ix_equity_rank_row_position",
            "rank_snapshot_id",
            "rank_position",
            "row_ordinal",
        ),
    )


def _reject_equity_evidence_update(
    _mapper: Any,
    _connection: Any,
    target: Any,
) -> None:
    msg = f"{target.__class__.__name__} rows are immutable"
    raise ValueError(msg)


def _reject_equity_evidence_delete(
    _mapper: Any,
    _connection: Any,
    target: Any,
) -> None:
    msg = f"{target.__class__.__name__} rows are append-only and cannot be deleted"
    raise ValueError(msg)


for _immutable_model in (
    EquityFactorSnapshot,
    EquityFactorSnapshotDetail,
    EquityFactorEvidence,
    EquityRankSnapshot,
    EquityRankSnapshotRow,
):
    event.listen(_immutable_model, "before_update", _reject_equity_evidence_update)
    event.listen(_immutable_model, "before_delete", _reject_equity_evidence_delete)


__all__ = [
    "EquityFactorEvidence",
    "EquityFactorSnapshot",
    "EquityFactorSnapshotDetail",
    "EquityRankSnapshot",
    "EquityRankSnapshotRow",
]
