"""Owner, idempotency, and correction contracts for delayed EODHD quotes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from lib_application.db.models import (
    Base,
    EquityObservation,
    EquityObservationValue,
    EquitySourceLineage,
    Instrument,
    User,
)
from lib_application.services.equity_lineage import OWNER_SCOPED_DELAYED_BBO_CONTRACT
from lib_infrastructure.market_data.eodhd_client import (
    EODHDDelayedQuote,
    EODHDDelayedQuoteBatch,
    eodhd_delayed_quote_content_sha256,
)
from lib_infrastructure.market_data.eodhd_delayed_quote_store import (
    EODHD_PERSONAL_PAPER_ENTITLEMENT,
    EODHDDelayedQuotePersistenceError,
    EODHDDelayedQuoteStore,
)

_TRADE_AT = datetime(2026, 7, 31, 19, 45, tzinfo=UTC)
_SNAPSHOT_AT = datetime(2026, 7, 31, 20, 5, tzinfo=UTC)
_RETRIEVED_AT = datetime(2026, 7, 31, 20, 6, tzinfo=UTC)


def _engine() -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _seed(session: Session) -> None:
    session.add_all(
        [
            User(user_id="owner-a", email="a@example.test", base_ccy="USD"),
            User(user_id="owner-b", email="b@example.test", base_ccy="USD"),
            Instrument(
                instr_id=101,
                asset_class="equity",
                canonical="AAPL",
                exchange="XNAS",
                settlement_currency="USD",
            ),
        ]
    )
    session.flush()


def _quote(
    price: str,
    *,
    raw_digest_character: str,
    bid_at: datetime | None = None,
    ask_at: datetime | None = None,
) -> EODHDDelayedQuote:
    kwargs = {
        "symbol": "AAPL.US",
        "exchange": "XNAS",
        "currency": "USD",
        "last_trade_price": Decimal(price),
        "last_trade_at": _TRADE_AT,
        "last_trade_size": 50,
        "bid_price": Decimal("199.95"),
        "bid_size": 100,
        "bid_at": bid_at or _TRADE_AT - timedelta(seconds=2),
        "ask_price": Decimal("200.05"),
        "ask_size": 80,
        "ask_at": ask_at or _TRADE_AT - timedelta(seconds=1),
        "snapshot_at": _SNAPSHOT_AT,
    }
    return EODHDDelayedQuote(
        **kwargs,
        content_sha256=eodhd_delayed_quote_content_sha256(**kwargs),
        raw_response_sha256=raw_digest_character * 64,
    )


def _batch(quote: EODHDDelayedQuote, *, retrieved_at: datetime = _RETRIEVED_AT):
    return EODHDDelayedQuoteBatch(
        quotes=(quote,),
        retrieved_at=retrieved_at,
        raw_response_sha256=quote.raw_response_sha256,
    )


def test_store_is_owner_scoped_idempotent_and_preserves_typed_event_times() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed(session)
        store = EODHDDelayedQuoteStore()
        first = store.store_batch(
            session,
            batch=_batch(_quote("200", raw_digest_character="a")),
            entitlement_owner_user_id="owner-a",
            instrument_ids={"AAPL.US": 101},
        )[0]
        repeated = store.store_batch(
            session,
            batch=_batch(_quote("200", raw_digest_character="a")),
            entitlement_owner_user_id="owner-a",
            instrument_ids={"AAPL.US": 101},
        )[0]

        assert repeated == first
        assert session.query(EquitySourceLineage).count() == 1
        assert session.query(EquityObservation).count() == 1
        lineage = session.get(EquitySourceLineage, first.lineage_id)
        assert lineage is not None
        assert lineage.entitlement_owner_user_id == "owner-a"
        assert lineage.entitlement_scope == EODHD_PERSONAL_PAPER_ENTITLEMENT
        observation = session.get(EquityObservation, first.observation_id)
        assert observation is not None
        assert observation.observation_kind == "price"
        assert observation.event_at == _TRADE_AT.replace(tzinfo=None)
        assert observation.content_sha256 != lineage.content_sha256
        values = {
            row.field_name: row
            for row in session.query(EquityObservationValue)
            .filter(EquityObservationValue.observation_id == first.observation_id)
            .all()
        }
        assert values["quote_contract"].text_value == OWNER_SCOPED_DELAYED_BBO_CONTRACT
        assert values["source_content_sha256"].text_value == lineage.content_sha256
        assert values["last_trade_at"].timestamp_value == _TRADE_AT.replace(tzinfo=None)
        assert values["bid_at"].timestamp_value == (_TRADE_AT - timedelta(seconds=2)).replace(
            tzinfo=None
        )
        assert values["ask_at"].timestamp_value == (_TRADE_AT - timedelta(seconds=1)).replace(
            tzinfo=None
        )


def test_store_rejects_future_or_post_snapshot_bbo_timestamps() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed(session)
        store = EODHDDelayedQuoteStore()
        with pytest.raises(
            EODHDDelayedQuotePersistenceError,
            match="bid timestamp is future-dated",
        ):
            store.store_batch(
                session,
                batch=_batch(
                    _quote(
                        "200",
                        raw_digest_character="a",
                        bid_at=_RETRIEVED_AT + timedelta(seconds=6),
                    )
                ),
                entitlement_owner_user_id="owner-a",
                instrument_ids={"AAPL.US": 101},
            )


def test_changed_same_snapshot_creates_append_only_correction_chain() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed(session)
        store = EODHDDelayedQuoteStore()
        first = store.store_batch(
            session,
            batch=_batch(_quote("200", raw_digest_character="a")),
            entitlement_owner_user_id="owner-a",
            instrument_ids={"AAPL.US": 101},
        )[0]
        corrected = store.store_batch(
            session,
            batch=_batch(
                _quote("201", raw_digest_character="b"),
                retrieved_at=_RETRIEVED_AT + timedelta(minutes=1),
            ),
            entitlement_owner_user_id="owner-a",
            instrument_ids={"AAPL.US": 101},
        )[0]

        assert corrected.revision == 2
        assert corrected.supersedes_observation_id == first.observation_id
        assert corrected.observation_id != first.observation_id
        assert session.query(EquitySourceLineage).count() == 2
        assert session.query(EquityObservation).count() == 2
        original = session.get(EquityObservation, first.observation_id)
        assert original is not None
        assert original.revision == 1
        assert original.supersedes_observation_id is None


def test_changed_raw_response_is_bound_into_append_only_observation_identity() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed(session)
        store = EODHDDelayedQuoteStore()
        first = store.store_batch(
            session,
            batch=_batch(_quote("200", raw_digest_character="a")),
            entitlement_owner_user_id="owner-a",
            instrument_ids={"AAPL.US": 101},
        )[0]
        reacquired = store.store_batch(
            session,
            batch=_batch(
                _quote("200", raw_digest_character="b"),
                retrieved_at=_RETRIEVED_AT + timedelta(minutes=1),
            ),
            entitlement_owner_user_id="owner-a",
            instrument_ids={"AAPL.US": 101},
        )[0]

        assert reacquired.lineage_id == first.lineage_id
        assert reacquired.observation_id != first.observation_id
        assert reacquired.revision == 2
        assert reacquired.supersedes_observation_id == first.observation_id
