"""Tests for prospective US Quality Compounder daily-market evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    Base,
    EquityObservation,
    EquityObservationValue,
    EquitySourceLineage,
    Instrument,
    User,
)
from lib_infrastructure.market_data.eodhd_client import EODHDJsonEvidence
from lib_strategy.equity_transaction_costs import DailyBarCostModelPolicy
from market_data_ingestor.quality_compounder_market import (
    QUALITY_COMPOUNDER_MARKET_ADJUSTMENT_POLICY,
    QualityCompounderMarketError,
    acquire_quality_compounder_market_series,
    persist_quality_compounder_market_series,
)
from market_data_ingestor.quality_compounder_universe import (
    QualityCompounderIdentityEvidence,
    QualityCompounderSecurityIdentity,
)

_OWNER = "quality-owner"
_CLOSE = datetime(2024, 6, 28, 20, 0, tzinfo=UTC)
_NEXT_OPEN = datetime(2024, 7, 1, 13, 30, tzinfo=UTC)
_RETRIEVED = datetime(2024, 6, 29, 0, 5, tzinfo=UTC)

# Fixed recorded AAPL EOD rows. They are unit-boundary fixtures, not a backtest.
_AAPL_ROWS = [
    {
        "date": "2024-06-26",
        "open": 211.5,
        "high": 214.86,
        "low": 210.64,
        "close": 213.25,
        "adjusted_close": 212.2554,
        "volume": 66_213_200,
    },
    {
        "date": "2024-06-27",
        "open": 214.69,
        "high": 215.7395,
        "low": 212.35,
        "close": 214.1,
        "adjusted_close": 213.1014,
        "volume": 49_772_700,
    },
    {
        "date": "2024-06-28",
        "open": 215.77,
        "high": 216.07,
        "low": 210.3,
        "close": 210.62,
        "adjusted_close": 209.6376,
        "volume": 82_542_700,
    },
]


def _evidence(
    endpoint: str,
    payload: object,
    *,
    retrieved_at: datetime = _RETRIEVED,
) -> EODHDJsonEvidence:
    content = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return EODHDJsonEvidence(
        endpoint=endpoint,
        retrieved_at=retrieved_at,
        payload=payload,
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )


def _identity(*, listing_date: date = date(1980, 12, 12)) -> QualityCompounderSecurityIdentity:
    placeholder = _evidence("/api/id-mapping/AAPL.US", {})
    return QualityCompounderSecurityIdentity(
        symbol="AAPL",
        security_id="figi:BBG000B9XRY4",
        issuer_id="cik:0000320193",
        name="Apple Inc.",
        exchange="NASDAQ",
        sector="Technology",
        industry="Consumer Electronics",
        listing_date=listing_date,
        source=QualityCompounderIdentityEvidence(mapping=placeholder, general=placeholder),
    )


def _sessions(days: tuple[date, ...] | None = None) -> tuple[tuple[datetime, datetime], ...]:
    resolved = days or (
        date(2024, 6, 26),
        date(2024, 6, 27),
        date(2024, 6, 28),
        date(2024, 7, 1),
    )
    return tuple(
        (
            datetime(day.year, day.month, day.day, 13, 30, tzinfo=UTC),
            datetime(day.year, day.month, day.day, 20, 0, tzinfo=UTC),
        )
        for day in resolved
    )


class _Client:
    def __init__(
        self,
        *,
        daily: list[dict[str, object]] | None = None,
        splits: list[dict[str, object]] | None = None,
        dividends: list[dict[str, object]] | None = None,
        retrieved_at: datetime = _RETRIEVED,
    ) -> None:
        self.daily = _AAPL_ROWS if daily is None else daily
        self.splits = [] if splits is None else splits
        self.dividends = [] if dividends is None else dividends
        self.retrieved_at = retrieved_at
        self.calls: list[tuple[str, str, date, date]] = []

    def fetch_daily_bar_evidence(
        self,
        *,
        product_id: str,
        start: date,
        end: date,
    ) -> EODHDJsonEvidence:
        self.calls.append(("daily", product_id, start, end))
        return _evidence("/api/eod/AAPL.US", self.daily, retrieved_at=self.retrieved_at)

    def fetch_split_evidence(
        self,
        *,
        product_id: str,
        start: date,
        end: date,
    ) -> EODHDJsonEvidence:
        self.calls.append(("split", product_id, start, end))
        return _evidence("/api/splits/AAPL.US", self.splits, retrieved_at=self.retrieved_at)

    def fetch_dividend_evidence(
        self,
        *,
        product_id: str,
        start: date,
        end: date,
    ) -> EODHDJsonEvidence:
        self.calls.append(("dividend", product_id, start, end))
        return _evidence("/api/div/AAPL.US", self.dividends, retrieved_at=self.retrieved_at)


def _acquire(
    client: _Client | None = None,
    *,
    identity: QualityCompounderSecurityIdentity | None = None,
):
    return acquire_quality_compounder_market_series(
        client=client or _Client(),
        identity=identity or _identity(),
        instrument_id=1,
        official_sessions=_sessions(),
        decision_session=date(2024, 6, 28),
        required_history_sessions=2,
        cost_policy=DailyBarCostModelPolicy(),
        entitlement_owner_user_id=_OWNER,
    )


def _engine() -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def test_acquisition_uses_exact_window_ignores_adjusted_close_and_models_costs() -> None:
    client = _Client()
    acquired = _acquire(client)

    assert client.calls == [
        ("daily", "AAPL", date(2024, 6, 26), date(2024, 6, 28)),
        ("split", "AAPL", date(2024, 6, 26), date(2024, 6, 28)),
        ("dividend", "AAPL", date(2024, 6, 26), date(2024, 6, 28)),
    ]
    assert acquired.decision_close == _CLOSE
    assert acquired.next_open == _NEXT_OPEN
    assert [item.session_date for item in acquired.records] == [
        date(2024, 6, 27),
        date(2024, 6, 28),
    ]
    latest = acquired.records[-1]
    assert latest.raw_close == Decimal("210.62")
    assert latest.total_return_close == Decimal("210.62")
    assert latest.total_return_close != Decimal(str(_AAPL_ROWS[-1]["adjusted_close"]))
    assert latest.cost is not None
    assert latest.cost.cost_context_sha256 == DailyBarCostModelPolicy().configuration_sha256
    assert len(acquired.submissions) == 7


@pytest.mark.parametrize("retrieved_at", [_CLOSE, _NEXT_OPEN])
def test_acquisition_rejects_evidence_on_window_boundaries(retrieved_at: datetime) -> None:
    with pytest.raises(QualityCompounderMarketError, match="strictly between"):
        _acquire(_Client(retrieved_at=retrieved_at))


def test_history_allows_only_exact_prelisting_prefix() -> None:
    listed_on_decision = _identity(listing_date=date(2024, 6, 28))
    acquired = _acquire(_Client(daily=[_AAPL_ROWS[-1]]), identity=listed_on_decision)

    assert [item.session_date for item in acquired.records] == [date(2024, 6, 28)]
    assert acquired.missing_prelisting_session_dates == (date(2024, 6, 27),)
    assert acquired.records[0].cost is None

    with pytest.raises(QualityCompounderMarketError, match="unexplained official sessions"):
        _acquire(
            _Client(daily=[_AAPL_ROWS[-1]]),
            identity=replace(listed_on_decision, listing_date=date(2024, 6, 27)),
        )


def test_in_house_adjustment_combines_split_and_dividend_evidence() -> None:
    days = (
        date(2020, 8, 26),
        date(2020, 8, 27),
        date(2020, 8, 28),
        date(2020, 8, 31),
        date(2020, 9, 1),
    )
    closes = (100.0, 100.0, 99.5, 25.5)
    rows = [
        {
            "date": day.isoformat(),
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "adjusted_close": close * 0.5,
            "volume": 10_000_000,
        }
        for day, close in zip(days[:-1], closes, strict=True)
    ]
    client = _Client(
        daily=rows,
        splits=[{"date": "2020-08-31", "split": "4/1"}],
        dividends=[{"date": "2020-08-28", "value": "1.00", "currency": "USD"}],
        retrieved_at=datetime(2020, 9, 1, 0, 5, tzinfo=UTC),
    )

    acquired = acquire_quality_compounder_market_series(
        client=client,
        identity=_identity(),
        instrument_id=1,
        official_sessions=_sessions(days),
        decision_session=date(2020, 8, 31),
        required_history_sessions=3,
        cost_policy=DailyBarCostModelPolicy(),
        entitlement_owner_user_id=_OWNER,
    )

    pre_actions = acquired.records[0]
    assert pre_actions.session_date == date(2020, 8, 27)
    assert pre_actions.split_adjustment_factor == Decimal("0.25")
    assert pre_actions.split_adjusted_close == Decimal("25.000")
    assert float(pre_actions.total_return_close) == pytest.approx(24.75)
    assert acquired.records[-1].split_adjusted_close == Decimal("25.5")
    assert acquired.records[-1].cost is not None


def test_generic_observations_persist_with_complete_lineage() -> None:
    acquired = _acquire()
    engine = _engine()
    with Session(engine) as session:
        session.add_all(
            (
                User(
                    user_id=_OWNER,
                    email="quality-owner@example.invalid",
                    base_ccy="USD",
                    status="active",
                ),
                Instrument(
                    instr_id=1,
                    asset_class="equity",
                    canonical="AAPL",
                    exchange="NASDAQ",
                    settlement_currency="USD",
                    is_tradable=True,
                ),
            )
        )
        session.flush()

        persisted = persist_quality_compounder_market_series(session, acquired)

        assert len(persisted) == 7
        rows = list(session.scalars(select(EquityObservation)))
        assert len(rows) == 7
        derived = list(
            session.execute(
                select(EquityObservation, EquitySourceLineage)
                .join(
                    EquitySourceLineage,
                    EquityObservation.lineage_id == EquitySourceLineage.lineage_id,
                )
                .where(EquitySourceLineage.product == "derived-daily-market-with-corporate-actions")
            )
        )
        assert len(derived) == 2
        assert {str(lineage.adjustment_policy) for _observation, lineage in derived} == {
            QUALITY_COMPOUNDER_MARKET_ADJUSTMENT_POLICY
        }
        derived_ids = tuple(str(observation.observation_id) for observation, _lineage in derived)
        field_names = set(
            session.scalars(
                select(EquityObservationValue.field_name).where(
                    EquityObservationValue.observation_id.in_(derived_ids)
                )
            )
        )
        assert "adjusted_close" not in field_names
        assert {
            "cost_context_sha256",
            "one_way_nonspread_cost_bps",
            "round_trip_spread_bps",
            "total_return_close",
        } <= field_names


def test_generic_prelisting_bundle_replays_idempotently() -> None:
    exact_binary_row = {
        "date": "2024-06-28",
        "open": 210.5,
        "high": 211.0,
        "low": 210.0,
        "close": 210.5,
        "adjusted_close": 1.0,
        "volume": 82_542_700,
    }
    acquired = _acquire(
        _Client(daily=[exact_binary_row]),
        identity=_identity(listing_date=date(2024, 6, 28)),
    )
    engine = _engine()
    with Session(engine) as session:
        session.add_all(
            (
                User(
                    user_id=_OWNER,
                    email="quality-owner@example.invalid",
                    base_ccy="USD",
                    status="active",
                ),
                Instrument(
                    instr_id=1,
                    asset_class="equity",
                    canonical="AAPL",
                    exchange="NASDAQ",
                    settlement_currency="USD",
                    is_tradable=True,
                ),
            )
        )
        session.flush()

        first = persist_quality_compounder_market_series(session, acquired)
        replay = persist_quality_compounder_market_series(session, acquired)

        assert tuple(item.observation_id for item in first) == tuple(
            item.observation_id for item in replay
        )
        assert len(list(session.scalars(select(EquityObservation)))) == 4
