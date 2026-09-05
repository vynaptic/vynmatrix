"""Tests for prospective US Quality Compounder universe evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from lib_application.db.models import (
    Base,
    Broker,
    Instrument,
    InstrumentBrokerSymbol,
    MarketCalendar,
    User,
)
from lib_infrastructure.market_data.eodhd_client import EODHDJsonEvidence
from market_data_ingestor.quality_compounder_universe import (
    QualityCompounderIdentityEvidence,
    QualityCompounderSecurityIdentity,
    QualityCompounderUniverseComponent,
    QualityCompounderUniverseError,
    parse_quality_compounder_benchmark_identity,
    parse_quality_compounder_components,
    parse_quality_compounder_security_identity,
    persist_quality_compounder_benchmark_identity,
    persist_quality_compounder_universe,
)

_CLOSE = datetime(2026, 6, 30, 20, 0, tzinfo=UTC)
_RETRIEVED = datetime(2026, 6, 30, 21, 0, tzinfo=UTC)
_CUTOFF = datetime(2026, 6, 30, 22, 0, tzinfo=UTC)


def _evidence(endpoint: str, payload: object, *, minute: int = 0) -> EODHDJsonEvidence:
    content = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return EODHDJsonEvidence(
        endpoint=endpoint,
        retrieved_at=_RETRIEVED.replace(minute=minute),
        payload=payload,
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )


def _component_payload() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    current: dict[str, object] = {}
    historical: dict[str, object] = {}
    ticker_history: dict[str, object] = {}
    for index in range(500):
        symbol = f"S{index:03d}"
        current_name = "Issuer 0 Corporation" if index == 0 else f"Issuer {index}"
        corroborating_name = "Issuer 0 Corp" if index == 0 else f"Issuer {index}"
        current[str(index)] = {
            "Code": symbol,
            "Name": current_name,
            "Exchange": "NASDAQ",
            "Sector": f"Sector {index % 10}",
            "Industry": f"Industry {index % 25}",
            "Weight": "0.002",
        }
        historical[str(index)] = {
            "Code": symbol,
            "Name": corroborating_name,
            "Exchange": "NASDAQ",
            "Date": "2026-06-30",
        }
        ticker_history[str(index)] = {
            "Code": symbol,
            "Name": corroborating_name,
            "IsActiveNow": 1,
            "IsDelisted": 0,
        }
    return (
        current,
        {"HistoricalComponents": {"2026-06-30": historical}},
        ticker_history,
    )


def _identity_evidence(
    symbol: str = "AAA",
    *,
    name: str = "Alpha Corporation",
    security_type: str = "Common Stock",
    exchange: str = "NASDAQ",
) -> QualityCompounderIdentityEvidence:
    mapping = {
        "meta": {"total": 1},
        "links": {"next": None},
        "data": [
            {
                "symbol": f"{symbol}.US",
                "figi": "BBG000TEST01",
                "isin": "US0000000001",
                "cusip": "000000001",
                "cik": "1234",
            }
        ],
    }
    general = {
        "Code": symbol,
        "Name": name,
        "Type": security_type,
        "Exchange": exchange,
        "CurrencyCode": "USD",
        "CountryISO": "US",
        "CountryName": "United States",
        "IsDelisted": False,
        "CIK": "0000001234",
        "ISIN": "US0000000001",
        "IPODate": "2000-01-03",
    }
    return QualityCompounderIdentityEvidence(
        mapping=_evidence(f"/api/id-mapping?symbol={symbol}.US", mapping, minute=1),
        general=_evidence(f"/api/v1.1/fundamentals/{symbol}.US?filter=General", general, minute=2),
    )


def _engine() -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)

    @sa.event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


def _seed_catalogue(
    session: Session,
    *,
    conid: str | None = "123456",
    lot_size: Decimal | None = Decimal("1"),
) -> None:
    session.add(User(user_id="owner-1", email="owner@example.com", base_ccy="EUR", status="active"))
    broker = Broker(code="ibkr", name="Interactive Brokers", capabilities={})
    calendar = MarketCalendar(
        code="XNYS",
        source_kind="exchange",
        provider="NYSE",
        source_reference="test official calendar",
    )
    session.add_all((broker, calendar))
    session.flush()
    instrument = Instrument(
        instr_id=1,
        asset_class="equity",
        canonical="AAA",
        exchange="NASDAQ",
        settlement_currency="USD",
        lot_size=lot_size,
        is_tradable=True,
        market_session_policy="scheduled",
        market_calendar_id=int(calendar.calendar_id),
    )
    session.add(instrument)
    session.flush()
    session.add(
        InstrumentBrokerSymbol(
            instr_id=1,
            broker_id=int(broker.broker_id),
            broker_symbol="AAA",
            broker_instrument_id=conid,
            broker_instrument_type="STK",
        )
    )
    session.flush()


def test_current_components_must_equal_latest_historical_checkpoint() -> None:
    current, historical, ticker_history = _component_payload()

    parsed = parse_quality_compounder_components(
        current=_evidence("/api/v1.1/fundamentals/GSPC.INDX?filter=Components", current),
        historical=_evidence(
            "/api/v1.1/fundamentals/GSPC.INDX?historical=1",
            historical,
        ),
        ticker_history=_evidence(
            "/api/fundamentals/GSPC.INDX?filter=HistoricalTickerComponents",
            ticker_history,
        ),
        decision_session=date(2026, 6, 30),
    )

    assert len(parsed) == 500
    assert parsed[0].symbol == "S000"
    assert sum(item.weight for item in parsed) == Decimal("1.000")

    historical_rows = historical["HistoricalComponents"]["2026-06-30"]  # type: ignore[index]
    historical_rows["0"]["Code"] = "DIFFERENT"  # type: ignore[index]
    with pytest.raises(QualityCompounderUniverseError, match="differs"):
        parse_quality_compounder_components(
            current=_evidence("/api/current", current),
            historical=_evidence("/api/historical", historical),
            ticker_history=_evidence("/api/ticker-history", ticker_history),
            decision_session=date(2026, 6, 30),
        )


def test_security_identity_requires_common_permanent_us_share_class() -> None:
    component = QualityCompounderUniverseComponent(
        symbol="AAA",
        name="Alpha Corporation",
        exchange="NASDAQ",
        sector="Technology",
        industry="Software",
        weight=Decimal("0.002"),
    )

    identity = parse_quality_compounder_security_identity(
        component,
        evidence=_identity_evidence(),
    )

    assert identity.security_id == "figi:BBG000TEST01"
    assert identity.issuer_id == "cik:0000001234"
    assert identity.listing_date == date(2000, 1, 3)


def test_spy_benchmark_identity_persists_as_etf_on_shared_calendar() -> None:
    source = _identity_evidence(
        "SPY",
        name="SPDR S&P 500 ETF Trust",
        security_type="ETF",
        exchange="NYSE",
    )
    identity = parse_quality_compounder_benchmark_identity(evidence=source)

    with Session(_engine()) as session:
        session.add(
            User(
                user_id="owner-1",
                email="owner@example.com",
                base_ccy="EUR",
                status="active",
            )
        )
        calendar = MarketCalendar(
            code="XNYS",
            source_kind="exchange",
            provider="ice_nyse",
            source_reference="https://www.nyse.com/markets/hours-calendars",
        )
        session.add(calendar)
        session.flush()
        session.add(
            Instrument(
                asset_class="etf",
                canonical="SPY",
                exchange="NYSE",
                settlement_currency="USD",
                is_tradable=True,
                market_session_policy="scheduled",
                market_calendar_id=int(calendar.calendar_id),
            )
        )
        session.flush()

        first = persist_quality_compounder_benchmark_identity(
            session,
            identity=identity,
            decision_session=date(2026, 6, 30),
            decision_close=_CLOSE,
            cutoff=_CUTOFF,
            entitlement_owner_user_id="owner-1",
        )
        second = persist_quality_compounder_benchmark_identity(
            session,
            identity=identity,
            decision_session=date(2026, 6, 30),
            decision_close=_CLOSE,
            cutoff=_CUTOFF,
            entitlement_owner_user_id="owner-1",
        )

        assert first == second
        assert first.canonical_symbol == "SPY"


def test_persistence_requires_exact_ibkr_conid_and_is_idempotent() -> None:
    component = QualityCompounderUniverseComponent(
        symbol="AAA",
        name="Alpha Corporation",
        exchange="NASDAQ",
        sector="Technology",
        industry="Software",
        weight=Decimal("1"),
    )
    source = _identity_evidence()
    identity = QualityCompounderSecurityIdentity(
        symbol="AAA",
        security_id="figi:BBG000TEST01",
        issuer_id="cik:0000001234",
        name="Alpha Corporation",
        exchange="NASDAQ",
        sector="Technology",
        industry="Software",
        listing_date=date(2000, 1, 3),
        source=source,
    )
    current = _evidence("/api/current", {"AAA": True})
    historical = _evidence("/api/historical", {"AAA": True}, minute=3)
    ticker_history = _evidence("/api/ticker-history", {"AAA": True}, minute=4)
    with Session(_engine()) as session:
        _seed_catalogue(session)
        first = persist_quality_compounder_universe(
            session,
            components=(component,),
            identities={"AAA": identity},
            current_evidence=current,
            historical_evidence=historical,
            ticker_history_evidence=ticker_history,
            decision_session=date(2026, 6, 30),
            decision_close=_CLOSE,
            cutoff=_CUTOFF,
            entitlement_owner_user_id="owner-1",
        )
        replay = persist_quality_compounder_universe(
            session,
            components=(component,),
            identities={"AAA": identity},
            current_evidence=current,
            historical_evidence=historical,
            ticker_history_evidence=ticker_history,
            decision_session=date(2026, 6, 30),
            decision_close=_CLOSE,
            cutoff=_CUTOFF,
            entitlement_owner_user_id="owner-1",
        )

        assert first.members == replay.members
        assert first.members[0].instrument_id == 1
        assert len(first.membership_observation_sha256_by_instrument[1]) == 64

    with Session(_engine()) as session:
        _seed_catalogue(session, conid=None)
        with pytest.raises(
            QualityCompounderUniverseError,
            match="IBKR stock conid and whole-share lot size",
        ):
            persist_quality_compounder_universe(
                session,
                components=(component,),
                identities={"AAA": identity},
                current_evidence=current,
                historical_evidence=historical,
                ticker_history_evidence=ticker_history,
                decision_session=date(2026, 6, 30),
                decision_close=_CLOSE,
                cutoff=_CUTOFF,
                entitlement_owner_user_id="owner-1",
            )

    with Session(_engine()) as session:
        _seed_catalogue(session, lot_size=None)
        with pytest.raises(
            QualityCompounderUniverseError,
            match="IBKR stock conid and whole-share lot size",
        ):
            persist_quality_compounder_universe(
                session,
                components=(component,),
                identities={"AAA": identity},
                current_evidence=current,
                historical_evidence=historical,
                ticker_history_evidence=ticker_history,
                decision_session=date(2026, 6, 30),
                decision_close=_CLOSE,
                cutoff=_CUTOFF,
                entitlement_owner_user_id="owner-1",
            )
