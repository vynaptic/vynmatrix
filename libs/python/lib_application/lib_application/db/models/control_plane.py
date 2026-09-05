"""Section G — User Control Plane.

Tables: ``user_trading_policies``, ``user_budget_buckets``,
``sizing_profiles``, ``user_strategy_bindings``.
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
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lib_common.asset_classes import ASSET_CLASS_CHECK_SQL

from ._base import ArrayType, Base, JSONType, SQLiteBigInteger

if TYPE_CHECKING:
    from .identity import User
    from .strategies import Strategy


class UserTradingPolicy(Base):
    """User trading preferences per asset class and horizon."""

    __tablename__ = "user_trading_policies"

    policy_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(50), ForeignKey("users.user_id"), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(20), nullable=False)
    horizon: Mapped[str] = mapped_column(String(20), nullable=False)
    methods_allowed: Mapped[Any] = mapped_column(ArrayType, nullable=False)
    default_method: Mapped[str | None] = mapped_column(String(30))
    sizing_rules: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    risk_overrides: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint(
            ASSET_CLASS_CHECK_SQL,
            name="ck_policy_asset_class",
        ),
        CheckConstraint("horizon IN ('intraday', 'swing', 'long_term')", name="ck_policy_horizon"),
        UniqueConstraint("user_id", "asset_class", "horizon", name="uq_user_policy"),
    )

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="trading_policies")


class UserBudgetBucket(Base):
    """Budget buckets per asset class and horizon."""

    __tablename__ = "user_budget_buckets"

    bucket_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(50), ForeignKey("users.user_id"), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(20), nullable=False)
    horizon: Mapped[str] = mapped_column(String(20), nullable=False)
    ccy: Mapped[str] = mapped_column(
        String(10), nullable=False, default="USD", server_default="USD"
    )
    target_alloc_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    hard_cap: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    rebalance_policy: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint(ASSET_CLASS_CHECK_SQL, name="ck_budget_asset_class"),
        UniqueConstraint("user_id", "asset_class", "horizon", name="uq_user_budget"),
    )

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="budget_buckets")


class SizingProfile(Base):
    """Reusable position sizing profiles for users."""

    __tablename__ = "sizing_profiles"

    profile_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(50), ForeignKey("users.user_id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    # Sizing method
    method: Mapped[str] = mapped_column(
        String(30), nullable=False
    )  # fixed_amount, fixed_pct, risk_pct, kelly, volatility

    # Method-specific parameters
    params: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    # e.g., {"fixed_amount": 1000, "fixed_pct": 0.02, "risk_pct": 0.01, "kelly_fraction": 0.25}

    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "method IN ('fixed_amount', 'fixed_pct', 'risk_pct', 'kelly', 'volatility')",
            name="ck_sizing_method",
        ),
    )


class UserStrategyBinding(Base):
    """User's binding to strategies with thresholds and execution preferences."""

    __tablename__ = "user_strategy_bindings"

    binding_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(50), ForeignKey("users.user_id"), nullable=False)
    strategy_id: Mapped[str | None] = mapped_column(
        String(50), ForeignKey("strategies.strategy_id")
    )
    # NULL strategy_id means "any strategy" for filtering by asset/sector
    broker_account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("linked_broker_accounts.account_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Thresholds
    asset_score_threshold: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=0.60, server_default="0.60"
    )
    sector_score_threshold: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4)
    )  # NULL = no sector check
    market_score_threshold: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4)
    )  # NULL = no market check

    # Execution preferences
    execution_modes_allowed: Mapped[Any] = mapped_column(
        ArrayType, nullable=False, default=["spot"]
    )
    preferred_mode: Mapped[str | None] = mapped_column(String(30))
    mode_selection_policy: Mapped[str] = mapped_column(
        String(30), nullable=False, default="fixed", server_default="fixed"
    )
    # fixed, best_return, lowest_risk, highest_sharpe

    # Filters
    asset_classes_allowed: Mapped[Any] = mapped_column(
        ArrayType, nullable=False, default=["crypto"]
    )
    instruments_allowed: Mapped[Any | None] = mapped_column(ArrayType)  # NULL = all instruments
    sectors_allowed: Mapped[Any | None] = mapped_column(ArrayType)  # NULL = all sectors

    # Sizing and risk
    sizing_profile_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("sizing_profiles.profile_id")
    )
    max_position_pct: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=0.10, server_default="0.10"
    )
    max_total_exposure_pct: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=0.50, server_default="0.50"
    )
    max_daily_loss_pct: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=0.05, server_default="0.05"
    )
    max_open_positions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10, server_default="10"
    )
    entry_cash_buffer_bps: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))

    # Broker restrictions
    allowed_brokers: Mapped[Any | None] = mapped_column(ArrayType)  # NULL = no broker restriction

    # Control
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    autopilot: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    entries_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    exits_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
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
            (
                "mode_selection_policy IN ("
                "'fixed', 'best_return', 'lowest_risk', 'highest_sharpe', 'user_rotating'"
                ")"
            ),
            name="ck_mode_selection_policy",
        ),
        CheckConstraint(
            "asset_score_threshold >= 0 AND asset_score_threshold <= 1",
            name="ck_binding_asset_score_threshold_range",
        ),
        CheckConstraint(
            "sector_score_threshold IS NULL OR "
            "(sector_score_threshold >= 0 AND sector_score_threshold <= 1)",
            name="ck_binding_sector_score_threshold_range",
        ),
        CheckConstraint(
            "market_score_threshold IS NULL OR "
            "(market_score_threshold >= 0 AND market_score_threshold <= 1)",
            name="ck_binding_market_score_threshold_range",
        ),
        CheckConstraint(
            "max_position_pct > 0 AND max_position_pct <= 1",
            name="ck_binding_max_position_pct_range",
        ),
        CheckConstraint(
            "max_total_exposure_pct > 0 AND max_total_exposure_pct <= 1",
            name="ck_binding_max_total_exposure_pct_range",
        ),
        CheckConstraint(
            "max_daily_loss_pct >= 0 AND max_daily_loss_pct <= 1",
            name="ck_binding_max_daily_loss_pct_range",
        ),
        CheckConstraint(
            "max_open_positions > 0 AND max_open_positions <= 1000",
            name="ck_binding_max_open_positions_range",
        ),
        CheckConstraint(
            "entry_cash_buffer_bps IS NULL OR "
            "(entry_cash_buffer_bps > 0 AND entry_cash_buffer_bps <= 1000)",
            name="ck_binding_entry_cash_buffer_bps_range",
        ),
        CheckConstraint(
            "is_active OR (NOT entries_enabled AND NOT exits_enabled)",
            name="ck_binding_inactive_has_no_authority",
        ),
        CheckConstraint(
            "NOT is_active OR (strategy_id IS NOT NULL AND length(trim(strategy_id)) > 0)",
            name="ck_binding_active_strategy_required",
        ),
        CheckConstraint(
            "autopilot OR NOT entries_enabled",
            name="ck_binding_entries_require_autopilot",
        ),
        ForeignKeyConstraint(
            ["broker_account_id", "user_id"],
            ["linked_broker_accounts.account_id", "linked_broker_accounts.user_id"],
            name="fk_binding_broker_account_owner",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "binding_id",
            "user_id",
            "broker_account_id",
            "strategy_id",
            name="uq_user_strategy_binding_owner",
        ),
        UniqueConstraint(
            "user_id",
            "strategy_id",
            "broker_account_id",
            name="uq_user_strategy_binding_account",
        ),
        Index(
            "uq_user_strategy_binding_wildcard_account",
            "user_id",
            "broker_account_id",
            unique=True,
            postgresql_where=text("strategy_id IS NULL"),
            sqlite_where=text("strategy_id IS NULL"),
        ),
    )

    # Relationships
    user: Mapped[User] = relationship("User")
    strategy: Mapped[Strategy] = relationship("Strategy")
    sizing_profile: Mapped[SizingProfile] = relationship("SizingProfile")


__all__ = [
    "SizingProfile",
    "UserBudgetBucket",
    "UserStrategyBinding",
    "UserTradingPolicy",
]
