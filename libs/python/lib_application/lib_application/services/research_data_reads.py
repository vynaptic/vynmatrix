"""Explicit-column, bounded recorded market-data read models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from lib_application.db.models.instruments import Instrument, InstrumentPrice
from lib_application.services.research_data_common import (
    ResearchDataError,
    decode_cursor,
    encode_cursor,
    page_limit,
)
from lib_data.research_dataset import decimal_string, iso_timestamp, timeframe_interval

MAX_SEARCH_LENGTH = 100
MAX_SOURCE_LENGTH = 50


def _price_page(
    session: Session,
    instrument_id: int,
    source: str,
    timeframe: str,
    cursor: tuple[datetime, str] | None,
    limit: int,
) -> tuple[list[dict[str, Any]], str | None]:
    if not source.strip() or len(source) > MAX_SOURCE_LENGTH:
        msg = "source must contain 1-50 characters"
        raise ResearchDataError(msg)
    try:
        timeframe_interval(timeframe)
    except (ValueError, OverflowError) as exc:
        msg = "Unsupported timeframe"
        raise ResearchDataError(msg) from exc
    prices = select(
        InstrumentPrice.price_id,
        InstrumentPrice.content_revision,
        InstrumentPrice.ts,
        InstrumentPrice.open,
        InstrumentPrice.high,
        InstrumentPrice.low,
        InstrumentPrice.close,
        InstrumentPrice.volume,
    ).where(
        InstrumentPrice.instr_id == instrument_id,
        InstrumentPrice.source == source,
        InstrumentPrice.timeframe == timeframe,
    )
    if cursor:
        stamp, identity = cursor
        try:
            price_id = int(identity)
        except ValueError as exc:
            msg = "Invalid price cursor"
            raise ResearchDataError(msg) from exc
        prices = prices.where(
            or_(
                InstrumentPrice.ts < stamp.replace(tzinfo=None),
                and_(
                    InstrumentPrice.ts == stamp.replace(tzinfo=None),
                    InstrumentPrice.price_id < price_id,
                ),
            )
        )
    rows = (
        session.execute(
            prices.order_by(InstrumentPrice.ts.desc(), InstrumentPrice.price_id.desc()).limit(
                limit + 1
            )
        )
        .mappings()
        .all()
    )
    bars = [
        {
            "price_id": row["price_id"],
            "content_revision": row["content_revision"],
            "ts": iso_timestamp(row["ts"]),
            "instrument_id": instrument_id,
            "source": source,
            "timeframe": timeframe,
            **{
                key: decimal_string(str(row[key])) if row[key] is not None else None
                for key in ("open", "high", "low", "close", "volume")
            },
        }
        for row in rows[:limit]
    ]
    next_before = (
        encode_cursor(rows[limit - 1]["ts"], str(rows[limit - 1]["price_id"]))
        if len(rows) > limit
        else None
    )
    return bars, next_before


def market_data(
    session: Session,
    owner_id: str,
    *,
    instrument_id: int | None = None,
    source: str | None = None,
    timeframe: str | None = None,
    before: str | None = None,
    limit: int = 50,
    search: str | None = None,
) -> dict[str, Any]:
    """Prices are deployment reference data; no credential or runtime state is read."""
    if not owner_id:
        msg = "An explicit owner scope is required"
        raise ResearchDataError(msg, status_code=403)
    limit = page_limit(limit)
    if search is not None and len(search) > MAX_SEARCH_LENGTH:
        msg = "search must contain at most 100 characters"
        raise ResearchDataError(msg)
    query = select(
        Instrument.instr_id,
        Instrument.canonical,
        Instrument.asset_class,
        Instrument.settlement_currency,
        Instrument.exchange,
        Instrument.is_tradable,
        Instrument.market_session_policy,
    )
    if instrument_id is not None:
        if type(instrument_id) is not int or instrument_id <= 0:
            msg = "instrument_id must be positive"
            raise ResearchDataError(msg)
        query = query.where(Instrument.instr_id == instrument_id)
    if search:
        query = query.where(
            Instrument.canonical.ilike(
                "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",
                escape="\\",
            )
        )
    cursor = decode_cursor(before)
    if cursor is not None and instrument_id is None:
        try:
            query = query.where(Instrument.instr_id > int(cursor[1]))
        except ValueError as exc:
            msg = "Invalid instrument cursor"
            raise ResearchDataError(msg) from exc
    instruments = (
        session.execute(query.order_by(Instrument.instr_id).limit(limit + 1)).mappings().all()
    )
    feeds = []
    bars: list[dict[str, Any]] = []
    next_before = None
    if instrument_id is not None:
        if not instruments:
            msg = "Instrument not found"
            raise ResearchDataError(msg, status_code=404)
        feed_rows = (
            session.execute(
                select(
                    InstrumentPrice.source,
                    InstrumentPrice.timeframe,
                    func.count().label("bar_count"),
                    func.min(InstrumentPrice.ts).label("first_ts"),
                    func.max(InstrumentPrice.ts).label("last_ts"),
                )
                .where(InstrumentPrice.instr_id == instrument_id)
                .group_by(InstrumentPrice.source, InstrumentPrice.timeframe)
                .order_by(InstrumentPrice.source, InstrumentPrice.timeframe)
                .limit(1000)
            )
            .mappings()
            .all()
        )
        feeds = [
            {
                **dict(row),
                "first_ts": iso_timestamp(row["first_ts"]),
                "last_ts": iso_timestamp(row["last_ts"]),
            }
            for row in feed_rows
        ]
        if (source is None) != (timeframe is None):
            msg = "source and timeframe must be selected together"
            raise ResearchDataError(msg)
        if source is not None and timeframe is not None:
            bars, next_before = _price_page(
                session, instrument_id, source, timeframe, cursor, limit
            )
            for bar in bars:
                bar.update(
                    symbol=instruments[0]["canonical"],
                    currency=instruments[0]["settlement_currency"],
                )
    elif len(instruments) > limit:
        next_before = encode_cursor(
            datetime(1970, 1, 1, tzinfo=UTC), str(instruments[limit - 1]["instr_id"])
        )
    return {
        "instruments": [
            {
                "instrument_id": row["instr_id"],
                "symbol": row["canonical"],
                "currency": row["settlement_currency"],
                **{key: value for key, value in row.items() if key != "instr_id"},
            }
            for row in instruments[:limit]
        ],
        "feeds": feeds,
        "bars": bars,
        "next_before": next_before,
        "provenance_strength": "stored_price_rows",
        "certified": False,
    }
