"""Section D — Instruments, Symbol Mapping & Trading Sessions.

Tables: ``instruments``, ``instrument_broker_symbols``, ``instrument_aliases``,
``market_calendars``, ``market_sessions``, ``prices``, ``watermarks``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lib_common.asset_classes import ASSET_CLASS_CHECK_SQL

from ._base import Base, SQLiteBigInteger


def _default_market_session_policy(context: Any) -> str:
    """Persist crypto as continuous while every other asset fails closed."""
    asset_class = str(context.get_current_parameters().get("asset_class") or "").lower()
    return "continuous" if asset_class == "crypto" else "scheduled"


def _default_is_tradable(context: Any) -> bool:
    """Keep cash indices reference-only; derivatives are separate instruments."""
    asset_class = str(context.get_current_parameters().get("asset_class") or "").lower()
    return asset_class != "index"


class MarketCalendar(Base):
    """Authoritative regular-session schedule and its current coverage.

    A trusted ingestion job refreshes one calendar atomically from an exchange
    or broker schedule endpoint. ``coverage_start``/``coverage_end`` establish
    that the source described the complete interval: a time inside coverage
    but outside every child session is therefore authoritatively closed.
    """

    __tablename__ = "market_calendars"

    calendar_id: Mapped[int] = mapped_column(
        SQLiteBigInteger,
        primary_key=True,
        autoincrement=True,
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    source_reference: Mapped[str] = mapped_column(Text, nullable=False)
    observation_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "equity_observations.observation_id",
            name="fk_market_calendar_observation",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )
    coverage_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    coverage_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
        onupdate=lambda: datetime.now(tz=UTC),
    )

    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('broker', 'exchange')",
            name="ck_market_calendar_source_kind",
        ),
        CheckConstraint(
            "length(trim(code)) > 0 AND length(trim(provider)) > 0 "
            "AND length(trim(source_reference)) > 0",
            name="ck_market_calendar_provenance",
        ),
        CheckConstraint(
            "(coverage_start IS NULL AND coverage_end IS NULL AND observed_at IS NULL) OR "
            "(coverage_start IS NOT NULL AND coverage_end IS NOT NULL "
            "AND observed_at IS NOT NULL AND coverage_end > coverage_start)",
            name="ck_market_calendar_coverage",
        ),
        UniqueConstraint("code", name="uq_market_calendars_code"),
        Index("ix_market_calendars_observation_id", "observation_id"),
    )

    instruments: Mapped[list[Instrument]] = relationship(
        "Instrument",
        back_populates="market_calendar",
    )
    sessions: Mapped[list[MarketSession]] = relationship(
        "MarketSession",
        back_populates="calendar",
        cascade="all, delete-orphan",
    )


class MarketSession(Base):
    """One open regular-trading interval from an authoritative calendar."""

    __tablename__ = "market_sessions"

    session_id: Mapped[int] = mapped_column(
        SQLiteBigInteger,
        primary_key=True,
        autoincrement=True,
    )
    calendar_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("market_calendars.calendar_id", ondelete="CASCADE"),
        nullable=False,
    )
    opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("closes_at > opens_at", name="ck_market_session_interval"),
        UniqueConstraint(
            "calendar_id",
            "opens_at",
            "closes_at",
            name="uq_market_session_window",
        ),
        Index(
            "ix_market_sessions_calendar_window",
            "calendar_id",
            "opens_at",
            "closes_at",
        ),
    )

    calendar: Mapped[MarketCalendar] = relationship(
        "MarketCalendar",
        back_populates="sessions",
    )


class Instrument(Base):
    """Canonical instrument definitions."""

    __tablename__ = "instruments"

    instr_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_class: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    canonical: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    exchange: Mapped[str | None] = mapped_column(String(50))
    settlement_currency: Mapped[str] = mapped_column(String(10), nullable=False)
    tick_size: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    lot_size: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    is_tradable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=_default_is_tradable,
        server_default="true",
    )
    market_session_policy: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=_default_market_session_policy,
        server_default="scheduled",
    )
    market_calendar_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("market_calendars.calendar_id", ondelete="RESTRICT"),
    )

    __table_args__ = (
        CheckConstraint(
            ASSET_CLASS_CHECK_SQL,
            name="ck_asset_class",
        ),
        CheckConstraint(
            "asset_class <> 'index' OR is_tradable = false",
            name="ck_index_reference_only",
        ),
        CheckConstraint(
            "(asset_class = 'crypto' AND market_session_policy = 'continuous' "
            "AND market_calendar_id IS NULL) OR "
            "(asset_class <> 'crypto' AND market_session_policy = 'scheduled')",
            name="ck_instrument_market_session_policy",
        ),
        UniqueConstraint("asset_class", "canonical", name="uq_instrument"),
        Index("ix_instrument_asset_canonical", "asset_class", "canonical"),
        Index("ix_instruments_market_calendar_id", "market_calendar_id"),
        Index(
            "uq_instruments_norm_canonical",
            func.upper(func.translate(canonical, "/-_", "")),
            unique=True,
        ).ddl_if(dialect="postgresql"),
    )

    # Relationships
    broker_symbols: Mapped[list[InstrumentBrokerSymbol]] = relationship(
        "InstrumentBrokerSymbol",
        back_populates="instrument",
        cascade="all, delete-orphan",
    )
    aliases: Mapped[list[InstrumentAlias]] = relationship(
        "InstrumentAlias", back_populates="instrument", cascade="all, delete-orphan"
    )
    market_calendar: Mapped[MarketCalendar | None] = relationship(
        "MarketCalendar",
        back_populates="instruments",
    )


class InstrumentBrokerSymbol(Base):
    """Mapping from canonical instruments to exact broker catalogue identity.

    ``broker_symbol`` remains the venue-facing display/product symbol.
    ``broker_instrument_id`` is an opaque exact identifier such as an IBKR
    conid, Zerodha instrument token, or Saxo UIC. Some venues reuse numeric
    identifiers across product types, so ``broker_instrument_type`` remains a
    separate, independently nullable discriminator.
    """

    __tablename__ = "instrument_broker_symbols"

    instr_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("instruments.instr_id", ondelete="CASCADE"),
        primary_key=True,
    )
    broker_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("brokers.broker_id"), primary_key=True
    )
    broker_symbol: Mapped[str] = mapped_column(String(100), nullable=False)
    broker_instrument_id: Mapped[str | None] = mapped_column(String(255))
    broker_instrument_type: Mapped[str | None] = mapped_column(String(100))

    __table_args__ = (
        CheckConstraint(
            "broker_instrument_id IS NULL OR length(trim(broker_instrument_id)) > 0",
            name="ck_instrument_broker_id_nonblank",
        ),
        CheckConstraint(
            "broker_instrument_type IS NULL OR length(trim(broker_instrument_type)) > 0",
            name="ck_instrument_broker_type_nonblank",
        ),
        UniqueConstraint(
            "broker_id",
            "broker_instrument_id",
            "broker_instrument_type",
            name="uq_instrument_broker_venue_identity",
        ),
        Index(
            "uq_instrument_broker_untyped_venue_id",
            "broker_id",
            "broker_instrument_id",
            unique=True,
            postgresql_where=(broker_instrument_id.is_not(None) & broker_instrument_type.is_(None)),
            sqlite_where=(broker_instrument_id.is_not(None) & broker_instrument_type.is_(None)),
        ),
    )

    # Relationships
    instrument: Mapped[Instrument] = relationship("Instrument", back_populates="broker_symbols")


class InstrumentAlias(Base):
    """Aliases for instruments (e.g., BTCUSD -> BTC/USD)."""

    __tablename__ = "instrument_aliases"

    alias_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    instr_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("instruments.instr_id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    source: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # lean, exchange-specific, etc.

    instrument: Mapped[Instrument] = relationship("Instrument", back_populates="aliases")


class InstrumentPrice(Base):
    """Time-series price data for instruments."""

    __tablename__ = "prices"

    price_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    instr_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("instruments.instr_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(
        String(20), nullable=False, default="1d", server_default="1d"
    )
    open: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    high: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    low: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    close: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    volume: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    source: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="coinbase_live",
        server_default="coinbase_live",
    )
    # Incremented only when the persisted OHLCV content changes. Signal workers
    # pin this revision while processing a bar so a concurrent venue correction
    # cannot be acknowledged as if the stale observation were authoritative.
    content_revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default="1",
    )

    __table_args__ = (
        UniqueConstraint("instr_id", "ts", "timeframe", "source", name="uq_prices_instr_ts"),
        Index("ix_prices_instr_ts", "instr_id", "ts"),
    )

    instrument: Mapped[Instrument] = relationship("Instrument")


class ConsumerWatermark(Base):
    """Per-worker market-data consumption watermark."""

    __tablename__ = "watermarks"

    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(50), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(20), primary_key=True)
    instr_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("instruments.instr_id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    last_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # The earliest changed/inserted source bar at or before ``last_ts``.
    # Ingestion advances ``rebuild_generation`` in the same transaction as the
    # price change. A fresh worker clears both fields only after a full,
    # deterministic replay and only if the captured generation still matches.
    rebuild_from_ts: Mapped[datetime | None] = mapped_column(DateTime)
    rebuild_generation: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "rebuild_generation >= 0",
            name="ck_watermarks_rebuild_generation_nonnegative",
        ),
        Index(
            "ix_watermarks_feed_rebuild",
            "instr_id",
            "source",
            "timeframe",
            "rebuild_from_ts",
        ),
    )


__all__ = [
    "ConsumerWatermark",
    "Instrument",
    "InstrumentAlias",
    "InstrumentBrokerSymbol",
    "InstrumentPrice",
    "MarketCalendar",
    "MarketSession",
]
