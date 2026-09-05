"""Section B — Legal, Consents & Suitability.

Tables: ``user_consents``, ``suitability_questionnaires``,
``user_suitability_responses``.

Extracted from the historical ``models.py`` as part of the domain-by-domain
split. Classes register against the shared ``Base`` exposed by :mod:`._base`;
cross-section ``relationship(...)`` strings continue to resolve through
``Base.registry`` once every domain submodule is imported by
``models/__init__``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._base import Base, JSONType, SQLiteBigInteger


class UserConsent(Base):
    """User consents and disclosures."""

    __tablename__ = "user_consents"

    consent_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(50), ForeignKey("users.user_id"), nullable=False)
    consent_code: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(tz=UTC)
    )
    meta: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)


class SuitabilityQuestionnaire(Base):
    """Suitability questionnaire definitions."""

    __tablename__ = "suitability_questionnaires"

    questionnaire_id: Mapped[int] = mapped_column(
        SQLiteBigInteger, primary_key=True, autoincrement=True
    )
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    spec_json: Mapped[Any] = mapped_column(JSONType, nullable=False)

    # Relationships
    responses: Mapped[list[UserSuitabilityResponse]] = relationship(
        "UserSuitabilityResponse", back_populates="questionnaire"
    )


class UserSuitabilityResponse(Base):
    """User suitability assessment responses."""

    __tablename__ = "user_suitability_responses"

    resp_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(50), ForeignKey("users.user_id"), nullable=False)
    questionnaire_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("suitability_questionnaires.questionnaire_id"),
        nullable=False,
    )
    answers_json: Mapped[Any] = mapped_column(JSONType, nullable=False)
    derived_profile: Mapped[str] = mapped_column(String(50), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(tz=UTC)
    )
    valid_to: Mapped[datetime | None] = mapped_column(DateTime)

    # Relationships
    questionnaire: Mapped[SuitabilityQuestionnaire] = relationship(
        "SuitabilityQuestionnaire", back_populates="responses"
    )


__all__ = [
    "SuitabilityQuestionnaire",
    "UserConsent",
    "UserSuitabilityResponse",
]
