"""SqlPriceQuoteProvider marks paper fills/NAV to the latest candle (G5)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from execution_engine.market_data import (
    PaperOnlyQuoteAuthorityError,
    SqlExecutionQuoteProvider,
    SqlOwnerScopedDelayedBBOProvider,
    SqlPriceQuoteProvider,
)
from lib_application.db.models import Base, Instrument, InstrumentAlias, InstrumentPrice, User
from lib_infrastructure.market_data.eodhd_client import (
    EODHDDelayedQuote,
    EODHDDelayedQuoteBatch,
    eodhd_delayed_quote_content_sha256,
)
from lib_infrastructure.market_data.eodhd_delayed_quote_store import EODHDDelayedQuoteStore

_BASE_TS = datetime(2026, 6, 1, tzinfo=UTC)
_OBSERVED_AT = _BASE_TS + timedelta(minutes=3)


def _session_local():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _seed_prices(session_local) -> None:
    with session_local() as s:
        s.add(
            Instrument(
                instr_id=1,
                asset_class="crypto",
                canonical="BTC-USD",
                exchange="coinbase",
                settlement_currency="USD",
            )
        )
        s.add(InstrumentAlias(instr_id=1, alias="BTCUSD"))
        for i, close in enumerate((100.0, 100.8, 101.5)):  # newest = 101.5
            s.add(
                InstrumentPrice(
                    instr_id=1,
                    ts=_BASE_TS + timedelta(minutes=i),
                    close=close,
                    source="coinbase_live",
                    timeframe="1m",
                )
            )
        s.commit()


def test_get_quote_returns_latest_close_via_alias_and_canonical() -> None:
    session_local = _session_local()
    _seed_prices(session_local)
    provider = SqlPriceQuoteProvider(
        session_factory=session_local,
        clock=lambda: _OBSERVED_AT,
    )

    via_alias = asyncio.run(provider.get_quote("BTCUSD", user_id="u1", environment="paper"))
    assert via_alias is not None
    assert via_alias["price"] == 101.5
    assert via_alias["bar_open_at"] == _BASE_TS + timedelta(minutes=2)
    assert via_alias["fetched_at"] == _OBSERVED_AT

    via_canonical = asyncio.run(provider.get_quote("BTC-USD", user_id="u1", environment="paper"))
    assert via_canonical is not None
    assert via_canonical["price"] == 101.5


def test_get_quote_unknown_symbol_is_none() -> None:
    session_local = _session_local()
    _seed_prices(session_local)
    provider = SqlPriceQuoteProvider(
        session_factory=session_local,
        clock=lambda: _OBSERVED_AT,
    )
    assert asyncio.run(provider.get_quote("DOGEUSD", user_id="u1", environment="paper")) is None
    assert asyncio.run(provider.get_quote("", user_id="u1", environment="paper")) is None


def test_get_quote_never_substitutes_an_unconfigured_source() -> None:
    session_local = _session_local()
    _seed_prices(session_local)
    with session_local() as session:
        session.add(
            InstrumentPrice(
                instr_id=1,
                ts=_BASE_TS + timedelta(minutes=2),
                close=999.0,
                source="deribit",
                timeframe="1m",
            )
        )
        session.commit()
    provider = SqlPriceQuoteProvider(
        session_factory=session_local,
        clock=lambda: _OBSERVED_AT,
    )

    quote = asyncio.run(provider.get_quote("BTCUSD", user_id="u1", environment="paper"))

    assert quote is not None
    assert quote["price"] == 101.5
    assert quote["source"] == "coinbase_live"


def test_get_quote_uses_configured_timeframe_priority() -> None:
    session_local = _session_local()
    _seed_prices(session_local)
    observed_at = _BASE_TS + timedelta(minutes=6)
    with session_local() as session:
        session.add(
            InstrumentPrice(
                instr_id=1,
                ts=_BASE_TS + timedelta(minutes=1),
                close=999.0,
                source="coinbase_live",
                timeframe="5m",
            )
        )
        session.commit()
    provider = SqlPriceQuoteProvider(
        session_factory=session_local,
        clock=lambda: observed_at,
    )

    quote = asyncio.run(provider.get_quote("BTCUSD", user_id="u1", environment="paper"))

    assert quote is not None
    assert quote["price"] == 101.5
    assert quote["timeframe"] == "1m"


def test_get_quote_excludes_open_and_stale_candles() -> None:
    session_local = _session_local()
    _seed_prices(session_local)
    with session_local() as session:
        session.add(
            InstrumentPrice(
                instr_id=1,
                ts=_OBSERVED_AT,
                close=999.0,
                source="coinbase_live",
                timeframe="1m",
            )
        )
        session.commit()

    fresh_provider = SqlPriceQuoteProvider(
        session_factory=session_local,
        clock=lambda: _OBSERVED_AT,
    )
    stale_provider = SqlPriceQuoteProvider(
        session_factory=session_local,
        clock=lambda: _BASE_TS + timedelta(minutes=20),
    )

    fresh_quote = asyncio.run(fresh_provider.get_quote("BTCUSD", user_id="u1", environment="paper"))
    assert fresh_quote is not None
    assert fresh_quote["price"] == 101.5
    assert (
        asyncio.run(stale_provider.get_quote("BTCUSD", user_id="u1", environment="paper")) is None
    )


def _seed_owner_delayed_quote(  # type: ignore[no-untyped-def]
    session_local,
    *,
    bid_at: datetime | None = None,
    ask_at: datetime | None = None,
) -> None:
    last_trade_at = _BASE_TS - timedelta(minutes=20)
    snapshot_at = _BASE_TS - timedelta(minutes=1)
    quote_fields = {
        "symbol": "AAPL.US",
        "exchange": "XNAS",
        "currency": "USD",
        "last_trade_price": Decimal("200.25"),
        "last_trade_at": last_trade_at,
        "last_trade_size": 10,
        "bid_price": Decimal("200.20"),
        "bid_size": 100,
        "bid_at": bid_at or last_trade_at - timedelta(seconds=2),
        "ask_price": Decimal("200.30"),
        "ask_size": 120,
        "ask_at": ask_at or last_trade_at - timedelta(seconds=1),
        "snapshot_at": snapshot_at,
    }
    quote = EODHDDelayedQuote(
        **quote_fields,
        content_sha256=eodhd_delayed_quote_content_sha256(**quote_fields),
        raw_response_sha256="a" * 64,
    )
    with session_local() as session:
        session.add_all(
            [
                User(user_id="owner-a", email="owner-a@example.test", base_ccy="USD"),
                User(user_id="owner-b", email="owner-b@example.test", base_ccy="USD"),
                Instrument(
                    instr_id=2,
                    asset_class="equity",
                    canonical="AAPL",
                    exchange="XNAS",
                    settlement_currency="USD",
                ),
            ]
        )
        session.flush()
        EODHDDelayedQuoteStore().store_batch(
            session,
            batch=EODHDDelayedQuoteBatch(
                quotes=(quote,),
                retrieved_at=_BASE_TS,
                raw_response_sha256=quote.raw_response_sha256,
            ),
            entitlement_owner_user_id="owner-a",
            instrument_ids={"AAPL.US": 2},
        )
        session.commit()


def test_owner_delayed_quote_is_visible_only_to_owner_and_only_in_paper() -> None:
    session_local = _session_local()
    _seed_owner_delayed_quote(session_local)
    provider = SqlOwnerScopedDelayedBBOProvider(
        session_local,
        max_staleness=timedelta(minutes=30),
        clock=lambda: _BASE_TS,
    )

    quote = asyncio.run(provider.get_quote("AAPL", user_id="owner-a", environment="paper"))

    assert quote is not None
    assert quote["price"] == 200.25
    assert quote["timestamp"] == _BASE_TS - timedelta(minutes=20)
    assert quote["available_at"] == _BASE_TS
    assert quote["execution_authority"] == "paper_only"
    assert quote["market_data_class"] == "delayed"
    assert quote["source"] == "eodhd"
    assert quote["source_content_sha256"] != quote["content_sha256"]
    assert asyncio.run(provider.get_quote("AAPL", user_id="owner-b", environment="paper")) is None
    with pytest.raises(PaperOnlyQuoteAuthorityError, match="paper-only"):
        asyncio.run(provider.get_quote("AAPL", user_id="owner-a", environment="live"))


def test_owner_delayed_quote_rejects_stale_bbo_even_when_trade_is_fresh() -> None:
    session_local = _session_local()
    _seed_owner_delayed_quote(
        session_local,
        bid_at=_BASE_TS - timedelta(minutes=31),
        ask_at=_BASE_TS - timedelta(minutes=31),
    )
    provider = SqlOwnerScopedDelayedBBOProvider(
        session_local,
        max_staleness=timedelta(minutes=30),
        clock=lambda: _BASE_TS,
    )

    with pytest.raises(PaperOnlyQuoteAuthorityError, match="bid became stale"):
        asyncio.run(provider.get_quote("AAPL", user_id="owner-a", environment="paper"))


def test_execution_quote_router_never_falls_back_to_owner_delayed_data_for_live() -> None:
    session_local = _session_local()
    _seed_owner_delayed_quote(session_local)
    router = SqlExecutionQuoteProvider(
        public_provider=SqlPriceQuoteProvider(session_local, clock=lambda: _BASE_TS),
        owner_delayed_provider=SqlOwnerScopedDelayedBBOProvider(
            session_local,
            max_staleness=timedelta(minutes=30),
            clock=lambda: _BASE_TS,
        ),
    )

    paper_quote = asyncio.run(router.get_quote("AAPL", user_id="owner-a", environment="paper"))
    live_quote = asyncio.run(router.get_quote("AAPL", user_id="owner-a", environment="live"))

    assert paper_quote is not None
    assert paper_quote["execution_authority"] == "paper_only"
    assert live_quote is None
