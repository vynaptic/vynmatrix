"""Session-aware OHLCV read methods used by the indicator signal worker.

These methods deliberately take the caller's session instead of opening their
own: the signal worker's source-revision fence must observe rows inside its
one strategy transaction, so any hidden session here would break that
consistency boundary.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from lib_application.db.models import Base, Instrument, InstrumentPrice
from lib_application.services.price_ingestion_service import PriceIngestionService

_SOURCE = "coinbase_live"
_TIMEFRAME = "1m"
_BASE_TS = datetime(2026, 5, 4, 12, 0, 0)


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _price_row(instr_id: int, ts: datetime, **overrides: Any) -> InstrumentPrice:
    values: dict[str, Any] = {
        "instr_id": instr_id,
        "ts": ts,
        "timeframe": _TIMEFRAME,
        "source": _SOURCE,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
    }
    values.update(overrides)
    return InstrumentPrice(**values)


def _seed_feed(factory: sessionmaker[Session], *, count: int) -> tuple[int, list[datetime]]:
    """Insert one instrument with ``count`` one-minute bars; return timestamps."""
    with factory() as session:
        instrument = Instrument(
            canonical="BTCUSD",
            asset_class="crypto",
            settlement_currency="USD",
        )
        session.add(instrument)
        session.flush()
        timestamps = [_BASE_TS + timedelta(minutes=index) for index in range(count)]
        for ts in timestamps:
            session.add(_price_row(int(instrument.instr_id), ts))
        # A same-instrument row on another feed must never leak into reads.
        session.add(_price_row(int(instrument.instr_id), _BASE_TS, source="other_feed"))
        session.commit()
        return int(instrument.instr_id), timestamps


def test_fetch_recent_bars_returns_newest_window_oldest_first() -> None:
    factory = _session_factory()
    instr_id, timestamps = _seed_feed(factory, count=5)
    service = PriceIngestionService(factory)

    with factory() as session:
        rows = service.fetch_recent_bars(
            session,
            instr_id=instr_id,
            source=_SOURCE,
            timeframe=_TIMEFRAME,
            limit=3,
        )
        assert [row.ts for row in rows] == timestamps[-3:]
        assert all(row.source == _SOURCE for row in rows)

        capped = service.fetch_recent_bars(
            session,
            instr_id=instr_id,
            source=_SOURCE,
            timeframe=_TIMEFRAME,
            limit=10,
            through=timestamps[2],
        )
        # ``through`` is inclusive: exactly the acknowledged prefix comes back.
        assert [row.ts for row in capped] == timestamps[:3]


def test_fetch_range_is_inclusive_on_both_boundaries() -> None:
    factory = _session_factory()
    instr_id, timestamps = _seed_feed(factory, count=5)
    service = PriceIngestionService(factory)

    with factory() as session:
        rows = service.fetch_range(
            session,
            instr_id=instr_id,
            source=_SOURCE,
            timeframe=_TIMEFRAME,
            start=timestamps[1],
            through=timestamps[3],
        )
        assert [row.ts for row in rows] == timestamps[1:4]


def test_fetch_bars_since_is_strictly_newer_and_bounded() -> None:
    factory = _session_factory()
    instr_id, timestamps = _seed_feed(factory, count=5)
    service = PriceIngestionService(factory)

    with factory() as session:
        rows = service.fetch_bars_since(
            session,
            instr_id=instr_id,
            source=_SOURCE,
            timeframe=_TIMEFRAME,
            since=timestamps[1],
            limit=2,
        )
        # Strictly newer than ``since`` and capped at ``limit``, oldest-first.
        assert [row.ts for row in rows] == timestamps[2:4]


def test_get_bar_identity_returns_exact_row_or_none() -> None:
    factory = _session_factory()
    instr_id, timestamps = _seed_feed(factory, count=1)
    service = PriceIngestionService(factory)

    with factory() as session:
        stored = (
            session.query(InstrumentPrice)
            .filter_by(instr_id=instr_id, ts=timestamps[0], source=_SOURCE)
            .one()
        )
        row = service.get_bar_identity(session, int(stored.price_id))
        assert row is not None
        assert int(row.price_id) == int(stored.price_id)
        assert int(row.content_revision) == 1
        assert service.get_bar_identity(session, int(stored.price_id) + 10_000) is None


def test_reads_share_the_callers_transaction() -> None:
    """The service must never open its own session for these reads.

    An uncommitted row flushed in the caller's transaction is visible to the
    read, and disappears again after rollback — proving the read executed
    inside the caller's transaction rather than a private session.
    """
    factory = _session_factory()
    instr_id, timestamps = _seed_feed(factory, count=1)
    service = PriceIngestionService(factory)
    pending_ts = _BASE_TS + timedelta(minutes=1)

    with factory() as session:
        session.add(_price_row(instr_id, pending_ts))
        session.flush()

        rows = service.fetch_bars_since(
            session,
            instr_id=instr_id,
            source=_SOURCE,
            timeframe=_TIMEFRAME,
            since=timestamps[0],
            limit=10,
        )
        assert [row.ts for row in rows] == [pending_ts]
        session.rollback()

    with factory() as session:
        assert (
            service.fetch_bars_since(
                session,
                instr_id=instr_id,
                source=_SOURCE,
                timeframe=_TIMEFRAME,
                since=timestamps[0],
                limit=10,
            )
            == []
        )
