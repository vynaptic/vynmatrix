"""Section O — immutable point-in-time equity source observations.

These global, append-only records preserve acquired-artifact lineage and the
exact event/availability semantics needed for cutoff-safe research replay.
Corrections create new content identities and supersession links; they never
rewrite information that was visible at an earlier decision cutoff.
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
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, JSONType

_DIGEST_LENGTH = 64


class EquitySourceLineage(Base):
    """Content-addressed identity and semantics for one acquired artifact."""

    __tablename__ = "equity_source_lineages"

    lineage_id: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), primary_key=True)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    product: Mapped[str] = mapped_column(String(150), nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(100), nullable=False)
    source_identity: Mapped[str] = mapped_column(Text, nullable=False)
    source_revision: Mapped[str] = mapped_column(String(100), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timestamp_semantics: Mapped[Any] = mapped_column(JSONType, nullable=False)
    adjustment_policy: Mapped[str] = mapped_column(String(100), nullable=False)
    entitlement_scope: Mapped[str] = mapped_column(String(200), nullable=False)
    entitlement_owner_user_id: Mapped[str | None] = mapped_column(
        String(50),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
    )
    missing_data_policy: Mapped[str] = mapped_column(String(100), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "length(lineage_id) = 64 AND length(content_sha256) = 64",
            name="ck_equity_source_digests",
        ),
        CheckConstraint(
            "length(trim(provider)) > 0 AND length(trim(product)) > 0 "
            "AND length(trim(endpoint)) > 0 AND length(trim(dataset_version)) > 0 "
            "AND length(trim(tool_version)) > 0 AND length(trim(source_identity)) > 0 "
            "AND length(trim(source_revision)) > 0",
            name="ck_equity_source_identity",
        ),
        CheckConstraint(
            "length(trim(adjustment_policy)) > 0 "
            "AND length(trim(entitlement_scope)) > 0 "
            "AND length(trim(missing_data_policy)) > 0",
            name="ck_equity_source_policies",
        ),
        CheckConstraint(
            "entitlement_owner_user_id IS NULL OR length(trim(entitlement_owner_user_id)) > 0",
            name="ck_equity_source_entitlement_owner",
        ),
        Index("ix_equity_source_provider_product", "provider", "product"),
        Index("ix_equity_source_entitlement_owner", "entitlement_owner_user_id"),
        Index("ix_equity_source_retrieved_at", "retrieved_at"),
    )


class EquityObservation(Base):
    """One immutable source record with explicit event and availability time."""

    __tablename__ = "equity_observations"

    observation_id: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), primary_key=True)
    lineage_id: Mapped[str] = mapped_column(
        String(_DIGEST_LENGTH),
        ForeignKey("equity_source_lineages.lineage_id", ondelete="RESTRICT"),
        nullable=False,
    )
    instr_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("instruments.instr_id", ondelete="RESTRICT"),
    )
    observation_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    source_record_identity: Mapped[str] = mapped_column(Text, nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_observation_id: Mapped[str | None] = mapped_column(
        String(_DIGEST_LENGTH),
        ForeignKey("equity_observations.observation_id", ondelete="RESTRICT"),
    )
    accession_number: Mapped[str | None] = mapped_column(String(32))
    filing_form: Mapped[str | None] = mapped_column(String(20))
    sic_code: Mapped[str | None] = mapped_column(String(10))
    disposition: Mapped[str] = mapped_column(String(20), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "observation_kind IN ('price', 'corporate_action', 'membership', "
            "'filing', 'xbrl_fact', 'benchmark', 'calendar', "
            "'earnings_event', 'market_cap', 'security_identity', "
            "'analyst_estimate', 'news_event', 'call_transcript', "
            "'insider_transaction', 'positioning', 'macro_release', "
            "'factor_risk_exposure')",
            name="ck_equity_observation_kind",
        ),
        CheckConstraint(
            "disposition IN ('observed', 'missing', 'excluded', 'unavailable', "
            "'partial', 'failed')",
            name="ck_equity_observation_disposition",
        ),
        CheckConstraint(
            "disposition <> 'observed' OR available_at IS NOT NULL",
            name="ck_equity_observed_availability",
        ),
        CheckConstraint("revision > 0", name="ck_equity_observation_revision"),
        CheckConstraint(
            "length(observation_id) = 64 AND length(content_sha256) = 64",
            name="ck_equity_observation_digests",
        ),
        CheckConstraint(
            "length(trim(source_record_identity)) > 0",
            name="ck_equity_observation_source_identity",
        ),
        CheckConstraint(
            "supersedes_observation_id IS NULL OR supersedes_observation_id <> observation_id",
            name="ck_equity_observation_supersedes_other",
        ),
        CheckConstraint(
            "observation_kind NOT IN ('filing', 'xbrl_fact') "
            "OR (accession_number IS NOT NULL AND length(trim(accession_number)) > 0 "
            "AND filing_form IS NOT NULL AND length(trim(filing_form)) > 0)",
            name="ck_equity_sec_observation_filing_identity",
        ),
        UniqueConstraint(
            "lineage_id",
            "source_record_identity",
            "revision",
            name="uq_equity_observation_source_revision",
        ),
        Index(
            "ix_equity_observation_instr_available",
            "instr_id",
            "observation_kind",
            "available_at",
        ),
        Index("ix_equity_observation_kind_event", "observation_kind", "event_at"),
        Index("ix_equity_observation_lineage", "lineage_id"),
    )


class EquityObservationValue(Base):
    """One normalized, exactly typed field from an equity observation."""

    __tablename__ = "equity_observation_values"

    value_id: Mapped[str] = mapped_column(String(_DIGEST_LENGTH), primary_key=True)
    observation_id: Mapped[str] = mapped_column(
        String(_DIGEST_LENGTH),
        ForeignKey("equity_observations.observation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    field_name: Mapped[str] = mapped_column(String(100), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    value_type: Mapped[str] = mapped_column(String(16), nullable=False)
    decimal_value: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    integer_value: Mapped[int | None] = mapped_column(BigInteger)
    text_value: Mapped[str | None] = mapped_column(Text)
    boolean_value: Mapped[bool | None] = mapped_column(Boolean)
    date_value: Mapped[date | None] = mapped_column(Date)
    timestamp_value: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unit: Mapped[str | None] = mapped_column(String(50))
    context_identity: Mapped[str | None] = mapped_column(String(200))
    fiscal_year: Mapped[int | None] = mapped_column(Integer)
    fiscal_period: Mapped[str | None] = mapped_column(String(20))
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)

    __table_args__ = (
        CheckConstraint("length(value_id) = 64", name="ck_equity_observation_value_id"),
        CheckConstraint(
            "length(trim(field_name)) > 0 AND ordinal >= 0",
            name="ck_equity_observation_value_key",
        ),
        CheckConstraint(
            "(value_type = 'decimal' AND decimal_value IS NOT NULL "
            "AND integer_value IS NULL AND text_value IS NULL "
            "AND boolean_value IS NULL AND date_value IS NULL "
            "AND timestamp_value IS NULL) OR "
            "(value_type = 'integer' AND decimal_value IS NULL "
            "AND integer_value IS NOT NULL AND text_value IS NULL "
            "AND boolean_value IS NULL AND date_value IS NULL "
            "AND timestamp_value IS NULL) OR "
            "(value_type = 'text' AND decimal_value IS NULL "
            "AND integer_value IS NULL AND text_value IS NOT NULL "
            "AND boolean_value IS NULL AND date_value IS NULL "
            "AND timestamp_value IS NULL) OR "
            "(value_type = 'boolean' AND decimal_value IS NULL "
            "AND integer_value IS NULL AND text_value IS NULL "
            "AND boolean_value IS NOT NULL AND date_value IS NULL "
            "AND timestamp_value IS NULL) OR "
            "(value_type = 'date' AND decimal_value IS NULL "
            "AND integer_value IS NULL AND text_value IS NULL "
            "AND boolean_value IS NULL AND date_value IS NOT NULL "
            "AND timestamp_value IS NULL) OR "
            "(value_type = 'timestamp' AND decimal_value IS NULL "
            "AND integer_value IS NULL AND text_value IS NULL "
            "AND boolean_value IS NULL AND date_value IS NULL "
            "AND timestamp_value IS NOT NULL)",
            name="ck_equity_observation_typed_value",
        ),
        CheckConstraint(
            "unit IS NULL OR length(trim(unit)) > 0",
            name="ck_equity_observation_unit",
        ),
        CheckConstraint(
            "fiscal_year IS NULL OR fiscal_year BETWEEN 1800 AND 3000",
            name="ck_equity_observation_fiscal_year",
        ),
        CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_end >= period_start",
            name="ck_equity_observation_value_period",
        ),
        UniqueConstraint(
            "observation_id",
            "field_name",
            "ordinal",
            name="uq_equity_observation_value",
        ),
        Index("ix_equity_observation_value_observation", "observation_id"),
    )


def _reject_equity_observation_update(
    _mapper: Any,
    _connection: Any,
    target: Any,
) -> None:
    msg = f"{target.__class__.__name__} rows are immutable"
    raise ValueError(msg)


def _reject_equity_observation_delete(
    _mapper: Any,
    _connection: Any,
    target: Any,
) -> None:
    msg = f"{target.__class__.__name__} rows are append-only and cannot be deleted"
    raise ValueError(msg)


for _immutable_model in (
    EquitySourceLineage,
    EquityObservation,
    EquityObservationValue,
):
    event.listen(_immutable_model, "before_update", _reject_equity_observation_update)
    event.listen(_immutable_model, "before_delete", _reject_equity_observation_delete)


__all__ = [
    "EquityObservation",
    "EquityObservationValue",
    "EquitySourceLineage",
]
