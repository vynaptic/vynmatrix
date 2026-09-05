"""Section N — Equity Market Reference Data.

Tables: ``corporate_actions``, ``index_membership``, ``equity_security_identities``,
``earnings_events``.

Global (non-tenant) reference data for the US-equity data plane
(strategies/indicator/USQualityCompounder/README.md). No RLS: the 0039/0040 policies scope
user- and account-owned rows; these tables carry no ``user_id``/``account_id``.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, SQLiteBigInteger


class CorporateAction(Base):
    """Split and dividend events keyed to the ex-date.

    Consumed by the §7.2 adjustment pipeline: signals run on in-house
    adjusted series computed from these rows (vendor ``adjusted_close`` is
    deliberately not stored), so a 2020→present backtest prices AAPL's
    2020-08-31 4:1 split correctly and a vendor swap stays a connector change.
    """

    __tablename__ = "corporate_actions"

    action_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    instr_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("instruments.instr_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action_type: Mapped[str] = mapped_column(String(20), nullable=False)
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    ratio: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    currency: Mapped[str | None] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(50), nullable=False)

    __table_args__ = (
        CheckConstraint("action_type IN ('split', 'dividend')", name="ck_corporate_action_type"),
        CheckConstraint(
            "(action_type = 'split' AND ratio IS NOT NULL) "
            "OR (action_type = 'dividend' AND amount IS NOT NULL)",
            name="ck_corporate_action_payload",
        ),
        UniqueConstraint(
            "instr_id", "action_type", "ex_date", "source", name="uq_corporate_actions_event"
        ),
        Index("ix_corporate_actions_instr_ex_date", "instr_id", "ex_date"),
    )


class IndexMembership(Base):
    """Point-in-time inclusive intervals (``effective_to`` NULL = current member).

    Consumed by §3 universe construction: the eligible pool is the S&P 500
    *as of the ranking date*, hand-audited from S&P DJI press releases
    (``source_ref``) — the survivorship control identified as the single
    largest missing subsystem in Phase 0.
    """

    __tablename__ = "index_membership"

    membership_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    index_code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    instr_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("instruments.instr_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    source_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    observation_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "equity_observations.observation_id",
            name="fk_index_membership_observation",
            ondelete="RESTRICT",
        ),
    )

    __table_args__ = (
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_index_membership_span",
        ),
        UniqueConstraint("index_code", "instr_id", "effective_from", name="uq_index_membership"),
        Index("ix_index_membership_code_from", "index_code", "effective_from"),
        Index("ix_index_membership_observation_id", "observation_id"),
    )


class EquitySecurityIdentity(Base):
    """Append-only effective share-class identity mapped to one instrument.

    ``security_id`` is permanent at the share-class level; multiple securities
    may intentionally share one ``issuer_id``. Both interval endpoints are
    inclusive. Synchronized panels require an active lineage-backed row and
    never infer either identity from ticker text.
    """

    __tablename__ = "equity_security_identities"

    identity_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    security_id: Mapped[str] = mapped_column(String(100), nullable=False)
    issuer_id: Mapped[str] = mapped_column(String(100), nullable=False)
    canonical_symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    instr_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("instruments.instr_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    source_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    observation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("equity_observations.observation_id", ondelete="RESTRICT"),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_equity_security_identity_span",
        ),
        CheckConstraint(
            "length(trim(security_id)) > 0 AND length(trim(issuer_id)) > 0 "
            "AND canonical_symbol = upper(trim(canonical_symbol)) "
            "AND length(trim(canonical_symbol)) > 0 "
            "AND length(trim(source_ref)) > 0 AND length(observation_id) = 64",
            name="ck_equity_security_identity_values",
        ),
        UniqueConstraint(
            "security_id",
            "effective_from",
            name="uq_equity_security_identity_security_from",
        ),
        UniqueConstraint(
            "instr_id",
            "effective_from",
            name="uq_equity_security_identity_instrument_from",
        ),
        Index(
            "ix_equity_security_identity_effective",
            "instr_id",
            "effective_from",
            "effective_to",
        ),
        Index(
            "ix_equity_security_identity_symbol_effective",
            "canonical_symbol",
            "effective_from",
            "effective_to",
        ),
        Index("ix_equity_security_identity_observation", "observation_id"),
    )


class EarningsEvent(Base):
    """Scheduled/actual earnings report dates with announce-time slot.

    Consumed by the §4.5 earnings blackout (the only news gate in v1):
    ``status`` widens the presumed-date window, ``announce_time``
    (bmo/amc/dmh) normalizes AMC announcements to the next session's
    reaction bar, and ``first_seen_at`` records when the date became known.
    """

    __tablename__ = "earnings_events"

    event_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    instr_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("instruments.instr_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    announce_time: Mapped[str] = mapped_column(
        String(10), nullable=False, default="unknown", server_default="unknown"
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(50), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "announce_time IN ('bmo', 'amc', 'dmh', 'unknown')",
            name="ck_earnings_announce_time",
        ),
        CheckConstraint(
            "status IN ('presumed', 'confirmed', 'actual')",
            name="ck_earnings_status",
        ),
        UniqueConstraint("instr_id", "report_date", "source", name="uq_earnings_event"),
        Index("ix_earnings_events_instr_report", "instr_id", "report_date"),
    )


def _reject_security_identity_mutation(
    _mapper: Any,
    _connection: Any,
    _target: EquitySecurityIdentity,
) -> None:
    """Mirror the production append-only trigger in SQLite-backed tests."""

    message = "EquitySecurityIdentity rows are append-only and immutable"
    raise ValueError(message)


event.listen(
    EquitySecurityIdentity,
    "before_update",
    _reject_security_identity_mutation,
)
event.listen(
    EquitySecurityIdentity,
    "before_delete",
    _reject_security_identity_mutation,
)


__all__ = [
    "CorporateAction",
    "EarningsEvent",
    "EquitySecurityIdentity",
    "IndexMembership",
]
