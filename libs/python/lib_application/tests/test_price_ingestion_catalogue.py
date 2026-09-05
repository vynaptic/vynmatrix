"""Tests for database-backed market-data venue identity selection."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from lib_application.db.models import (
    Base,
    Broker,
    Instrument,
    InstrumentAlias,
    InstrumentBrokerSymbol,
)
from lib_application.services.price_ingestion_service import PriceIngestionService


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_text_symbol_feed_uses_exact_broker_symbol_and_canonical_identity() -> None:
    session_factory = _session_factory()
    with session_factory() as session:
        session.add(Broker(broker_id=1, code="coinbase", name="Coinbase", capabilities={}))
        session.add(
            Instrument(
                instr_id=1,
                canonical="BTC/USDC",
                asset_class="crypto",
                settlement_currency="USDC",
            )
        )
        session.add(InstrumentAlias(instr_id=1, alias="BTCUSDC", source="canonical"))
        session.add(
            InstrumentBrokerSymbol(
                instr_id=1,
                broker_id=1,
                broker_symbol="BTC-USDC",
            )
        )
        session.commit()

    instruments = PriceIngestionService(session_factory).load_market_data_instruments(
        broker_code="coinbase",
        symbols=["BTCUSDC"],
        identifier_field="broker_symbol",
    )

    assert len(instruments) == 1
    assert instruments[0].canonical_symbol == "BTC/USDC"
    assert instruments[0].product_id == "BTC-USDC"
    assert instruments[0].asset_class == "crypto"
    assert instruments[0].broker_instrument_type is None


def test_saxo_feed_preserves_typed_uic_and_asset_type() -> None:
    session_factory = _session_factory()
    with session_factory() as session:
        session.add(Broker(broker_id=7, code="saxo", name="Saxo Bank", capabilities={}))
        session.add(
            Instrument(
                instr_id=1,
                canonical="ASML:xams",
                asset_class="equity",
                settlement_currency="EUR",
            )
        )
        session.add(
            InstrumentBrokerSymbol(
                instr_id=1,
                broker_id=7,
                broker_symbol="ASML:xams",
                broker_instrument_id="1636",
                broker_instrument_type="Stock",
            )
        )
        session.commit()

    instruments = PriceIngestionService(session_factory).load_market_data_instruments(
        broker_code="saxo",
        symbols=["ASML:xams"],
        identifier_field="broker_instrument_id",
        require_numeric_identifier=True,
        require_instrument_type=True,
    )

    assert len(instruments) == 1
    assert instruments[0].canonical_symbol == "ASML:xams"
    assert instruments[0].product_id == "1636"
    assert instruments[0].broker_instrument_type == "Stock"


@pytest.mark.parametrize(
    ("broker_instrument_id", "broker_instrument_type", "message"),
    [
        (None, "Stock", "broker_instrument_id"),
        ("ASML", "Stock", "positive numeric"),
        ("1636", None, "broker_instrument_type"),
    ],
)
def test_typed_feed_identity_fails_closed_when_catalogue_is_incomplete(
    broker_instrument_id: str | None,
    broker_instrument_type: str | None,
    message: str,
) -> None:
    session_factory = _session_factory()
    with session_factory() as session:
        session.add(Broker(broker_id=7, code="saxo", name="Saxo Bank", capabilities={}))
        session.add(
            Instrument(
                instr_id=1,
                canonical="ASML:xams",
                asset_class="equity",
                settlement_currency="EUR",
            )
        )
        session.add(
            InstrumentBrokerSymbol(
                instr_id=1,
                broker_id=7,
                broker_symbol="ASML:xams",
                broker_instrument_id=broker_instrument_id,
                broker_instrument_type=broker_instrument_type,
            )
        )
        session.commit()

    with pytest.raises(ValueError, match=message):
        PriceIngestionService(session_factory).load_market_data_instruments(
            broker_code="saxo",
            symbols=["ASML:xams"],
            identifier_field="broker_instrument_id",
            require_numeric_identifier=True,
            require_instrument_type=True,
        )


def test_feed_selector_cannot_repeat_one_canonical_instrument() -> None:
    session_factory = _session_factory()
    with session_factory() as session:
        session.add(Broker(broker_id=1, code="coinbase", name="Coinbase", capabilities={}))
        session.add(
            Instrument(
                instr_id=1,
                canonical="BTC/USDC",
                asset_class="crypto",
                settlement_currency="USDC",
            )
        )
        session.add(InstrumentAlias(instr_id=1, alias="BTCUSDC", source="canonical"))
        session.add(
            InstrumentBrokerSymbol(
                instr_id=1,
                broker_id=1,
                broker_symbol="BTC-USDC",
            )
        )
        session.commit()

    with pytest.raises(ValueError, match="resolve uniquely"):
        PriceIngestionService(session_factory).load_market_data_instruments(
            broker_code="coinbase",
            symbols=["BTC/USDC", "BTCUSDC"],
            identifier_field="broker_symbol",
        )


def test_feed_selector_cannot_route_one_venue_identity_to_two_instruments() -> None:
    session_factory = _session_factory()
    with session_factory() as session:
        session.add(Broker(broker_id=1, code="coinbase", name="Coinbase", capabilities={}))
        session.add_all(
            [
                Instrument(
                    instr_id=1,
                    canonical="BTC/USDC",
                    asset_class="crypto",
                    settlement_currency="USDC",
                ),
                Instrument(
                    instr_id=2,
                    canonical="BTC/USD",
                    asset_class="crypto",
                    settlement_currency="USD",
                ),
            ]
        )
        session.add_all(
            [
                InstrumentBrokerSymbol(
                    instr_id=1,
                    broker_id=1,
                    broker_symbol="BTC-USDC",
                ),
                InstrumentBrokerSymbol(
                    instr_id=2,
                    broker_id=1,
                    broker_symbol="BTC-USDC",
                ),
            ]
        )
        session.commit()

    with pytest.raises(ValueError, match=r"repeat.*venue identity"):
        PriceIngestionService(session_factory).load_market_data_instruments(
            broker_code="coinbase",
            symbols=["BTC/USDC", "BTC/USD"],
            identifier_field="broker_symbol",
        )
