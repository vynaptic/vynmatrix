"""Real PostgreSQL reference functions preserve identities and caller transactions."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lib_application.db.models import Broker, Instrument
from lib_application.db.session import dispose_engine, get_session_factory
from lib_application.services.catalogue import register_brokers, upsert_instruments


@pytest.mark.integration
def test_backend_reference_registration_is_idempotent_and_transactional() -> None:
    url = os.getenv("CATALOGUE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Dedicated CATALOGUE_TEST_DATABASE_URL is required")
    factory = get_session_factory(db_url=url)
    engine = factory.kw["bind"]
    suffix = uuid4().hex[:12]
    broker_code = f"reference-{suffix}"
    symbol = f"UNIT{suffix.upper()}"
    broker = {
        "code": broker_code,
        "name": "Reference transaction test",
        "capabilities": {},
        "environments": [
            {"environment": "paper", "region": "test", "base_urls": {}, "rate_limits": {}}
        ],
    }
    instrument = {
        "symbol": symbol,
        "asset_class": "crypto",
        "settlement_currency": "USD",
        "market_session_policy": "continuous",
        "sector": f"sector-{suffix}",
    }
    try:
        with engine.connect() as connection, connection.begin():
            connection.execute(text("SET LOCAL ROLE vm_backend"))
            with Session(connection) as session:
                register_brokers(session, [broker])
                upsert_instruments(session, [instrument])
                first_broker = session.scalar(
                    select(Broker.broker_id).where(Broker.code == broker_code)
                )
                first_instrument = session.scalar(
                    select(Instrument.instr_id).where(Instrument.canonical == symbol)
                )
                register_brokers(session, [broker])
                upsert_instruments(session, [instrument])
                assert session.scalars(
                    select(Broker.broker_id).where(Broker.code == broker_code)
                ).all() == [first_broker]
                assert session.scalars(
                    select(Instrument.instr_id).where(Instrument.canonical == symbol)
                ).all() == [first_instrument]
                session.rollback()
        with factory() as session:
            assert session.scalar(select(Broker).where(Broker.code == broker_code)) is None
            assert session.scalar(select(Instrument).where(Instrument.canonical == symbol)) is None
    finally:
        dispose_engine(engine)
