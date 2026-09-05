"""Tests for exact broker-scoped instrument catalogue resolution."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from lib_application.db.models import (
    Base,
    Broker,
    Instrument,
    InstrumentBrokerSymbol,
)
from lib_application.services.instrument_resolution import (
    InstrumentResolutionError,
    resolve_broker_instrument_identity,
)


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_exact_broker_identity_preserves_opaque_id_and_type() -> None:
    with _session() as session:
        session.add(Broker(broker_id=7, code="saxo", name="Saxo", capabilities={}))
        session.add(
            Instrument(
                instr_id=1,
                canonical="EUR/USD",
                asset_class="fx",
                settlement_currency="USD",
                lot_size=Decimal("1000"),
            )
        )
        session.add(
            InstrumentBrokerSymbol(
                instr_id=1,
                broker_id=7,
                broker_symbol="EURUSD",
                broker_instrument_id="21",
                broker_instrument_type="FxSpot",
            )
        )
        session.commit()

        resolved = resolve_broker_instrument_identity(
            session,
            instrument_id=1,
            broker_code="SAXO",
        )

    assert resolved is not None
    assert resolved.broker_code == "saxo"
    assert resolved.broker_symbol == "EURUSD"
    assert resolved.broker_instrument_id == "21"
    assert resolved.broker_instrument_type == "FxSpot"
    assert resolved.lot_size == Decimal("1000")


def test_exact_broker_identity_does_not_fall_back_to_another_broker() -> None:
    with _session() as session:
        session.add(Broker(broker_id=1, code="coinbase", name="Coinbase", capabilities={}))
        session.add(
            Instrument(
                instr_id=1,
                canonical="BTC/USD",
                asset_class="crypto",
                settlement_currency="USD",
            )
        )
        session.add(
            InstrumentBrokerSymbol(
                instr_id=1,
                broker_id=1,
                broker_symbol="BTC-USD",
            )
        )
        session.commit()

        assert (
            resolve_broker_instrument_identity(
                session,
                instrument_id=1,
                broker_code="saxo",
            )
            is None
        )


def test_exact_broker_identity_rejects_boolean_instrument_id() -> None:
    with (
        _session() as session,
        pytest.raises(
            InstrumentResolutionError,
            match="positive instrument_id",
        ),
    ):
        resolve_broker_instrument_identity(
            session,
            instrument_id=True,
            broker_code="saxo",
        )


def test_exact_broker_identity_rejects_invalid_lot_size() -> None:
    with _session() as session:
        session.add(Broker(broker_id=7, code="ibkr", name="IBKR", capabilities={}))
        session.add(
            Instrument(
                instr_id=1,
                canonical="AAPL",
                asset_class="equity",
                settlement_currency="USD",
                lot_size=Decimal("0"),
            )
        )
        session.add(
            InstrumentBrokerSymbol(
                instr_id=1,
                broker_id=7,
                broker_symbol="AAPL",
                broker_instrument_id="265598",
                broker_instrument_type="STK",
            )
        )
        session.commit()

        with pytest.raises(InstrumentResolutionError, match="invalid lot_size"):
            resolve_broker_instrument_identity(
                session,
                instrument_id=1,
                broker_code="ibkr",
            )
