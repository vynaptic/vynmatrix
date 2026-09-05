"""Validation-only EODHD historical snapshot provider tests."""

from __future__ import annotations

import hashlib
import traceback
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from dev_cli.validation.providers.eodhd import EODHDClient, EODHDMarketDataError
from lib_infrastructure.market_data import eodhd_client as shared_eodhd

_TOKEN = "test-token"
_START = datetime(2024, 7, 8, tzinfo=UTC)
_END = datetime(2024, 7, 12, tzinfo=UTC)


def _row(day: str, close: float) -> dict[str, object]:
    return {
        "date": day,
        "open": close - 1.0,
        "high": close + 1.0,
        "low": close - 2.0,
        "close": close,
        "adjusted_close": close - 50.0,
        "volume": 1_000,
    }


def _provider(handler: Any) -> EODHDClient:
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://eodhd.com",
    )
    return EODHDClient(api_token=_TOKEN, client=client)


def test_symbol_suffix_and_raw_close() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        assert request.url.params["period"] == "d"
        assert request.url.params["from"] == "2024-07-08"
        assert request.url.params["to"] == "2024-07-12"
        return httpx.Response(200, json=[_row("2024-07-10", 100.0)])

    provider = _provider(handler)
    rows = provider.fetch_candle_rows(
        product_id="AAPL",
        instr_id=8,
        start_time=_START,
        end_time=_END,
        granularity="ONE_DAY",
    )

    assert seen_paths == ["/api/eod/AAPL.US"]
    assert len(rows) == 1
    assert rows[0].close == 100.0
    assert rows[0].source == "eodhd"
    assert rows[0].timeframe == "1d"
    assert rows[0].ts == datetime(2024, 7, 10, 20, 0)
    assert rows[0].ts.tzinfo is None

    def index_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/eod/GSPC.INDX"
        return httpx.Response(200, json=[])

    index_provider = _provider(index_handler)
    assert (
        index_provider.fetch_candle_rows(
            product_id="GSPC.INDX",
            instr_id=9,
            start_time=_START,
            end_time=_END,
            granularity="ONE_DAY",
        )
        == []
    )


def test_rejects_non_daily_granularity() -> None:
    provider = EODHDClient(api_token=_TOKEN, client=httpx.Client())
    with pytest.raises(ValueError, match="supports only ONE_DAY"):
        provider.fetch_candle_rows(
            product_id="AAPL",
            instr_id=1,
            start_time=_START,
            end_time=_END,
            granularity="ONE_MINUTE",
        )


def test_non_trading_provider_row_is_excluded_from_official_axis() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                _row("2024-07-10", 100.0),
                _row("2024-07-13", 101.0),
            ],
        )

    rows = _provider(handler).fetch_candle_rows(
        product_id="AAPL",
        instr_id=8,
        start_time=_START,
        end_time=_END,
        granularity="ONE_DAY",
    )

    assert [row.ts for row in rows] == [datetime(2024, 7, 10, 20, 0)]


def test_unclosed_session_is_excluded() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                _row("2024-07-10", 100.0),
                _row("2024-07-11", 102.0),
            ],
        )

    rows = _provider(handler).fetch_candle_rows(
        product_id="AAPL",
        instr_id=8,
        start_time=_START,
        end_time=datetime(2024, 7, 11, 19, 0, tzinfo=UTC),
        granularity="ONE_DAY",
    )
    assert [row.ts for row in rows] == [datetime(2024, 7, 10, 20, 0)]


@pytest.mark.parametrize(
    "row",
    [
        {"date": "2024-07-10", "open": "not-a-number"},
        {**_row("2024-07-10", 100.0), "volume": "not-a-number"},
    ],
)
def test_malformed_daily_bar_fails_loud(row: dict[str, object]) -> None:
    provider = _provider(
        lambda _request: httpx.Response(
            200,
            json=[row],
        )
    )
    with pytest.raises(EODHDMarketDataError, match="malformed daily bar"):
        provider.fetch_candle_rows(
            product_id="AAPL",
            instr_id=8,
            start_time=_START,
            end_time=_END,
            granularity="ONE_DAY",
        )


def test_retries_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json=[_row("2024-07-10", 100.0)])

    monkeypatch.setattr(shared_eodhd.time, "sleep", lambda _delay: None)
    rows = _provider(handler).fetch_candle_rows(
        product_id="AAPL",
        instr_id=8,
        start_time=_START,
        end_time=_END,
        granularity="ONE_DAY",
    )

    assert calls == 2
    assert len(rows) == 1


def test_split_and_dividend_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/splits/AAPL.US":
            return httpx.Response(
                200,
                json=[{"date": "2020-08-31", "split": "4.000000/1.000000"}],
            )
        assert request.url.path == "/api/div/AAPL.US"
        return httpx.Response(
            200,
            json=[
                {"date": "2020-11-06", "value": 0.205, "currency": "USD"},
                {"date": "2020-08-07", "value": 0.205, "currency": "USD"},
            ],
        )

    provider = _provider(handler)
    splits = provider.fetch_splits(
        product_id="AAPL",
        start=datetime(2020, 1, 1, tzinfo=UTC).date(),
        end=datetime(2021, 1, 1, tzinfo=UTC).date(),
    )
    dividends = provider.fetch_dividends(
        product_id="AAPL",
        start=datetime(2020, 1, 1, tzinfo=UTC).date(),
        end=datetime(2021, 1, 1, tzinfo=UTC).date(),
    )

    assert len(splits) == 1
    assert splits[0].ratio == Decimal(4)
    assert splits[0].ex_date.isoformat() == "2020-08-31"
    assert [item.ex_date.isoformat() for item in dividends] == [
        "2020-08-07",
        "2020-11-06",
    ]
    assert dividends[0].amount == Decimal("0.205")
    assert dividends[0].currency == "USD"


@pytest.mark.parametrize(
    "payload",
    [
        [{"date": "2020-08-31", "split": "0/1.000000"}],
        [{"date": "2020-08-31", "split": "garbage"}],
        [{"date": "2020-08-31"}],
    ],
)
def test_malformed_splits_fail_loud(payload: list[dict[str, object]]) -> None:
    provider = _provider(lambda _request: httpx.Response(200, json=payload))
    with pytest.raises(EODHDMarketDataError):
        provider.fetch_splits(
            product_id="AAPL",
            start=datetime(2020, 1, 1, tzinfo=UTC).date(),
            end=datetime(2021, 1, 1, tzinfo=UTC).date(),
        )


def test_errors_never_leak_token() -> None:
    provider = _provider(lambda _request: httpx.Response(403, text="forbidden"))
    with pytest.raises(EODHDMarketDataError, match="HTTP 403") as exc_info:
        provider.fetch_candle_rows(
            product_id="AAPL",
            instr_id=8,
            start_time=_START,
            end_time=_END,
            granularity="ONE_DAY",
        )

    assert _TOKEN not in str(exc_info.value)
    assert _TOKEN not in repr(exc_info.value)


def test_transport_error_traceback_never_leaks_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        message = f"credential={_TOKEN}"
        raise httpx.ConnectError(message, request=request)

    monkeypatch.setattr(shared_eodhd.time, "sleep", lambda _delay: None)
    with pytest.raises(EODHDMarketDataError, match="failed after") as exc_info:
        _provider(handler).fetch_id_mapping(provider_symbol="AAA")

    rendered = "".join(
        traceback.format_exception(
            exc_info.type,
            exc_info.value,
            exc_info.tb,
        )
    )
    assert _TOKEN not in rendered


def test_security_fundamentals_general_evidence_is_exact_and_credential_free() -> None:
    retrieved_at = datetime(2026, 8, 2, 11, 12, 13, tzinfo=UTC)
    content = b'{"Code":"AAA","IPODate":"1980-12-12","IsDelisted":false}'
    observed: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append((request.url.path, dict(request.url.params)))
        return httpx.Response(
            200,
            content=content,
            headers={"content-type": "application/json"},
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://eodhd.com",
    )
    provider = EODHDClient(
        api_token=_TOKEN,
        client=client,
        clock=lambda: retrieved_at,
    )

    evidence = provider.fetch_security_fundamentals_general(provider_symbol="aaa")

    assert observed == [
        (
            "/api/v1.1/fundamentals/AAA.US",
            {"api_token": _TOKEN, "fmt": "json", "filter": "General"},
        )
    ]
    assert evidence.endpoint == ("/api/v1.1/fundamentals/AAA.US?filter=General&fmt=json")
    assert evidence.payload == {
        "Code": "AAA",
        "IPODate": "1980-12-12",
        "IsDelisted": False,
    }
    assert evidence.content == content
    assert evidence.content_sha256 == hashlib.sha256(content).hexdigest()
    assert evidence.retrieved_at == retrieved_at
    assert _TOKEN not in evidence.endpoint


@pytest.mark.parametrize(
    ("content", "expected_payload"),
    [(b"{}", {}), (b"null", None)],
)
def test_security_fundamentals_general_preserves_no_data_response(
    content: bytes,
    expected_payload: object,
) -> None:
    provider = _provider(
        lambda _request: httpx.Response(
            200,
            content=content,
            headers={"content-type": "application/json"},
        )
    )

    evidence = provider.fetch_security_fundamentals_general(provider_symbol="AAA.US")

    assert evidence.payload == expected_payload
    assert evidence.content == content
    assert evidence.content_sha256 == hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize("provider_symbol", ["", "   ", "A AA"])
def test_security_fundamentals_general_rejects_invalid_symbol(
    provider_symbol: str,
) -> None:
    provider = _provider(lambda _request: httpx.Response(200, json={}))

    with pytest.raises(ValueError, match="US security symbol"):
        provider.fetch_security_fundamentals_general(provider_symbol=provider_symbol)


def test_membership_and_identity_evidence_is_exact_and_credential_free() -> None:
    observed: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/api/fundamentals/GSPC.INDX":
            return httpx.Response(200, json={"0": {"Code": "AAA"}})
        if request.url.path == "/api/v1.1/fundamentals/GSPC.INDX":
            return httpx.Response(200, json={"HistoricalComponents": {}})
        if request.url.path == "/api/exchange-symbol-list/US":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/symbol-change-history":
            return httpx.Response(200, json=[])
        assert request.url.path == "/api/id-mapping"
        return httpx.Response(
            200,
            json={
                "meta": {"limit": 100, "offset": 0, "total": 0},
                "data": [],
                "links": {"next": None},
            },
        )

    provider = _provider(handler)
    ticker, snapshots = provider.fetch_index_membership_history(
        index_symbol="GSPC.INDX",
        start=date(2018, 1, 1),
        end=date(2025, 12, 31),
    )
    active = provider.fetch_us_symbol_directory(delisted=False)
    inactive = provider.fetch_us_symbol_directory(delisted=True)
    changes = provider.fetch_us_symbol_change_history(
        start=date(2018, 1, 1),
        end=date(2025, 12, 31),
    )
    mapping = provider.fetch_id_mapping(provider_symbol="AAA")

    assert observed[0] == (
        "/api/fundamentals/GSPC.INDX",
        {
            "api_token": _TOKEN,
            "filter": "HistoricalTickerComponents",
            "fmt": "json",
        },
    )
    assert observed[1][1]["historical"] == "1"
    assert observed[1][1]["from"] == "2018-01-01"
    assert observed[1][1]["to"] == "2025-12-31"
    assert "delisted" not in observed[2][1]
    assert observed[3][1]["delisted"] == "1"
    assert observed[4] == (
        "/api/symbol-change-history",
        {
            "api_token": _TOKEN,
            "from": "2018-01-01",
            "to": "2025-12-31",
            "ex": "US",
            "fmt": "json",
        },
    )
    assert observed[5][1]["filter[symbol]"] == "AAA.US"
    for evidence in (ticker, snapshots, active, inactive, changes, mapping):
        assert _TOKEN not in evidence.endpoint
        assert evidence.content_sha256 == hashlib.sha256(evidence.content).hexdigest()
        assert evidence.retrieved_at.tzinfo is UTC


@pytest.mark.parametrize("index_symbol", ["GSPC", " GSPC INDX ", ""])
def test_membership_rejects_non_index_symbols(index_symbol: str) -> None:
    provider = _provider(lambda _request: httpx.Response(200, json={}))
    with pytest.raises(ValueError, match=r"\.INDX"):
        provider.fetch_index_membership_history(
            index_symbol=index_symbol,
            start=date(2020, 1, 1),
            end=date(2020, 1, 2),
        )
