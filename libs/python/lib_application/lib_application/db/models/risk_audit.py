"""Section J — Risk, Mandates, Audit & Notifications.

Tables: ``risk_mandates``, ``risk_breaches``, ``api_audit_logs``,
``user_notifications``, ``outbound_webhooks``, ``feature_flags``,
``user_feature_flags``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, JSONType, SQLiteBigInteger


class RiskMandate(Base):
    """Global or user-owned risk policy envelopes."""

    __tablename__ = "risk_mandates"

    mandate_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(
        String(50),
        ForeignKey(
            "users.user_id",
            name="fk_risk_mandate_user",
            ondelete="RESTRICT",
        ),
    )
    rules: Mapped[Any] = mapped_column(JSONType, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        Index(
            "ix_risk_mandates_user_effective",
            "user_id",
            "effective_at",
        ),
    )


class RiskBreach(Base):
    """Tenant-owned risk rule breaches, optionally scoped to one account."""

    __tablename__ = "risk_breaches"

    breach_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey(
            "users.user_id",
            name="fk_risk_breach_user",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    broker_account_id: Mapped[int | None] = mapped_column(BigInteger)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )
    rule_code: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str | None] = mapped_column(String(20))
    context: Mapped[Any | None] = mapped_column(JSONType)

    __table_args__ = (
        ForeignKeyConstraint(
            ["broker_account_id", "user_id"],
            ["linked_broker_accounts.account_id", "linked_broker_accounts.user_id"],
            name="fk_risk_breach_account_owner",
            ondelete="RESTRICT",
        ),
        CheckConstraint("severity IN ('info', 'warn', 'block')", name="ck_severity"),
        Index(
            "ix_risk_breaches_user_account_occurred",
            "user_id",
            "broker_account_id",
            "occurred_at",
        ),
    )


class ApiAuditLog(Base):
    """Immutable audit trail."""

    __tablename__ = "api_audit_logs"

    audit_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(String(50), ForeignKey("users.user_id"))
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("linked_broker_accounts.account_id")
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    req: Mapped[Any] = mapped_column(JSONType, nullable=False)
    resp: Mapped[Any | None] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        index=True,
        server_default=func.now(),
    )

    __table_args__ = (CheckConstraint("status IN ('ok', 'error')", name="ck_audit_status"),)


class UserNotification(Base):
    """User notifications."""

    __tablename__ = "user_notifications"

    notif_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(50), ForeignKey("users.user_id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    event_code: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[Any] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        CheckConstraint("channel IN ('email', 'sms', 'push', 'webhook')", name="ck_channel"),
    )


class OutboundWebhook(Base):
    """Outbound webhook configurations."""

    __tablename__ = "outbound_webhooks"

    hook_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(String(50), ForeignKey("users.user_id"))
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    secret_ref: Mapped[str | None] = mapped_column(String(500))
    event_filter: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)


class FeatureFlag(Base):
    """Feature flags for experiments."""

    __tablename__ = "feature_flags"

    flag_code: Mapped[str] = mapped_column(String(100), primary_key=True)
    description: Mapped[str | None] = mapped_column(Text)


class UserFeatureFlag(Base):
    """User-specific feature flag assignments."""

    __tablename__ = "user_feature_flags"

    user_id: Mapped[str] = mapped_column(String(50), ForeignKey("users.user_id"), primary_key=True)
    flag_code: Mapped[str] = mapped_column(
        String(100), ForeignKey("feature_flags.flag_code"), primary_key=True
    )
    variant: Mapped[str] = mapped_column(String(50), nullable=False, default="on")


__all__ = [
    "ApiAuditLog",
    "FeatureFlag",
    "OutboundWebhook",
    "RiskBreach",
    "RiskMandate",
    "UserFeatureFlag",
    "UserNotification",
]
