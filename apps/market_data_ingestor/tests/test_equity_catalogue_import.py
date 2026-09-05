"""Reviewed equity catalogue import, dry-run, and exact-replay contracts."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    Base,
    Broker,
    Instrument,
    InstrumentBrokerSymbol,
    MarketCalendar,
)
from lib_common.hashing import canonical_json_bytes
from market_data_ingestor.equity_catalogue_import import (
    EquityCatalogueImportError,
    apply_reviewed_equity_catalogue,
    load_reviewed_equity_catalogue_artifact,
)

_REVIEWED_AT = datetime(2026, 8, 16, 12, tzinfo=UTC)


def _payload(*, symbols: tuple[str, ...] = ("AAPL", "MSFT")) -> dict[str, object]:
    conids = {"AAPL": "265598", "MSFT": "272093"}
    exchanges = {"AAPL": "NASDAQ", "MSFT": "NASDAQ"}
    return {
        "instruments": [
            {
                "asset_class": "equity",
                "broker_code": "ibkr",
                "broker_instrument_id": conids[symbol],
                "broker_instrument_type": "STK",
                "broker_symbol": symbol,
                "calendar_code": "XNYS",
                "canonical": symbol,
                "exchange": exchanges[symbol],
                "is_tradable": True,
                "lot_size": "1",
                "market_session_policy": "scheduled",
                "settlement_currency": "USD",
                "tick_size": "0.01",
            }
            for symbol in symbols
        ],
        "reviewed_at": _REVIEWED_AT.isoformat(),
        "reviewer": "portfolio-operations",
        "schema": "vynmatrix.equity-catalogue-import.v1",
        "source_reference": "reviewed IBKR Client Portal contract export",
    }


def _artifact(*, symbols: tuple[str, ...] = ("AAPL", "MSFT")):
    content = canonical_json_bytes(_payload(symbols=symbols))
    return load_reviewed_equity_catalogue_artifact(
        content,
        expected_sha256=hashlib.sha256(content).hexdigest(),
    )


def _engine(*, include_broker: bool = True, include_calendar: bool = True):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        if include_broker:
            session.add(Broker(code="ibkr", name="Interactive Brokers", capabilities={}))
        if include_calendar:
            session.add(
                MarketCalendar(
                    code="XNYS",
                    source_kind="exchange",
                    provider="ice_nyse",
                    source_reference="https://www.nyse.com/markets/hours-calendars",
                )
            )
    return engine


def test_dry_run_apply_and_exact_replay_share_one_deterministic_plan() -> None:
    engine = _engine()
    artifact = _artifact()

    with Session(engine) as session, session.begin():
        dry_run = apply_reviewed_equity_catalogue(session, artifact, dry_run=True)
    assert dry_run.dry_run is True
    assert dry_run.instruments_created == ("AAPL", "MSFT")
    assert dry_run.broker_mappings_created == ("AAPL", "MSFT")
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Instrument)) == 0

    with Session(engine) as session, session.begin():
        applied = apply_reviewed_equity_catalogue(session, artifact, dry_run=False)
    assert applied.instruments_created == dry_run.instruments_created
    assert applied.broker_mappings_created == dry_run.broker_mappings_created

    with Session(engine) as session, session.begin():
        replay = apply_reviewed_equity_catalogue(session, artifact, dry_run=False)
    assert replay.exact_replays == ("AAPL", "MSFT")
    assert replay.instruments_created == ()
    assert replay.instruments_completed == ()
    assert replay.broker_mappings_created == ()

    with Session(engine) as session:
        instruments = tuple(session.scalars(select(Instrument).order_by(Instrument.canonical)))
        mappings = tuple(session.scalars(select(InstrumentBrokerSymbol)))
    assert len(instruments) == len(mappings) == 2
    assert all(item.asset_class == "equity" for item in instruments)
    assert all(item.market_calendar_id is not None for item in instruments)
    assert all(item.lot_size == Decimal(1) for item in instruments)
    assert {item.broker_instrument_type for item in mappings} == {"STK"}


def test_apply_completes_only_null_fields_and_attaches_existing_xnys() -> None:
    engine = _engine()
    artifact = _artifact(symbols=("AAPL",))
    with Session(engine) as session, session.begin():
        session.add(
            Instrument(
                asset_class="equity",
                canonical="AAPL",
                exchange=None,
                settlement_currency="USD",
                tick_size=None,
                lot_size=None,
                is_tradable=True,
                market_session_policy="scheduled",
                market_calendar_id=None,
            )
        )

    with Session(engine) as session, session.begin():
        result = apply_reviewed_equity_catalogue(session, artifact, dry_run=False)

    assert result.instruments_completed == ("AAPL",)
    assert result.broker_mappings_created == ("AAPL",)
    with Session(engine) as session:
        instrument = session.scalar(select(Instrument).where(Instrument.canonical == "AAPL"))
        assert instrument is not None
        assert instrument.exchange == "NASDAQ"
        assert instrument.tick_size == Decimal("0.01")
        assert instrument.lot_size == Decimal(1)
        assert instrument.market_calendar_id is not None


def test_apply_fails_closed_on_conflicting_reviewed_lot_size() -> None:
    engine = _engine()
    artifact = _artifact(symbols=("AAPL",))
    with Session(engine) as session, session.begin():
        calendar = session.scalar(select(MarketCalendar).where(MarketCalendar.code == "XNYS"))
        assert calendar is not None
        session.add(
            Instrument(
                asset_class="equity",
                canonical="AAPL",
                exchange="NASDAQ",
                settlement_currency="USD",
                tick_size=Decimal("0.01"),
                lot_size=Decimal("100"),
                is_tradable=True,
                market_session_policy="scheduled",
                market_calendar_id=int(calendar.calendar_id),
            )
        )

    with (
        Session(engine) as session,
        session.begin(),
        pytest.raises(
            EquityCatalogueImportError,
            match=r"AAPL differs in reviewed fields: lot_size",
        ),
    ):
        apply_reviewed_equity_catalogue(session, artifact, dry_run=False)

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(InstrumentBrokerSymbol)) == 0


def test_apply_fails_closed_when_conid_belongs_to_another_instrument() -> None:
    engine = _engine()
    artifact = _artifact(symbols=("AAPL",))
    with Session(engine) as session, session.begin():
        calendar = session.scalar(select(MarketCalendar).where(MarketCalendar.code == "XNYS"))
        broker = session.scalar(select(Broker).where(Broker.code == "ibkr"))
        assert calendar is not None
        assert broker is not None
        other = Instrument(
            asset_class="equity",
            canonical="OTHER",
            exchange="NASDAQ",
            settlement_currency="USD",
            tick_size=Decimal("0.01"),
            lot_size=Decimal("1"),
            is_tradable=True,
            market_session_policy="scheduled",
            market_calendar_id=int(calendar.calendar_id),
        )
        session.add(other)
        session.flush()
        session.add(
            InstrumentBrokerSymbol(
                instr_id=int(other.instr_id),
                broker_id=int(broker.broker_id),
                broker_symbol="OTHER",
                broker_instrument_id="265598",
                broker_instrument_type="STK",
            )
        )

    with (
        Session(engine) as session,
        session.begin(),
        pytest.raises(
            EquityCatalogueImportError,
            match=r"IBKR conId 265598 belongs to another instrument",
        ),
    ):
        apply_reviewed_equity_catalogue(session, artifact, dry_run=True)


def test_loader_requires_pinned_canonical_bytes_and_reviewed_lot_size() -> None:
    payload = _payload(symbols=("AAPL",))
    content = canonical_json_bytes(payload)
    with pytest.raises(EquityCatalogueImportError, match="expected SHA-256"):
        load_reviewed_equity_catalogue_artifact(content, expected_sha256="b" * 64)

    instruments = payload["instruments"]
    assert isinstance(instruments, list)
    instruments[0]["lot_size"] = 1
    changed = canonical_json_bytes(payload)
    with pytest.raises(EquityCatalogueImportError, match="reviewed decimal string"):
        load_reviewed_equity_catalogue_artifact(
            changed,
            expected_sha256=hashlib.sha256(changed).hexdigest(),
        )


@pytest.mark.parametrize(
    ("include_broker", "include_calendar", "message"),
    [
        (True, False, "existing official XNYS calendar"),
        (False, True, "seeded IBKR broker"),
    ],
)
def test_apply_requires_existing_catalogue_authorities(
    include_broker: bool,
    include_calendar: bool,
    message: str,
) -> None:
    engine = _engine(
        include_broker=include_broker,
        include_calendar=include_calendar,
    )
    artifact = _artifact(symbols=("AAPL",))

    with (
        Session(engine) as session,
        session.begin(),
        pytest.raises(EquityCatalogueImportError, match=message),
    ):
        apply_reviewed_equity_catalogue(session, artifact, dry_run=True)
