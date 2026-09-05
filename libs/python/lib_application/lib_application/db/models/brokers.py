"""Section C — Broker Connectors & User-Linked Accounts.

Tables: ``brokers``, ``broker_environments``, ``linked_broker_accounts``,
``broker_credentials``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._base import Base, JSONType, SQLiteBigInteger

if TYPE_CHECKING:
    from .identity import User


class Broker(Base):
    """Supported brokers and their capabilities."""

    __tablename__ = "brokers"

    broker_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    capabilities: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)

    # Relationships
    environments: Mapped[list[BrokerEnvironment]] = relationship(
        "BrokerEnvironment", back_populates="broker"
    )
    linked_accounts: Mapped[list[LinkedBrokerAccount]] = relationship(
        "LinkedBrokerAccount", back_populates="broker"
    )


class BrokerEnvironment(Base):
    """Broker endpoints by environment/region."""

    __tablename__ = "broker_environments"

    broker_env_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broker_id: Mapped[int] = mapped_column(Integer, ForeignKey("brokers.broker_id"), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    region: Mapped[str] = mapped_column(String(50), nullable=False)
    base_urls: Mapped[Any] = mapped_column(JSONType, nullable=False)
    rate_limits: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint("environment IN ('paper', 'live')", name="ck_broker_environment"),
        CheckConstraint("length(trim(region)) > 0", name="ck_broker_environment_region"),
        UniqueConstraint(
            "broker_id",
            "environment",
            "region",
            name="uq_broker_environment_route",
        ),
    )

    # Relationships
    broker: Mapped[Broker] = relationship("Broker", back_populates="environments")


class LinkedBrokerAccount(Base):
    """User's external brokerage account link."""

    __tablename__ = "linked_broker_accounts"

    account_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(50), ForeignKey("users.user_id"), nullable=False)
    broker_id: Mapped[int] = mapped_column(Integer, ForeignKey("brokers.broker_id"), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    external_ref: Mapped[str | None] = mapped_column(String(255))
    base_ccy: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="connected", server_default="connected"
    )
    paper_initial_equity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    paper_initial_cash: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("environment IN ('paper', 'live')", name="ck_account_env"),
        CheckConstraint("status IN ('connected', 'revoked', 'error')", name="ck_account_status"),
        CheckConstraint(
            "base_ccy = upper(trim(base_ccy)) AND length(base_ccy) BETWEEN 3 AND 10",
            name="ck_account_base_ccy",
        ),
        CheckConstraint(
            "(environment = 'paper' AND paper_initial_equity IS NOT NULL "
            "AND paper_initial_cash IS NOT NULL) OR "
            "(environment = 'live' AND paper_initial_equity IS NULL "
            "AND paper_initial_cash IS NULL)",
            name="ck_account_paper_capital_by_environment",
        ),
        CheckConstraint(
            "paper_initial_equity IS NULL OR paper_initial_equity > 0",
            name="ck_account_paper_equity_positive",
        ),
        CheckConstraint(
            "paper_initial_cash IS NULL OR "
            "(paper_initial_cash >= 0 AND paper_initial_cash <= paper_initial_equity)",
            name="ck_account_paper_cash_range",
        ),
        UniqueConstraint("account_id", "user_id", name="uq_linked_broker_account_owner"),
    )

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="linked_accounts")
    broker: Mapped[Broker] = relationship("Broker", back_populates="linked_accounts")
    credentials: Mapped[list[BrokerCredential]] = relationship(
        "BrokerCredential", back_populates="account", cascade="all, delete-orphan"
    )


class BrokerCredential(Base):
    """Broker credential references (not raw keys!)."""

    __tablename__ = "broker_credentials"

    cred_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("linked_broker_accounts.account_id", ondelete="CASCADE"),
        nullable=False,
    )
    secret_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    last_rotated_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )

    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled', 'expired')", name="ck_credential_status"),
        CheckConstraint(
            "length(trim(secret_ref)) > 0",
            name="ck_broker_credentials_secret_ref",
        ),
        UniqueConstraint("secret_ref", name="uq_broker_credentials_secret_ref"),
        Index(
            "uq_broker_credentials_active_account",
            "account_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    # Relationships
    account: Mapped[LinkedBrokerAccount] = relationship(
        "LinkedBrokerAccount", back_populates="credentials"
    )


class ManagedSecret(Base):
    """Encrypted secret value stored in the database, addressed by ``secret_ref``.

    Backs the ``db`` secrets backend (``DbSecretsProvider`` in ``lib_infrastructure``):
    per-account broker credentials are stored here as ciphertext, encrypted at rest
    with a newest-first Fernet key ring (``SECRETS_MASTER_KEYS``) held only in the
    process environment. This lets a self-hosted / DigitalOcean deployment rotate
    its master key without downtime and onboard a tenant with a row insert (no
    redeploy). The plaintext — JSON ``{"api_key", "api_secret", ...}`` — is never
    persisted; ``secret_ref`` matches one account's ``broker_credentials`` row.
    """

    __tablename__ = "managed_secrets"

    secret_ref: Mapped[str] = mapped_column(String(500), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("linked_broker_accounts.account_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
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
        server_default=func.now(),
        onupdate=lambda: datetime.now(tz=UTC),
    )


__all__ = [
    "Broker",
    "BrokerCredential",
    "BrokerEnvironment",
    "LinkedBrokerAccount",
    "ManagedSecret",
]
