"""Deployment owner designation and preserved historical user identity."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._base import Base, generate_uuid

if TYPE_CHECKING:
    from .brokers import LinkedBrokerAccount
    from .control_plane import UserBudgetBucket, UserTradingPolicy
    from .strategies import UserStrategyConfig


class User(Base):
    """Stable user identities, with at most one designated deployment owner."""

    __tablename__ = "users"

    is_deployment_owner: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    user_id: Mapped[str] = mapped_column(String(50), primary_key=True, default=generate_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    tz: Mapped[str | None] = mapped_column(String(50), default="Europe/Amsterdam")
    base_ccy: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="active",
        server_default="active",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        Index(
            "uq_users_deployment_owner",
            "is_deployment_owner",
            unique=True,
            postgresql_where=text("is_deployment_owner"),
            sqlite_where=text("is_deployment_owner = 1"),
        ),
        CheckConstraint("status IN ('active', 'suspended', 'closed')", name="ck_user_status"),
        CheckConstraint(
            "base_ccy = upper(trim(base_ccy)) AND length(base_ccy) BETWEEN 3 AND 10",
            name="ck_user_base_ccy",
        ),
    )

    # Relationships
    linked_accounts: Mapped[list[LinkedBrokerAccount]] = relationship(
        "LinkedBrokerAccount", back_populates="user"
    )
    trading_policies: Mapped[list[UserTradingPolicy]] = relationship(
        "UserTradingPolicy", back_populates="user"
    )
    budget_buckets: Mapped[list[UserBudgetBucket]] = relationship(
        "UserBudgetBucket", back_populates="user"
    )
    strategy_configs: Mapped[list[UserStrategyConfig]] = relationship(
        "UserStrategyConfig", back_populates="user"
    )


__all__ = ["User"]
