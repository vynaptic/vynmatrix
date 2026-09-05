"""Database-backed historical prices for validation campaigns."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from dev_cli.validation.historical_dataset import DatasetAudit, validate_bars
from lib_application.db.models import Instrument, InstrumentPrice
from lib_data.bars import Bar

SessionFactory = Callable[[], Any]


class HistoricalPriceRepository:
    """Load and quality-gate immutable DB intervals for deterministic replay."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def load_validated_bars(
        self,
        instr_id: int,
        *,
        start: datetime,
        end: datetime,
        timeframe: str,
        source: str,
        symbol: str | None = None,
        minimum_coverage: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[list[Bar], DatasetAudit]:
        """Load a half-open interval without repairing missing or invalid OHLCV."""

        if start.tzinfo is None or start.utcoffset() is None:
            msg = "Price interval start must be timezone-aware"
            raise ValueError(msg)
        if end.tzinfo is None or end.utcoffset() is None:
            msg = "Price interval end must be timezone-aware"
            raise ValueError(msg)
        start_db = start.astimezone(UTC).replace(tzinfo=None)
        end_db = end.astimezone(UTC).replace(tzinfo=None)
        with self._session_factory() as session:
            instrument = session.get(Instrument, instr_id)
            if instrument is None:
                msg = f"Unknown instrument id: {instr_id}"
                raise ValueError(msg)
            rows = (
                session.execute(
                    select(InstrumentPrice)
                    .where(
                        InstrumentPrice.instr_id == instr_id,
                        InstrumentPrice.ts >= start_db,
                        InstrumentPrice.ts < end_db,
                        InstrumentPrice.timeframe == timeframe,
                        InstrumentPrice.source == source,
                    )
                    .order_by(InstrumentPrice.ts)
                )
                .scalars()
                .all()
            )
            resolved_symbol = symbol or str(instrument.canonical)

        base_metadata = {
            **(metadata or {}),
            "source": source,
            "price_source": source,
            "timeframe": timeframe,
            "price_timeframe": timeframe,
            "timestamp_semantics": "period_start",
        }
        bars: list[Bar] = []
        for row in rows:
            missing_fields = [
                field
                for field in ("open", "high", "low", "close", "volume")
                if getattr(row, field) is None
            ]
            if missing_fields:
                msg = (
                    f"Stored candle instr_id={instr_id} ts={row.ts.isoformat()} "
                    f"is missing required OHLCV fields: {', '.join(missing_fields)}"
                )
                raise ValueError(msg)
            bars.append(
                Bar(
                    symbol=resolved_symbol,
                    timestamp=(
                        row.ts.replace(tzinfo=UTC)
                        if row.ts.tzinfo is None
                        else row.ts.astimezone(UTC)
                    ),
                    open=float(row.open),
                    high=float(row.high),
                    low=float(row.low),
                    close=float(row.close),
                    volume=float(row.volume),
                    timeframe=row.timeframe,
                    source=row.source,
                    metadata=dict(base_metadata),
                )
            )
        audit = validate_bars(
            bars,
            symbol=resolved_symbol,
            source=source,
            timeframe=timeframe,
            start=start,
            end=end,
            minimum_coverage=minimum_coverage,
        )
        return bars, audit
