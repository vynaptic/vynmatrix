"""Shared price ingestion service.

Provides instrument resolution and idempotent DB upsert logic used by the
canonical ``market_data_ingestor`` app. Venue HTTP adapters live behind the
market-data provider port in ``lib_infrastructure``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from lib_application.db.models import ConsumerWatermark, InstrumentPrice
from lib_application.services.instrument_resolution import (
    load_instrument_symbol_map,
    resolve_broker_instrument_identity,
    resolve_instrument,
)
from lib_common.logging import get_logger
from lib_data.bars import ohlcv_invariant_error
from lib_data.market_data import CandleRow, MarketDataInstrument

logger = get_logger(__name__)

SessionFactory = Callable[[], Any]
_CONSUMER_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def _is_valid_candle(row: CandleRow) -> bool:
    """OHLC sanity check (MD-5): reject malformed candles before they corrupt
    indicators. Requires positive O/H/L/C, ``high >= max(o,c,l)``,
    ``low <= min(o,c,h)``, and non-negative volume when present.
    """
    return (
        ohlcv_invariant_error(
            open_price=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
            allow_missing_volume=True,
        )
        is None
    )


class PriceIngestionService:
    """Instrument resolution + idempotent OHLCV upsert.

    This service is DB-only — it does **not** fetch data from exchanges.
    Callers are responsible for obtaining :class:`CandleRow` objects from an
    infrastructure market-data provider and passing them here.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def load_instrument_map(self, *, asset_class: str = "crypto") -> dict[str, int]:
        """Return ``{normalized_symbol: instr_id}`` for all instruments of the asset class.

        Canonical names, aliases, and broker symbols share one map. If two
        instruments normalize to the same key, that key is omitted and logged so
        catalogue corruption cannot route prices to an arbitrary row.
        """
        with self._session_factory() as session:
            return load_instrument_symbol_map(session, asset_class=asset_class)

    def resolve_instrument(self, symbol: str, *, asset_class: str = "crypto") -> int | None:
        """Resolve a single symbol to its ``instr_id``, or ``None`` if not found."""
        with self._session_factory() as session:
            instrument = resolve_instrument(session, symbol, asset_class=asset_class)
            return int(instrument.instr_id) if instrument is not None else None

    def load_market_data_instruments(
        self,
        *,
        broker_code: str,
        symbols: Sequence[str],
        identifier_field: Literal["broker_symbol", "broker_instrument_id"],
        require_numeric_identifier: bool = False,
        require_instrument_type: bool = False,
    ) -> list[MarketDataInstrument]:
        """Resolve configured canonical selectors to one broker catalogue.

        Canonical instruments and provider identifiers are deliberately loaded
        from the database together. This prevents opaque conids, Kite tokens,
        and Saxo UICs from becoming downstream symbols and prevents an
        environment-only mapping from drifting away from the execution
        catalogue.
        """
        normalized_broker_code = broker_code.strip().lower()
        selectors = [str(symbol).strip() for symbol in symbols if str(symbol).strip()]
        if not normalized_broker_code:
            msg = "Market-data broker_code must be non-empty"
            raise ValueError(msg)
        if not selectors:
            msg = "Market-data ingestion requires at least one instrument selector"
            raise ValueError(msg)
        if identifier_field not in {"broker_symbol", "broker_instrument_id"}:
            msg = f"Unsupported market-data identifier field: {identifier_field!r}"
            raise ValueError(msg)
        if require_numeric_identifier and identifier_field != "broker_instrument_id":
            msg = "Numeric market-data identifiers must use broker_instrument_id"
            raise ValueError(msg)

        instruments: list[MarketDataInstrument] = []
        resolved_ids: set[int] = set()
        venue_identities: set[tuple[str, str | None]] = set()
        with self._session_factory() as session:
            for selector in selectors:
                instrument = resolve_instrument(session, selector)
                if instrument is None:
                    msg = f"Unknown canonical market-data instrument: {selector!r}"
                    raise ValueError(msg)
                instrument_id = int(instrument.instr_id)
                if instrument_id in resolved_ids:
                    msg = (
                        "Market-data instrument selectors must resolve uniquely; "
                        f"{selector!r} repeats instr_id={instrument_id}"
                    )
                    raise ValueError(msg)
                identity = resolve_broker_instrument_identity(
                    session,
                    instrument_id=instrument_id,
                    broker_code=normalized_broker_code,
                )
                if identity is None:
                    msg = (
                        f"Instrument {instrument.canonical!r} has no "
                        f"{normalized_broker_code!r} catalogue mapping"
                    )
                    raise ValueError(msg)

                if identifier_field == "broker_instrument_id":
                    product_id = identity.broker_instrument_id
                    if product_id is None:
                        msg = (
                            f"Instrument {instrument.canonical!r} requires a typed "
                            f"{normalized_broker_code!r} broker_instrument_id"
                        )
                        raise ValueError(msg)
                else:
                    product_id = identity.broker_symbol

                if require_numeric_identifier and (
                    not product_id.isdecimal() or int(product_id) <= 0
                ):
                    msg = (
                        f"Instrument {instrument.canonical!r} requires a positive numeric "
                        f"{normalized_broker_code!r} broker_instrument_id"
                    )
                    raise ValueError(msg)

                instrument_type = identity.broker_instrument_type
                if require_instrument_type and instrument_type is None:
                    msg = (
                        f"Instrument {instrument.canonical!r} requires a typed "
                        f"{normalized_broker_code!r} broker_instrument_type"
                    )
                    raise ValueError(msg)

                venue_identity = (product_id, instrument_type)
                if venue_identity in venue_identities:
                    msg = (
                        f"Market-data selectors repeat {normalized_broker_code!r} "
                        f"venue identity {venue_identity!r}"
                    )
                    raise ValueError(msg)
                instruments.append(
                    MarketDataInstrument(
                        canonical_symbol=str(instrument.canonical),
                        product_id=product_id,
                        asset_class=str(instrument.asset_class),
                        broker_instrument_type=instrument_type,
                    )
                )
                resolved_ids.add(instrument_id)
                venue_identities.add(venue_identity)
        return instruments

    def oldest_candle_ts(self, instr_id: int, *, source: str, timeframe: str) -> datetime | None:
        """Return the oldest stored candle ts for an instrument feed, or None.

        Lets historical backfill be deficit-aware (SW-2): only the window older
        than what is already stored needs to be fetched, so an auto-warm backfill
        at deploy does not re-page already-covered history on every restart.
        """
        with self._session_factory() as session:
            oldest: datetime | None = session.execute(
                select(func.min(InstrumentPrice.ts)).where(
                    InstrumentPrice.instr_id == instr_id,
                    InstrumentPrice.source == source,
                    InstrumentPrice.timeframe == timeframe,
                )
            ).scalar_one_or_none()
            # ``prices.ts`` is ``timestamp without time zone`` (UTC by convention),
            # so the DB hands back a tz-naive datetime. Stamp UTC at this boundary
            # so callers can compare it against tz-aware windows without crashing.
            if oldest is not None and oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=UTC)
            return oldest

    def latest_candle_ts(self, instr_id: int, *, source: str, timeframe: str) -> datetime | None:
        """Return the newest stored candle timestamp for a feed.

        Polling callers use this durable watermark to resume from the last
        committed candle after an outage instead of repeatedly requesting only
        a fixed recent window and leaving an unrecoverable hole.
        """
        with self._session_factory() as session:
            latest: datetime | None = session.execute(
                select(func.max(InstrumentPrice.ts)).where(
                    InstrumentPrice.instr_id == instr_id,
                    InstrumentPrice.source == source,
                    InstrumentPrice.timeframe == timeframe,
                )
            ).scalar_one_or_none()
        if latest is not None and latest.tzinfo is None:
            return latest.replace(tzinfo=UTC)
        return latest

    def candle_count(
        self,
        instr_id: int,
        *,
        source: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> int:
        """Count stored candles in the half-open UTC window ``[start, end)``."""
        start_db = start.astimezone(UTC).replace(tzinfo=None)
        end_db = end.astimezone(UTC).replace(tzinfo=None)
        with self._session_factory() as session:
            count = session.execute(
                select(func.count(InstrumentPrice.price_id)).where(
                    InstrumentPrice.instr_id == instr_id,
                    InstrumentPrice.source == source,
                    InstrumentPrice.timeframe == timeframe,
                    InstrumentPrice.ts >= start_db,
                    InstrumentPrice.ts < end_db,
                )
            ).scalar_one()
        return int(count)

    def fetch_recent_bars(
        self,
        session: Session,
        *,
        instr_id: int,
        source: str,
        timeframe: str,
        limit: int,
        through: datetime | None = None,
    ) -> list[InstrumentPrice]:
        """Return the newest ``limit`` rows for one feed, oldest-first.

        Session-aware by design: the caller owns the session and transaction so
        this read shares the caller's consistency boundary (e.g. the signal
        worker's source-revision fence). This method never opens its own
        session. ``through`` optionally caps the window at an acknowledged
        timestamp (inclusive), passed through to the database unchanged.
        """
        statement = select(InstrumentPrice).where(
            InstrumentPrice.instr_id == instr_id,
            InstrumentPrice.source == source,
            InstrumentPrice.timeframe == timeframe,
        )
        if through is not None:
            statement = statement.where(InstrumentPrice.ts <= through)
        rows = session.scalars(statement.order_by(InstrumentPrice.ts.desc()).limit(limit)).all()
        return list(reversed(rows))

    def fetch_range(
        self,
        session: Session,
        *,
        instr_id: int,
        source: str,
        timeframe: str,
        start: datetime,
        through: datetime,
    ) -> list[InstrumentPrice]:
        """Return the inclusive ``[start, through]`` rows for one feed, oldest-first.

        Session-aware by design: the caller owns the session and transaction
        (see :meth:`fetch_recent_bars`). Timestamps are passed through to the
        database unchanged.
        """
        return list(
            session.scalars(
                select(InstrumentPrice)
                .where(
                    InstrumentPrice.instr_id == instr_id,
                    InstrumentPrice.source == source,
                    InstrumentPrice.timeframe == timeframe,
                    InstrumentPrice.ts >= start,
                    InstrumentPrice.ts <= through,
                )
                .order_by(InstrumentPrice.ts)
            ).all()
        )

    def fetch_bars_since(
        self,
        session: Session,
        *,
        instr_id: int,
        source: str,
        timeframe: str,
        since: datetime,
        limit: int,
    ) -> list[InstrumentPrice]:
        """Return up to ``limit`` rows strictly newer than ``since``, oldest-first.

        Session-aware by design: the caller owns the session and transaction
        (see :meth:`fetch_recent_bars`). ``since`` is passed through to the
        database unchanged so catch-up resumption semantics stay exact.
        """
        return list(
            session.scalars(
                select(InstrumentPrice)
                .where(
                    InstrumentPrice.instr_id == instr_id,
                    InstrumentPrice.source == source,
                    InstrumentPrice.timeframe == timeframe,
                    InstrumentPrice.ts > since,
                )
                .order_by(InstrumentPrice.ts)
                .limit(limit)
            ).all()
        )

    def get_bar_identity(self, session: Session, price_id: int) -> InstrumentPrice | None:
        """Return the currently persisted row for one ``price_id``, or ``None``.

        Session-aware by design: revision fencing must observe the row inside
        the caller's transaction, so this read never opens its own session.
        """
        row: InstrumentPrice | None = session.scalars(
            select(InstrumentPrice).where(InstrumentPrice.price_id == price_id)
        ).one_or_none()
        return row

    def upsert_candles(self, rows: Sequence[CandleRow]) -> int:
        """Idempotent upsert of OHLCV rows into the ``prices`` table.

        Uses PostgreSQL ``INSERT … ON CONFLICT DO UPDATE`` on the
        ``(instr_id, ts, timeframe, source)`` unique constraint.

        Args:
            rows: Sequence of :class:`CandleRow` to upsert.

        Returns:
            Number of valid, de-duplicated observations accepted. Replaying an
            identical candle remains accepted but does not mutate the row or
            request an indicator rebuild.
        """
        if not rows:
            return 0
        valid = [r for r in rows if _is_valid_candle(r)]
        rejected = len(rows) - len(valid)
        if rejected:
            logger.warning(
                "Rejected %d malformed candle(s) failing OHLC invariants before upsert",
                rejected,
            )
        if not valid:
            return 0

        # PostgreSQL rejects one INSERT .. ON CONFLICT statement when the input
        # contains the same conflict target more than once. Providers may emit a
        # corrected candle twice in one response, so collapse deterministically
        # before building the statement. The last provider observation wins.
        deduplicated: dict[tuple[int, datetime, str, str], CandleRow] = {}
        for row in valid:
            key = (row.instr_id, row.ts, row.timeframe, row.source)
            deduplicated[key] = row
        duplicate_count = len(valid) - len(deduplicated)
        if duplicate_count:
            logger.warning(
                "Collapsed %d duplicate candle(s) before upsert",
                duplicate_count,
            )

        ordered_rows = sorted(
            deduplicated.values(),
            key=lambda row: (row.instr_id, row.source, row.timeframe, row.ts),
        )
        dicts: list[dict[str, Any]] = [row.to_dict() for row in ordered_rows]
        insert_stmt = pg_insert(InstrumentPrice).values(dicts)
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=["instr_id", "ts", "timeframe", "source"],
            set_={
                "open": insert_stmt.excluded.open,
                "high": insert_stmt.excluded.high,
                "low": insert_stmt.excluded.low,
                "close": insert_stmt.excluded.close,
                "volume": insert_stmt.excluded.volume,
                "content_revision": InstrumentPrice.content_revision + 1,
            },
            where=or_(
                InstrumentPrice.open.is_distinct_from(insert_stmt.excluded.open),
                InstrumentPrice.high.is_distinct_from(insert_stmt.excluded.high),
                InstrumentPrice.low.is_distinct_from(insert_stmt.excluded.low),
                InstrumentPrice.close.is_distinct_from(insert_stmt.excluded.close),
                InstrumentPrice.volume.is_distinct_from(insert_stmt.excluded.volume),
            ),
        ).returning(
            InstrumentPrice.instr_id,
            InstrumentPrice.ts,
            InstrumentPrice.timeframe,
            InstrumentPrice.source,
        )
        with self._session_factory() as session:
            result = session.execute(stmt)
            changed_rows = result.all()

            # A historical insert (gap repair) and a correction have the same
            # consequence for an already-advanced strategy: its state no longer
            # corresponds to persisted history. Keep the earliest outstanding
            # boundary and advance a generation fence in this same transaction.
            earliest_by_feed: dict[tuple[int, str, str], datetime] = {}
            for changed in changed_rows:
                feed_key = (
                    int(changed.instr_id),
                    str(changed.source),
                    str(changed.timeframe),
                )
                changed_ts = changed.ts
                current = earliest_by_feed.get(feed_key)
                if current is None or changed_ts < current:
                    earliest_by_feed[feed_key] = changed_ts

            for (instr_id, source, timeframe), earliest_ts in earliest_by_feed.items():
                session.execute(
                    update(ConsumerWatermark)
                    .where(
                        ConsumerWatermark.instr_id == instr_id,
                        ConsumerWatermark.source == source,
                        ConsumerWatermark.timeframe == timeframe,
                        or_(
                            ConsumerWatermark.last_ts >= earliest_ts,
                            ConsumerWatermark.last_ts == _CONSUMER_EPOCH.replace(tzinfo=None),
                        ),
                    )
                    .values(
                        rebuild_from_ts=case(
                            (
                                ConsumerWatermark.rebuild_from_ts.is_(None),
                                earliest_ts,
                            ),
                            (
                                ConsumerWatermark.rebuild_from_ts > earliest_ts,
                                earliest_ts,
                            ),
                            else_=ConsumerWatermark.rebuild_from_ts,
                        ),
                        rebuild_generation=ConsumerWatermark.rebuild_generation + 1,
                        updated_at=func.now(),
                    )
                )
            session.commit()
        return len(dicts)
