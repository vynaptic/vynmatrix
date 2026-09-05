"""Section H — Options Strategy Templates & User Presets.

Tables: ``option_strategy_templates``, ``option_strategy_template_legs``,
``user_option_presets``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._base import Base, JSONType, SQLiteBigInteger


class OptionStrategyTemplate(Base):
    """Predefined option strategy templates."""

    __tablename__ = "option_strategy_templates"

    template_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # Relationships
    legs: Mapped[list[OptionStrategyTemplateLeg]] = relationship(
        "OptionStrategyTemplateLeg",
        back_populates="template",
        cascade="all, delete-orphan",
    )


class OptionStrategyTemplateLeg(Base):
    """Legs for option strategy templates."""

    __tablename__ = "option_strategy_template_legs"

    leg_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("option_strategy_templates.template_id", ondelete="CASCADE"),
        nullable=False,
    )
    leg_index: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[str] = mapped_column(String(10), nullable=False)
    option_right: Mapped[str] = mapped_column(
        String(10), nullable=False
    )  # Renamed from 'right' (reserved word)
    ratio: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=1)
    strike_spec: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    expiry_spec: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint("position IN ('LONG', 'SHORT')", name="ck_leg_position"),
        CheckConstraint("option_right IN ('CALL', 'PUT')", name="ck_leg_right"),
    )

    # Relationships
    template: Mapped[OptionStrategyTemplate] = relationship(
        "OptionStrategyTemplate", back_populates="legs"
    )


class UserOptionPreset(Base):
    """User-specific option strategy presets."""

    __tablename__ = "user_option_presets"

    preset_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(50), ForeignKey("users.user_id"), nullable=False)
    template_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("option_strategy_templates.template_id"), nullable=False
    )
    underlier: Mapped[str] = mapped_column(String(50), nullable=False)
    params: Mapped[Any] = mapped_column(JSONType, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )


__all__ = [
    "OptionStrategyTemplate",
    "OptionStrategyTemplateLeg",
    "UserOptionPreset",
]
