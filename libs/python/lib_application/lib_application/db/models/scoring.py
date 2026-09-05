"""Section K — Scoring Engine.

Tables: ``sectors``, ``instrument_sectors``, ``canonical_signals``,
``asset_scores``, ``sector_scores``, ``market_scores``, ``mode_performance``,
``scoring_rules``.

The scoring engine's core persistence surface. The OMS-edge audit/durability
tables (``execution_decision_logs``, ``pending_orders``) and the transactional
outbox (``outbox_events``) the engine also writes to live in the sibling
:mod:`.dispatch` submodule to keep each file under the audit LOC cap.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lib_common.asset_classes import (
    ASSET_CLASS_CHECK_SQL,
    NULLABLE_ASSET_CLASS_CHECK_SQL,
)
from lib_common.internal_events import EXTERNAL_SIGNAL_ID_MAX_LENGTH

from ._base import Base, JSONType, SQLiteBigInteger

if TYPE_CHECKING:
    from .instruments import Instrument


class Sector(Base):
    """Sector and industry hierarchy for grouping instruments."""

    __tablename__ = "sectors"

    sector_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(20), nullable=False)
    parent_sector_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("sectors.sector_id"))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            ASSET_CLASS_CHECK_SQL,
            name="ck_sector_asset_class",
        ),
    )

    # Relationships
    parent: Mapped[Sector] = relationship("Sector", remote_side=[sector_id], backref="children")
    instruments: Mapped[list[InstrumentSector]] = relationship(
        "InstrumentSector", back_populates="sector"
    )


class InstrumentSector(Base):
    """Many-to-many mapping of instruments to sectors with weights."""

    __tablename__ = "instrument_sectors"

    instr_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("instruments.instr_id", ondelete="CASCADE"),
        primary_key=True,
    )
    sector_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("sectors.sector_id", ondelete="CASCADE"),
        primary_key=True,
    )
    weight: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=1.0)

    # Relationships
    instrument: Mapped[Instrument] = relationship("Instrument")
    sector: Mapped[Sector] = relationship("Sector", back_populates="instruments")


class CanonicalSignal(Base):
    """Normalized indicator-strategy signals with provenance."""

    __tablename__ = "canonical_signals"

    signal_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("strategies.strategy_id"), nullable=False
    )
    strat_ver_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("strategy_versions.strat_ver_id")
    )
    instr_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("instruments.instr_id"), nullable=False
    )
    sector_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("sectors.sector_id"))

    # Signal data
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    raw_score: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    direction: Mapped[str | None] = mapped_column(String(10))  # long, short, flat
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    expected_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))  # mu over horizon
    predicted_risk: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))  # sigma over horizon
    horizon_seconds: Mapped[int | None] = mapped_column(BigInteger)  # prediction horizon in seconds

    # Metadata
    features: Mapped[Any | None] = mapped_column(JSONType)
    signal_meta: Mapped[Any | None] = mapped_column(
        JSONType
    )  # Renamed from 'metadata' (reserved word)
    source_runner: Mapped[str | None] = mapped_column(String(50))  # indicator
    run_id: Mapped[str | None] = mapped_column(
        String(64), index=True
    )  # Cross-container correlation ID

    # Idempotency: deterministic key from strategy_id + strategy_version + symbol
    # + normalized action + bar close ts + entry/exit reason.
    external_signal_id: Mapped[str] = mapped_column(
        String(EXTERNAL_SIGNAL_ID_MAX_LENGTH),
        nullable=False,
        unique=True,
    )

    ts: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(tz=UTC), index=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        CheckConstraint(
            "action IN ('long', 'short', 'flat', 'hold', 'open_spread', 'close_spread')",
            name="ck_canonical_action",
        ),
        CheckConstraint("direction IN ('long', 'short', 'flat')", name="ck_canonical_direction"),
        CheckConstraint(
            "length(trim(external_signal_id)) > 0",
            name="ck_canonical_signal_external_identity",
        ),
        Index("ix_canonical_signal_instr_ts", "instr_id", "ts"),
        Index("ix_canonical_signal_strategy_ts", "strategy_id", "ts"),
        Index("ix_canonical_signal_ext_id", "external_signal_id", unique=True),
    )


class AssetScore(Base):
    """Aggregated scores for individual assets/instruments."""

    __tablename__ = "asset_scores"

    score_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    instr_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("instruments.instr_id"), nullable=False
    )

    # Score data
    score_value: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    components: Mapped[Any] = mapped_column(
        JSONType, nullable=False
    )  # {"strategy_id": contribution}
    weights_applied: Mapped[Any] = mapped_column(JSONType, nullable=False)
    aggregation_method: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="weighted_average",
        server_default="weighted_average",
    )

    # Confidence and decay
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    decay_factor: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4), default=1.0, server_default="1.0"
    )
    signals_count: Mapped[int | None] = mapped_column(Integer, default=0, server_default="0")

    # Stable identity of the originating signal (Signal.external_signal_id). Lets the
    # score write be idempotent on re-delivery (NOTIFY redelivery / worker restart):
    # a re-POST of the same signal UPDATEs its row instead of inserting a duplicate
    # (SC-6). Distinct same-bar signals from different strategies carry
    # different IDs and remain separate rows.
    external_signal_id: Mapped[str] = mapped_column(
        String(EXTERNAL_SIGNAL_ID_MAX_LENGTH),
        nullable=False,
        unique=True,
        index=True,
    )

    ts: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(tz=UTC), index=True
    )
    valid_until: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        CheckConstraint(
            "length(trim(external_signal_id)) > 0",
            name="ck_asset_score_external_identity",
        ),
        Index("ix_asset_score_instr_ts", "instr_id", "ts"),
    )


class SectorScore(Base):
    """Aggregated scores for sectors (groups of instruments)."""

    __tablename__ = "sector_scores"

    score_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    sector_id: Mapped[int] = mapped_column(Integer, ForeignKey("sectors.sector_id"), nullable=False)

    # Score data
    score_value: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    constituent_scores: Mapped[Any] = mapped_column(JSONType, nullable=False)  # {"instr_id": score}
    aggregation_method: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="weighted_average",
        server_default="weighted_average",
    )

    ts: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(tz=UTC), index=True
    )

    __table_args__ = (Index("ix_sector_score_ts", "sector_id", "ts"),)


class MarketScore(Base):
    """Aggregated scores for entire markets/asset classes."""

    __tablename__ = "market_scores"

    score_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    asset_class: Mapped[str] = mapped_column(String(20), nullable=False)

    # Score data
    score_value: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    sector_scores: Mapped[Any] = mapped_column(JSONType, nullable=False)  # {"sector_id": score}

    ts: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(tz=UTC), index=True
    )

    __table_args__ = (
        CheckConstraint(
            ASSET_CLASS_CHECK_SQL,
            name="ck_market_score_asset_class",
        ),
    )


class ModePerformance(Base):
    """Historical performance metrics per execution mode for mode optimization."""

    __tablename__ = "mode_performance"

    perf_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("linked_broker_accounts.account_id", ondelete="CASCADE"),
        nullable=False,
    )
    strategy_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("strategies.strategy_id", ondelete="CASCADE"),
        nullable=False,
    )

    # Scope (at least one must be set)
    instr_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("instruments.instr_id"))
    sector_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("sectors.sector_id"))
    asset_class: Mapped[str | None] = mapped_column(String(20))

    # Mode and horizon
    execution_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    horizon: Mapped[str] = mapped_column(String(20), nullable=False)

    # Performance metrics
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    total_return: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    sharpe_ratio: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    sortino_ratio: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    win_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    avg_win: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    avg_loss: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    max_drawdown: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    sample_size: Mapped[int | None] = mapped_column(Integer)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "execution_mode IN ('spot', 'margin', 'perpetual', 'futures', "
            "'bull_call', 'bear_put', 'bull_put', 'bear_call', "
            "'iron_condor', 'straddle', 'strangle', 'options_single')",
            name="ck_mode_perf_exec_mode",
        ),
        CheckConstraint(
            "horizon IN ('intraday', 'swing', 'long_term')", name="ck_mode_perf_horizon"
        ),
        CheckConstraint(
            NULLABLE_ASSET_CLASS_CHECK_SQL,
            name="ck_mode_performance_asset_class",
        ),
        CheckConstraint(
            "instr_id IS NOT NULL OR sector_id IS NOT NULL OR asset_class IS NOT NULL",
            name="ck_mode_performance_scope_required",
        ),
        Index(
            "uq_mode_performance_instrument_scope",
            "account_id",
            "strategy_id",
            "instr_id",
            "execution_mode",
            "horizon",
            unique=True,
            postgresql_where=text("instr_id IS NOT NULL"),
            sqlite_where=text("instr_id IS NOT NULL"),
        ),
        Index(
            "uq_mode_performance_sector_scope",
            "account_id",
            "strategy_id",
            "sector_id",
            "execution_mode",
            "horizon",
            unique=True,
            postgresql_where=text("instr_id IS NULL AND sector_id IS NOT NULL"),
            sqlite_where=text("instr_id IS NULL AND sector_id IS NOT NULL"),
        ),
        Index(
            "uq_mode_performance_asset_class_scope",
            "account_id",
            "strategy_id",
            "asset_class",
            "execution_mode",
            "horizon",
            unique=True,
            postgresql_where=text(
                "instr_id IS NULL AND sector_id IS NULL AND asset_class IS NOT NULL"
            ),
            sqlite_where=text("instr_id IS NULL AND sector_id IS NULL AND asset_class IS NOT NULL"),
        ),
        Index(
            "ix_mode_perf_lookup",
            "account_id",
            "strategy_id",
            "instr_id",
            "execution_mode",
            "horizon",
        ),
    )


class ScoringRule(Base):
    """Configurable scoring rules for signal aggregation."""

    __tablename__ = "scoring_rules"

    rule_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # Target scope
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)  # asset, sector, market
    asset_class: Mapped[str | None] = mapped_column(String(20))  # NULL = all asset classes

    # Aggregation configuration
    aggregation_method: Mapped[str] = mapped_column(
        String(50), nullable=False, default="weighted_average"
    )
    min_quorum: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    decay_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    decay_half_life_hours: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=24.0)

    # Strategy weights (JSON array)
    strategy_weights: Mapped[Any] = mapped_column(JSONType, nullable=False, default=list)
    # e.g., [{"strategy_id": 1, "weight": 0.6, "min_confidence": 0.3}, ...]

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "target_type IN ('asset', 'sector', 'market')",
            name="ck_scoring_rule_target",
        ),
        CheckConstraint(
            "aggregation_method IN ('weighted_average', 'majority_vote', 'unanimous', "
            "'max_confidence', 'bayesian', 'min_confidence')",
            name="ck_scoring_rule_agg_method",
        ),
        CheckConstraint(
            NULLABLE_ASSET_CLASS_CHECK_SQL,
            name="ck_scoring_rule_asset_class",
        ),
    )


__all__ = [
    "AssetScore",
    "CanonicalSignal",
    "InstrumentSector",
    "MarketScore",
    "ModePerformance",
    "ScoringRule",
    "Sector",
    "SectorScore",
]
