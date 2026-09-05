"""Instrument de-duplication / normalization guards (H2/L5).

A duplicate price-less instrument (e.g. BTCUSD id 11 alongside BTC/USD id 1)
previously fragmented the catalogue. Runtime scoring no longer exposes a
catalogue write surface. These tests lock in collision-safe loading and
normalized alias resolution at the canonical control-plane boundary.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from lib_application.db.models import (
    Base,
    Broker,
    Instrument,
    InstrumentAlias,
    InstrumentBrokerSymbol,
)
from lib_application.services.instrument_resolution import (
    AmbiguousInstrumentError,
    InstrumentIdentityMismatchError,
    resolve_instrument,
)
from lib_application.services.price_ingestion_service import PriceIngestionService


def _memory_factory():  # type: ignore[no-untyped-def]
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)

    @contextmanager
    def factory():  # type: ignore[no-untyped-def]
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    return engine, factory


def test_load_instrument_map_omits_ambiguous_normalized_key() -> None:
    engine, factory = _memory_factory()
    with Session(engine) as s:
        s.add_all(
            [
                Instrument(
                    instr_id=1,
                    canonical="BTC/USD",
                    asset_class="crypto",
                    settlement_currency="USD",
                ),
                Instrument(
                    instr_id=11,
                    canonical="BTCUSD",
                    asset_class="crypto",
                    settlement_currency="USD",
                ),
            ]
        )
        s.commit()
    svc = PriceIngestionService(factory)
    mapping = svc.load_instrument_map(asset_class="crypto")
    assert "BTCUSD" not in mapping


def test_canonical_resolver_consults_alias_and_broker_symbol_tables() -> None:
    engine, _ = _memory_factory()
    with Session(engine) as s:
        s.add(Broker(broker_id=7, code="coinbase", name="Coinbase", capabilities={}))
        s.add(
            Instrument(
                instr_id=1,
                canonical="BTC/USD",
                asset_class="crypto",
                settlement_currency="USD",
            )
        )
        s.add(InstrumentAlias(instr_id=1, alias="XBTUSD"))
        s.add(
            InstrumentBrokerSymbol(
                instr_id=1,
                broker_id=7,
                broker_symbol="BTC-USD-SPOT",
            )
        )
        s.commit()

        assert resolve_instrument(s, "xbt/usd").instr_id == 1
        assert resolve_instrument(s, "btc_usd_spot", broker_id=7).instr_id == 1


def test_canonical_resolver_fails_closed_on_ambiguity_and_identity_mismatch() -> None:
    engine, _ = _memory_factory()
    with Session(engine) as s:
        s.add_all(
            [
                Instrument(
                    instr_id=1,
                    canonical="BTC/USD",
                    asset_class="crypto",
                    settlement_currency="USD",
                ),
                Instrument(
                    instr_id=2,
                    canonical="BTCUSD",
                    asset_class="crypto",
                    settlement_currency="USD",
                ),
                Instrument(
                    instr_id=3,
                    canonical="ETH/USD",
                    asset_class="crypto",
                    settlement_currency="USD",
                ),
            ]
        )
        s.commit()

        with pytest.raises(AmbiguousInstrumentError):
            resolve_instrument(s, "btc-usd", asset_class="crypto")
        with pytest.raises(InstrumentIdentityMismatchError):
            resolve_instrument(
                s,
                "ETH/USD",
                instrument_id=1,
                asset_class="crypto",
            )
