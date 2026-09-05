"""Section A — Core Tenancy & Users.

Tables: ``orgs``, ``users``, ``user_roles``, ``plans``,
``user_plan_subscriptions``.

Extracted from the historical 2 006-LOC ``models.py`` as part of the
domain-by-domain split. Classes register against the shared ``Base`` exposed
by :mod:`._base`; cross-section ``relationship(...)`` strings continue to
resolve through ``Base.registry`` once every domain submodule is imported by
``models/__init__``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._base import Base, JSONType, generate_uuid

if TYPE_CHECKING:
    from .brokers import LinkedBrokerAccount
    from .control_plane import UserBudgetBucket, UserTradingPolicy
    from .strategies import UserStrategyConfig


class Organization(Base):
    """Tenants/organizations (B2B or multi-org support)."""

    __tablename__ = "orgs"

    org_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    # Relationships
    users: Mapped[list[User]] = relationship("User", back_populates="organization")


class User(Base):
    """End users (retail customers)."""

    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True, default=generate_uuid)
    org_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("orgs.org_id"), nullable=True)
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
        CheckConstraint("status IN ('active', 'suspended', 'closed')", name="ck_user_status"),
        CheckConstraint(
            "base_ccy = upper(trim(base_ccy)) AND length(base_ccy) BETWEEN 3 AND 10",
            name="ck_user_base_ccy",
        ),
    )

    # Relationships
    organization: Mapped[Organization] = relationship("Organization", back_populates="users")
    roles: Mapped[list[UserRole]] = relationship("UserRole", back_populates="user")
    subscriptions: Mapped[list[UserPlanSubscription]] = relationship(
        "UserPlanSubscription", back_populates="user"
    )
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


class UserRole(Base):
    """User roles for access control."""

    __tablename__ = "user_roles"

    user_id: Mapped[str] = mapped_column(String(50), ForeignKey("users.user_id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(50), primary_key=True)

    __table_args__ = (
        CheckConstraint("role IN ('admin', 'trader', 'viewer', 'support')", name="ck_user_role"),
    )

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="roles")


class Plan(Base):
    """Commercial plans/entitlements."""

    __tablename__ = "plans"

    plan_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    features: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)

    # Relationships
    subscriptions: Mapped[list[UserPlanSubscription]] = relationship(
        "UserPlanSubscription", back_populates="plan"
    )


class UserPlanSubscription(Base):
    """User plan subscriptions."""

    __tablename__ = "user_plan_subscriptions"

    sub_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(50), ForeignKey("users.user_id"), nullable=False)
    plan_id: Mapped[int] = mapped_column(Integer, ForeignKey("plans.plan_id"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'past_due', 'canceled')",
            name="ck_subscription_status",
        ),
    )

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="subscriptions")
    plan: Mapped[Plan] = relationship("Plan", back_populates="subscriptions")


__all__ = [
    "Organization",
    "Plan",
    "User",
    "UserPlanSubscription",
    "UserRole",
]
