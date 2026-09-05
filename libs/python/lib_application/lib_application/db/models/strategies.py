"""Section E — Strategy Catalog & Versions.

Tables: ``strategies``, ``strategy_versions``, ``user_strategy_configs``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lib_common.asset_classes import NULLABLE_ASSET_CLASS_CHECK_SQL

from ._base import Base, JSONType, SQLiteBigInteger, generate_uuid

if TYPE_CHECKING:
    from .identity import User


class Strategy(Base):
    """Strategy definitions."""

    __tablename__ = "strategies"

    # Column names match migration 0002_core_app_schema
    strategy_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    strategy_name: Mapped[str] = mapped_column(String(255), nullable=False)
    asset_class: Mapped[str | None] = mapped_column(String(50))
    description: Mapped[str | None] = mapped_column(String(500))
    is_active: Mapped[bool | None] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    # Relationships
    versions: Mapped[list[StrategyVersion]] = relationship(
        "StrategyVersion", back_populates="strategy"
    )
    user_configs: Mapped[list[UserStrategyConfig]] = relationship(
        "UserStrategyConfig", back_populates="strategy"
    )

    __table_args__ = (
        CheckConstraint(
            NULLABLE_ASSET_CLASS_CHECK_SQL,
            name="ck_strategy_asset_class",
        ),
    )


class StrategyVersion(Base):
    """Immutable strategy release configuration."""

    __tablename__ = "strategy_versions"

    strat_ver_id: Mapped[int] = mapped_column(
        SQLiteBigInteger, primary_key=True, autoincrement=True
    )
    strategy_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("strategies.strategy_id"), nullable=False
    )
    semver: Mapped[str] = mapped_column(String(20), nullable=False)
    docker_image: Mapped[str | None] = mapped_column(String(500))
    git_repo: Mapped[str | None] = mapped_column(String(500))
    git_commit: Mapped[str | None] = mapped_column(String(100))
    param_schema: Mapped[Any] = mapped_column(JSONType, nullable=False)
    default_params: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    released_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "strat_ver_id",
            "strategy_id",
            name="uq_strategy_version_lineage",
        ),
        UniqueConstraint("strategy_id", "semver", name="uq_strategy_version_semver"),
        CheckConstraint("status IN ('active', 'deprecated', 'pulled')", name="ck_version_status"),
    )

    # Relationships
    strategy: Mapped[Strategy] = relationship("Strategy", back_populates="versions")


class UserStrategyConfig(Base):
    """User-specific strategy execution configuration."""

    __tablename__ = "user_strategy_configs"
    __table_args__ = (UniqueConstraint("user_id", "strategy_id", name="uq_user_strategy"),)

    config_id: Mapped[str] = mapped_column(String(50), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(String(50), ForeignKey("users.user_id"), nullable=False)
    strategy_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("strategies.strategy_id"), nullable=False
    )
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    parameters: Mapped[Any | None] = mapped_column(JSONType)
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

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="strategy_configs")
    strategy: Mapped[Strategy] = relationship("Strategy", back_populates="user_configs")


class StrategyRuntimeState(Base):
    """Latest durable shared-model snapshot for one indicator worker.

    The payload is strategy-model state, not a tenant or broker position. The
    surrounding identity columns form the restoration contract and are checked
    against the active strategy configuration before a worker accepts live
    market data.
    """

    __tablename__ = "strategy_runtime_states"

    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("strategies.strategy_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    strategy_version: Mapped[str] = mapped_column(String(20), nullable=False)
    symbols: Mapped[Any] = mapped_column(JSONType, nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(20), nullable=False)
    consolidation_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    panel_runtime_environment: Mapped[str | None] = mapped_column(String(32))
    panel_data_use_scope: Mapped[str | None] = mapped_column(String(32))
    panel_entitlement_owner_user_id: Mapped[str | None] = mapped_column(
        String(50),
        ForeignKey(
            "users.user_id",
            name="fk_strategy_runtime_panel_owner",
            ondelete="RESTRICT",
        ),
    )
    panel_activation_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state_payload: Mapped[Any] = mapped_column(JSONType, nullable=False)
    last_source_symbol: Mapped[str | None] = mapped_column(String(50))
    last_source_price_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("prices.price_id", ondelete="RESTRICT"),
    )
    last_source_ts: Mapped[datetime | None] = mapped_column(DateTime)
    last_source_content_revision: Mapped[int | None] = mapped_column(BigInteger)
    generation: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default="1",
    )
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
        CheckConstraint(
            "length(trim(strategy_version)) > 0",
            name="ck_strategy_runtime_version_nonempty",
        ),
        CheckConstraint(
            "length(config_fingerprint) = 64",
            name="ck_strategy_runtime_config_fingerprint",
        ),
        CheckConstraint(
            "(panel_runtime_environment IS NULL "
            "AND panel_data_use_scope IS NULL "
            "AND panel_entitlement_owner_user_id IS NULL "
            "AND panel_activation_cutoff IS NULL) OR "
            "(length(trim(panel_runtime_environment)) > 0 "
            "AND panel_data_use_scope = 'paper_forward' "
            "AND length(trim(panel_entitlement_owner_user_id)) > 0 "
            "AND panel_activation_cutoff IS NOT NULL)",
            name="ck_strategy_runtime_panel_binding",
        ),
        CheckConstraint(
            "consolidation_minutes >= 0",
            name="ck_strategy_runtime_consolidation_nonnegative",
        ),
        CheckConstraint(
            "state_schema_version > 0",
            name="ck_strategy_runtime_state_schema_positive",
        ),
        CheckConstraint(
            "generation > 0",
            name="ck_strategy_runtime_generation_positive",
        ),
        CheckConstraint(
            "last_source_content_revision IS NULL OR last_source_content_revision > 0",
            name="ck_strategy_runtime_source_revision_positive",
        ),
        Index(
            "ix_strategy_runtime_identity",
            "strategy_id",
            "strategy_version",
            "source",
            "timeframe",
        ),
    )


class StrategyDecision(Base):
    """Append-only strategy-bar decision journal.

    ``signal_envelopes`` is empty for a no-signal decision. Emitted envelopes
    are copied to the transactional outbox in the same commit; this journal
    retains the deterministic strategy decision and source-bar provenance.
    """

    __tablename__ = "strategy_decisions"

    decision_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    worker_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("strategy_runtime_states.worker_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    strategy_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("strategies.strategy_id", ondelete="RESTRICT"),
        nullable=False,
    )
    strategy_version: Mapped[str] = mapped_column(String(20), nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(20), nullable=False)
    consolidation_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    strategy_bar_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source_price_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("prices.price_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_content_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    signal_envelopes: Mapped[Any] = mapped_column(JSONType, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "length(decision_key) = 64",
            name="ck_strategy_decision_key",
        ),
        CheckConstraint(
            "length(trim(strategy_version)) > 0",
            name="ck_strategy_decision_version_nonempty",
        ),
        CheckConstraint(
            "consolidation_minutes >= 0",
            name="ck_strategy_decision_consolidation_nonnegative",
        ),
        CheckConstraint(
            "source_content_revision > 0",
            name="ck_strategy_decision_source_revision_positive",
        ),
        CheckConstraint(
            "state_schema_version > 0",
            name="ck_strategy_decision_state_schema_positive",
        ),
        UniqueConstraint(
            "worker_id",
            "symbol",
            "strategy_bar_ts",
            "source_content_revision",
            name="uq_strategy_decision_bar_revision",
        ),
        Index(
            "ix_strategy_decision_source",
            "source_price_id",
            "source_content_revision",
        ),
    )


__all__ = [
    "Strategy",
    "StrategyDecision",
    "StrategyRuntimeState",
    "StrategyVersion",
    "UserStrategyConfig",
]
