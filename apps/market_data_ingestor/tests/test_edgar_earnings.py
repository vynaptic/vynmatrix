"""EDGAR earnings-loader tests: filtering, classification, pagination, idempotency."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from lib_application.db.models import Base, EarningsEvent, Instrument
from market_data_ingestor.edgar_earnings import (
    EdgarEarningsError,
    EdgarEarningsLoader,
    classify_announce_time,
    select_earnings_filings,
)

AAPL_ID = 8
GOOGL_ID = 10
GOOG_ID = 11
UA = "vynmatrix research test@example.invalid"

_TICKERS_JSON = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
    "2": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."},
}


def _columns(rows: list[tuple[str, str, str, str]]) -> dict[str, list[str]]:
    return {
        "form": [row[0] for row in rows],
        "items": [row[1] for row in rows],
        "filingDate": [row[2] for row in rows],
        "acceptanceDateTime": [row[3] for row in rows],
    }


@pytest.mark.parametrize(
    ("acceptance", "expected"),
    [
        # January (EST, UTC-5): 12:30Z = 07:30 ET -> bmo; 22:05Z = 17:05 ET -> amc.
        (datetime(2024, 1, 25, 12, 30, tzinfo=UTC), "bmo"),
        (datetime(2024, 1, 25, 22, 5, tzinfo=UTC), "amc"),
        # July (EDT, UTC-4): 17:00Z = 13:00 ET -> dmh; 20:30Z = 16:30 ET -> amc.
        (datetime(2024, 7, 25, 17, 0, tzinfo=UTC), "dmh"),
        (datetime(2024, 7, 25, 20, 30, tzinfo=UTC), "amc"),
        (datetime(2024, 7, 25, 13, 0, tzinfo=UTC), "bmo"),  # 09:00 ET
    ],
)
def test_classify_announce_time(acceptance: datetime, expected: str) -> None:
    assert classify_announce_time(acceptance) == expected


def test_select_earnings_filings_filters_forms_items_and_since() -> None:
    columns = _columns(
        [
            ("8-K", "2.02,9.01", "2024-05-02", "2024-05-02T16:31:00.000Z"),
            ("8-K", "5.02", "2024-05-03", "2024-05-03T16:31:00.000Z"),  # wrong item
            ("8-K/A", "2.02", "2024-05-02", "2024-05-02T17:00:00.000Z"),  # dupe, later
            ("10-Q", "2.02", "2024-05-04", "2024-05-04T16:31:00.000Z"),  # wrong form
            ("8-K", "2.02", "2019-01-30", "2019-01-30T16:00:00.000Z"),  # before since
            ("8-K", "12.02", "2024-05-06", "2024-05-06T16:31:00.000Z"),  # not item 2.02
        ]
    )
    events = select_earnings_filings(columns, since=date(2020, 1, 1))
    assert list(events) == [date(2024, 5, 2)]
    assert events[date(2024, 5, 2)] == datetime(2024, 5, 2, 20, 31, tzinfo=UTC)


def _mock_client(handler) -> httpx.Client:  # type: ignore[no-untyped-def]
    return httpx.Client(transport=httpx.MockTransport(handler))


def _sqlite_session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                Instrument(
                    instr_id=AAPL_ID,
                    asset_class="equity",
                    canonical="AAPL",
                    exchange="NASDAQ",
                    settlement_currency="USD",
                ),
                Instrument(
                    instr_id=GOOGL_ID,
                    asset_class="equity",
                    canonical="GOOGL",
                    exchange="NASDAQ",
                    settlement_currency="USD",
                ),
                Instrument(
                    instr_id=GOOG_ID,
                    asset_class="equity",
                    canonical="GOOG",
                    exchange="NASDAQ",
                    settlement_currency="USD",
                ),
            ]
        )
        session.commit()
    return lambda: Session(engine), engine


def test_loader_paginates_dedupes_shared_cik_and_is_idempotent() -> None:
    session_factory, engine = _sqlite_session_factory()
    submission_requests: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["User-Agent"] == UA
        path = request.url.path
        if path == "/files/company_tickers.json":
            return httpx.Response(200, json=_TICKERS_JSON)
        if path == "/submissions/CIK-older-001.json":
            submission_requests.append(path)
            return httpx.Response(
                200,
                json=_columns([("8-K", "2.02", "2021-02-02", "2021-02-02T07:30:00.000Z")]),
            )
        if path.startswith("/submissions/CIK"):
            submission_requests.append(path)
            return httpx.Response(
                200,
                json={
                    "filings": {
                        "recent": _columns(
                            [("8-K", "2.02,9.01", "2024-05-02", "2024-05-02T16:31:00.000Z")]
                        ),
                        "files": [
                            {
                                "name": "CIK-older-001.json",
                                "filingFrom": "2020-01-01",
                                "filingTo": "2021-12-31",
                            },
                            {
                                "name": "CIK-ancient-002.json",
                                "filingFrom": "2010-01-01",
                                "filingTo": "2015-12-31",  # before since: must be skipped
                            },
                        ],
                    }
                },
            )
        msg = f"unexpected request: {path}"
        raise AssertionError(msg)

    loader = EdgarEarningsLoader(session_factory, user_agent=UA, client=_mock_client(_handler))
    instruments = {"GOOGL": GOOGL_ID, "GOOG": GOOG_ID}

    first = loader.load_for_instruments(instruments, since=date(2020, 1, 1))
    second = loader.load_for_instruments(instruments, since=date(2020, 1, 1))

    # One CIK fetch chain for both share classes, repeated across the two runs.
    cik_pages = [p for p in submission_requests if p.startswith("/submissions/CIK0001652044")]
    assert len(cik_pages) == 2  # once per run, not per ticker
    assert first == {"GOOGL": 2, "GOOG": 2}
    assert second == {"GOOGL": 0, "GOOG": 0}

    with Session(engine) as session:
        rows = session.execute(select(EarningsEvent)).scalars().all()
    assert len(rows) == 4  # 2 events x 2 instruments
    by_key = {(row.instr_id, row.report_date): row for row in rows}
    amc_row = by_key[(GOOGL_ID, date(2024, 5, 2))]
    assert amc_row.announce_time == "amc"
    assert amc_row.status == "actual"
    bmo_row = by_key[(GOOG_ID, date(2021, 2, 2))]
    assert bmo_row.announce_time == "bmo"


def test_unknown_ticker_fails_loud() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_TICKERS_JSON)

    loader = EdgarEarningsLoader(lambda: None, user_agent=UA, client=_mock_client(_handler))
    with pytest.raises(EdgarEarningsError, match="NOPE"):
        loader.load_for_instruments({"NOPE": 99}, since=date(2020, 1, 1))


def test_blank_user_agent_rejected() -> None:
    with pytest.raises(ValueError, match="User-Agent"):
        EdgarEarningsLoader(lambda: None, user_agent="  ")
