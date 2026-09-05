"""Scheduled-market daily backfill policy: calendar coverage, narrowed spans,
corporate actions, fail-loud paths — through HistoricalBackfiller's one surface."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from lib_application.db.models import Base, CorporateAction, Instrument
from lib_data.market_data import CandleRow, MarketDataInstrument
from lib_data.sessions import session_close_utc, sessions_between
from lib_infrastructure.market_data.eodhd_client import (
    EODHDErrorKind,
    EODHDMarketDataError,
)
from market_data_ingestor.backfill import (
    HistoricalBackfiller,
    HistoricalBackfillError,
    UnknownInstrumentError,
)

XNYS = "XNYS"
# Half-open datetime window [Jul 1, Aug 1) == inclusive session range Jul 1..Jul 31.
START = datetime(2024, 7, 1, tzinfo=UTC)
END = datetime(2024, 8, 1, tzinfo=UTC)
NOW = datetime(2024, 8, 15, tzinfo=UTC)
FIRST_SESSION = date(2024, 7, 1)
LAST_SESSION = date(2024, 7, 31)
AAPL_ID = 8


def _instrument(symbol: str) -> MarketDataInstrument:
    return MarketDataInstrument(
        canonical_symbol=symbol,
        product_id=symbol,
        asset_class="equity",
    )


def _daily_rows(
    instr_id: int,
    first: date,
    last: date,
    *,
    skip: set[date] | None = None,
) -> list[CandleRow]:
    rows = []
    for session_date in sessions_between(XNYS, first, last):
        if skip and session_date in skip:
            continue
        rows.append(
            CandleRow(
                instr_id=instr_id,
                ts=session_close_utc(XNYS, session_date),
                timeframe="1d",
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1_000_000.0,
                source="eodhd",
            )
        )
    return rows


class FakeIngestionService:
    def __init__(self, instrument_map: dict[str, int]) -> None:
        self._map = instrument_map
        self.upserted: list[CandleRow] = []

    def load_instrument_map(self, *, asset_class: str = "crypto") -> dict[str, int]:
        assert asset_class == "equity"
        return dict(self._map)

    def upsert_candles(self, rows: list[CandleRow]) -> int:
        self.upserted.extend(rows)
        return len(rows)

    def candle_count(
        self,
        _instr_id: int,
        *,
        source: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> int:
        def _aware(ts: datetime) -> datetime:
            return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts

        return sum(start <= _aware(row.ts) < end for row in self.upserted)


class FakeProvider:
    source = "eodhd"

    def __init__(self, rows_by_symbol: dict[str, list[CandleRow]]) -> None:
        self._rows = rows_by_symbol
        self.fetch_calls = 0

    def fetch_candle_rows(self, *, product_id: str, **_kwargs: Any) -> list[CandleRow]:
        self.fetch_calls += 1
        return self._rows[product_id]

    def close(self) -> None:  # pragma: no cover - interface completeness
        return


def _backfiller(
    rows_by_symbol: dict[str, list[CandleRow]],
    *,
    symbols: tuple[str, ...] = ("AAPL",),
    provider: FakeProvider | None = None,
    **kwargs: Any,
) -> HistoricalBackfiller:
    return HistoricalBackfiller(
        session_factory=lambda: None,
        instruments=[_instrument(symbol) for symbol in symbols],
        granularity="ONE_DAY",
        source="eodhd",
        provider=provider or FakeProvider(rows_by_symbol),
        ingestion_service=FakeIngestionService({"AAPL": AAPL_ID}),  # type: ignore[arg-type]
        request_pause_sec=0.0,
        sleep=lambda _s: None,
        **kwargs,
    )


def test_full_coverage_pass() -> None:
    backfiller = _backfiller({"AAPL": _daily_rows(AAPL_ID, FIRST_SESSION, LAST_SESSION)})
    result = backfiller.backfill_range(start=START, end=END, now=NOW)
    (summary,) = backfiller.scheduled_daily_summaries
    assert summary.instr_id == AAPL_ID
    assert summary.rows_upserted == summary.expected_sessions
    assert summary.narrowed is False
    assert summary.first_session == date(2024, 7, 1)
    assert summary.last_session == date(2024, 7, 31)
    assert result.products_processed == 1
    assert result.rows_upserted == summary.rows_upserted


def test_mid_gap_fails_the_coverage_gate() -> None:
    skip = {date(2024, 7, 10), date(2024, 7, 11)}
    backfiller = _backfiller({"AAPL": _daily_rows(AAPL_ID, FIRST_SESSION, LAST_SESSION, skip=skip)})
    with pytest.raises(HistoricalBackfillError, match="AAPL: coverage"):
        backfiller.backfill_range(start=START, end=END, now=NOW)


def test_late_listing_narrows_the_span_instead_of_failing() -> None:
    backfiller = _backfiller({"AAPL": _daily_rows(AAPL_ID, date(2024, 7, 15), LAST_SESSION)})
    backfiller.backfill_range(start=START, end=END, now=NOW)
    (summary,) = backfiller.scheduled_daily_summaries
    assert summary.narrowed is True
    assert summary.first_session == date(2024, 7, 15)
    assert summary.expected_sessions == len(sessions_between(XNYS, date(2024, 7, 15), LAST_SESSION))


def test_unknown_symbol_fails_loud() -> None:
    backfiller = _backfiller(
        {"AAPL": _daily_rows(AAPL_ID, FIRST_SESSION, LAST_SESSION)},
        symbols=("MSFT",),
    )
    with pytest.raises(UnknownInstrumentError, match="MSFT"):
        backfiller.backfill_range(start=START, end=END, now=NOW)


def test_empty_provider_result_fails_loud_after_a_single_request() -> None:
    provider = FakeProvider({"AAPL": []})
    backfiller = _backfiller({}, provider=provider)
    with pytest.raises(HistoricalBackfillError, match="no daily bars"):
        backfiller.backfill_range(start=START, end=END, now=NOW)
    # A clean empty daily response is classified by the calendar gate, never
    # retried or window-paged: exactly one provider request.
    assert provider.fetch_calls == 1


def test_historical_backfill_fails_closed_immediately_on_eodhd_daily_quota() -> None:
    class QuotaProvider(FakeProvider):
        def fetch_candle_rows(self, *, product_id: str, **_kwargs: Any) -> list[CandleRow]:
            self.fetch_calls += 1
            msg = f"EODHD request for {product_id}.US failed with HTTP 402"
            raise EODHDMarketDataError(
                msg,
                kind=EODHDErrorKind.DAILY_QUOTA,
                status_code=402,
                retry_at=datetime(2026, 8, 3, tzinfo=UTC),
            )

    provider = QuotaProvider({})
    backfiller = _backfiller({}, provider=provider)

    with pytest.raises(HistoricalBackfillError, match="provider contract failed") as excinfo:
        backfiller.backfill_range(start=START, end=END, now=NOW)

    assert provider.fetch_calls == 1
    assert isinstance(excinfo.value.__cause__, EODHDMarketDataError)


def test_ensure_history_fetches_once_then_skips_when_covered() -> None:
    provider = FakeProvider({"AAPL": _daily_rows(AAPL_ID, FIRST_SESSION, LAST_SESSION)})
    backfiller = _backfiller({}, provider=provider)
    now = datetime(2024, 8, 1, tzinfo=UTC)

    first = backfiller.ensure_history(days=31, now=now)

    assert provider.fetch_calls == 1
    assert first.products_processed == 1
    assert first.rows_upserted == len(sessions_between(XNYS, FIRST_SESSION, LAST_SESSION))

    second = backfiller.ensure_history(days=31, now=now)

    # Calendar-aware deficit check: full session coverage + fresh tail → no refetch.
    assert provider.fetch_calls == 1
    assert second.products_processed == 0
    assert second.rows_upserted == 0


@dataclass(frozen=True)
class _Split:
    ex_date: date
    ratio: Decimal


@dataclass(frozen=True)
class _Dividend:
    ex_date: date
    amount: Decimal
    currency: str | None


class FakeCorporateActionSource:
    source = "eodhd"

    def __init__(self, splits: list[_Split], dividends: list[_Dividend]) -> None:
        self._splits = splits
        self._dividends = dividends

    def fetch_splits(self, *, product_id: str, start: date, end: date) -> list[_Split]:
        return self._splits

    def fetch_dividends(self, *, product_id: str, start: date, end: date) -> list[_Dividend]:
        return self._dividends


class FakeActionCapableProvider(FakeProvider):
    """A provider that also serves splits/dividends (structural detection)."""

    def fetch_splits(self, *, product_id: str, start: date, end: date) -> list[_Split]:
        return []

    def fetch_dividends(self, *, product_id: str, start: date, end: date) -> list[_Dividend]:
        return []


def _sqlite_session_factory() -> tuple[Any, Any]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Instrument(
                instr_id=AAPL_ID,
                asset_class="equity",
                canonical="AAPL",
                exchange="NASDAQ",
                settlement_currency="USD",
            )
        )
        session.commit()
    return lambda: Session(engine), engine


def test_corporate_actions_upsert_is_idempotent() -> None:
    session_factory, engine = _sqlite_session_factory()
    reference = FakeCorporateActionSource(
        splits=[_Split(ex_date=date(2020, 8, 31), ratio=Decimal(4))],
        dividends=[
            _Dividend(ex_date=date(2020, 8, 7), amount=Decimal("0.205"), currency="USD"),
            _Dividend(ex_date=date(2020, 11, 6), amount=Decimal("0.205"), currency="USD"),
        ],
    )
    backfiller = HistoricalBackfiller(
        session_factory=session_factory,
        instruments=[_instrument("AAPL")],
        granularity="ONE_DAY",
        source="eodhd",
        provider=FakeProvider({}),
        ingestion_service=FakeIngestionService({"AAPL": AAPL_ID}),  # type: ignore[arg-type]
        corporate_action_source=reference,
        request_pause_sec=0.0,
    )

    first = backfiller.load_corporate_actions(start=date(2020, 1, 1), end=date(2021, 1, 1))
    second = backfiller.load_corporate_actions(start=date(2020, 1, 1), end=date(2021, 1, 1))

    assert first == {"AAPL": 3}
    assert second == {"AAPL": 0}
    with Session(engine) as session:
        rows = session.execute(select(CorporateAction)).scalars().all()
    assert len(rows) == 3
    split_rows = [row for row in rows if row.action_type == "split"]
    assert split_rows[0].ratio == Decimal(4)


def test_corporate_actions_require_a_source() -> None:
    backfiller = _backfiller({"AAPL": []})
    assert backfiller.corporate_actions_supported is False
    with pytest.raises(HistoricalBackfillError, match="corporate-action source"):
        backfiller.load_corporate_actions(start=FIRST_SESSION, end=LAST_SESSION)


def test_action_capable_provider_is_detected_structurally() -> None:
    session_factory, _engine = _sqlite_session_factory()
    backfiller = HistoricalBackfiller(
        session_factory=session_factory,
        instruments=[_instrument("AAPL")],
        granularity="ONE_DAY",
        source="eodhd",
        provider=FakeActionCapableProvider({}),
        ingestion_service=FakeIngestionService({"AAPL": AAPL_ID}),  # type: ignore[arg-type]
        request_pause_sec=0.0,
    )
    assert backfiller.corporate_actions_supported is True
    actions = backfiller.load_corporate_actions(start=FIRST_SESSION, end=LAST_SESSION)
    assert actions == {"AAPL": 0}
