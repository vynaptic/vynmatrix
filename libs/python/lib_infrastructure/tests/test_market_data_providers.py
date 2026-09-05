"""Market-data provider port + resolver + Deribit/IBKR adapters (multi-asset seam)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from lib_infrastructure.market_data import coinbase_client as coinbase_client_module
from lib_infrastructure.market_data.coinbase_client import CoinbaseCandleClient
from lib_infrastructure.market_data.delta_client import DeltaCandleClient
from lib_infrastructure.market_data.deribit_client import DeribitCandleClient
from lib_infrastructure.market_data.eodhd_client import (
    EODHDClient,
    EODHDErrorKind,
    EODHDMarketDataError,
)
from lib_infrastructure.market_data.ibkr_client import IBKRCandleClient, IBKRGatewayError
from lib_infrastructure.market_data.ports import IMarketDataProvider
from lib_infrastructure.market_data.providers import get_market_data_provider
from lib_infrastructure.market_data.saxo_client import (
    SaxoCandleClient,
    SaxoChartShapeError,
    SaxoDelayedMarketDataError,
    SaxoTokenExpiredError,
)
from lib_infrastructure.market_data.zerodha_client import ZerodhaCandleClient

_START = datetime(2026, 6, 1, tzinfo=UTC)
_END = datetime(2026, 6, 1, 0, 3, tzinfo=UTC)
_SAXO_EXPIRY = datetime(2026, 6, 2, tzinfo=UTC)


def test_resolver_dispatch() -> None:
    assert isinstance(get_market_data_provider("coinbase_live"), CoinbaseCandleClient)
    assert isinstance(get_market_data_provider("deribit"), DeribitCandleClient)
    deribit_testnet = get_market_data_provider("deribit_testnet")
    assert isinstance(deribit_testnet, DeribitCandleClient)
    assert deribit_testnet.source == "deribit_testnet"
    assert isinstance(get_market_data_provider("delta"), DeltaCandleClient)
    delta_india = get_market_data_provider("delta_india")
    assert isinstance(delta_india, DeltaCandleClient)
    assert delta_india.source == "delta_india"
    assert isinstance(get_market_data_provider("ibkr"), IBKRCandleClient)
    assert isinstance(
        get_market_data_provider(
            "saxo_live",
            access_token="oauth-token",
            access_token_expires_at=_SAXO_EXPIRY.isoformat(),
        ),
        SaxoCandleClient,
    )
    saxo_simulation = get_market_data_provider(
        "saxo_simulation",
        access_token="oauth-token",
        access_token_expires_at=_SAXO_EXPIRY.isoformat(),
    )
    assert isinstance(saxo_simulation, SaxoCandleClient)
    assert saxo_simulation.source == "saxo_simulation"
    assert isinstance(
        get_market_data_provider(
            "zerodha",
            api_key="kite-key",
            access_token="daily-token",
        ),
        ZerodhaCandleClient,
    )


@pytest.mark.parametrize(
    "source",
    [
        "bogus_venue",
        "coinbase",
        "deribit_live",
        "interactive_brokers",
        "saxo",
        "COINBASE_LIVE",
    ],
)
def test_resolver_rejects_unknown_or_noncanonical_source(source: str) -> None:
    with pytest.raises(ValueError, match="Unknown market-data source"):
        get_market_data_provider(source)


def test_providers_conform_to_port() -> None:
    assert isinstance(get_market_data_provider("coinbase_live"), IMarketDataProvider)
    assert isinstance(get_market_data_provider("deribit"), IMarketDataProvider)
    assert isinstance(get_market_data_provider("delta"), IMarketDataProvider)
    assert isinstance(get_market_data_provider("ibkr"), IMarketDataProvider)
    assert isinstance(
        get_market_data_provider(
            "saxo_live",
            access_token="oauth-token",
            access_token_expires_at=_SAXO_EXPIRY.isoformat(),
        ),
        IMarketDataProvider,
    )
    assert isinstance(
        get_market_data_provider(
            "zerodha",
            api_key="kite-key",
            access_token="daily-token",
        ),
        IMarketDataProvider,
    )
    assert CoinbaseCandleClient.source == "coinbase_live"
    assert DeribitCandleClient.source == "deribit"
    assert DeltaCandleClient.source == "delta"
    assert IBKRCandleClient.source == "ibkr"
    assert ZerodhaCandleClient.source == "zerodha"


def test_coinbase_uses_public_candle_endpoint_without_credentials() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/brokerage/market/products/BTC-USDC/candles"
        assert request.url.params["granularity"] == "ONE_MINUTE"
        assert request.url.params["limit"] == "350"
        assert "Authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "candles": [
                    {
                        "start": str(int(_START.timestamp())),
                        "low": "99",
                        "high": "102",
                        "open": "100",
                        "close": "101",
                        "volume": "5",
                    }
                ]
            },
        )

    client = httpx.Client(
        transport=httpx.MockTransport(_handler), base_url="https://api.coinbase.com"
    )
    provider = CoinbaseCandleClient(client=client)
    rows = provider.fetch_candle_rows(
        product_id="BTC-USDC",
        instr_id=1,
        start_time=_START,
        end_time=_END,
        granularity="ONE_MINUTE",
    )
    assert len(rows) == 1
    assert rows[0].close == 101.0


@pytest.mark.parametrize(
    ("api_key", "api_secret"),
    [("key", None), (None, "secret")],
)
def test_coinbase_rejects_partial_credentials(api_key: str | None, api_secret: str | None) -> None:
    with pytest.raises(ValueError, match="both api_key and api_secret"):
        CoinbaseCandleClient(api_key=api_key, api_secret=api_secret)


def test_coinbase_rejects_unknown_granularity_before_provider_request() -> None:
    def _unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("unsupported granularity must not reach Coinbase")

    provider = CoinbaseCandleClient(
        client=httpx.Client(
            transport=httpx.MockTransport(_unexpected_request),
            base_url="https://api.coinbase.com",
        )
    )

    with pytest.raises(ValueError, match="Unsupported Coinbase granularity"):
        provider.fetch_candle_rows(
            product_id="BTC-USDC",
            instr_id=1,
            start_time=_START,
            end_time=_END,
            granularity="ONE_WEEK",
        )


def test_coinbase_uses_authenticated_endpoint_when_credentials_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coinbase_client_module,
        "build_coinbase_auth_headers",
        lambda **_kwargs: {"Authorization": "Bearer test"},
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/brokerage/products/BTC-USDC/candles"
        assert request.headers["Authorization"] == "Bearer test"
        return httpx.Response(200, json={"candles": []})

    client = httpx.Client(
        transport=httpx.MockTransport(_handler), base_url="https://api.coinbase.com"
    )
    provider = CoinbaseCandleClient(api_key="key", api_secret="secret", client=client)
    assert provider.fetch_candles(product_id="BTC-USDC", start_time=_START, end_time=_END) == []


def test_coinbase_normalizer_rejects_missing_volume_and_preserves_zero_volume() -> None:
    candles = [
        {
            "start": str(int(_START.timestamp())),
            "low": "99",
            "high": "102",
            "open": "100",
            "close": "101",
        },
        {
            "start": str(int((_START.replace(minute=1)).timestamp())),
            "low": "99",
            "high": "102",
            "open": "100",
            "close": "101",
            "volume": "0",
        },
    ]

    rows = CoinbaseCandleClient.normalize_candles(
        instr_id=1,
        product_id="BTC-USDC",
        candles=candles,
        timeframe="1m",
    )

    assert len(rows) == 1
    assert rows[0].ts == datetime(2026, 6, 1, 0, 1)
    assert rows[0].volume == 0.0


@pytest.mark.parametrize("payload", [{}, {"candles": {}}, []])
def test_coinbase_rejects_malformed_candle_contract(payload: object) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
        base_url="https://api.coinbase.com",
    )
    provider = CoinbaseCandleClient(client=client)
    with pytest.raises(RuntimeError, match="Coinbase candle response"):
        provider.fetch_candles(product_id="BTC-USDC", start_time=_START, end_time=_END)


_AUTHENTICATED_TICKLE = {"iserver": {"authStatus": {"authenticated": True}}}


def test_ibkr_normalizes_history_via_client_portal() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        if "tickle" in request.url.path:
            return httpx.Response(200, json=_AUTHENTICATED_TICKLE)
        if "marketdata/history" in request.url.path:
            assert request.url.params["conid"] == "265598"
            assert request.url.params["period"] == "3min"
            assert request.url.params["startTime"] == "20260601-00:00:00"
            assert request.url.params["direction"] == "1"
            assert request.url.params["source"] == "Last"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "t": int(_START.timestamp() * 1000),
                            "o": 100.0,
                            "h": 102.0,
                            "l": 99.0,
                            "c": 101.0,
                        },
                        {
                            "t": int((_START + timedelta(minutes=1)).timestamp() * 1000),
                            "o": 101.0,
                            "h": 103.0,
                            "l": 100.0,
                            "c": 102.5,
                            "v": 0.0,
                        },
                    ]
                },
            )
        return httpx.Response(404)

    client = httpx.Client(
        transport=httpx.MockTransport(_handler), base_url="https://localhost:5000"
    )
    provider = IBKRCandleClient(client=client)
    rows = provider.fetch_candle_rows(
        product_id="265598",
        instr_id=8,
        start_time=_START,
        end_time=_END,
        granularity="ONE_MINUTE",
    )
    assert len(rows) == 2
    assert rows[0].instr_id == 8
    assert rows[0].source == "ibkr"
    assert rows[0].timeframe == "1m"
    assert rows[0].volume is None
    assert rows[1].volume == 0.0
    assert rows[1].close == 102.5


def test_ibkr_requires_explicit_numeric_conid_before_request() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        msg = f"ambiguous symbol must not reach IBKR: {request.url}"
        raise AssertionError(msg)

    client = httpx.Client(
        transport=httpx.MockTransport(_handler), base_url="https://localhost:5000"
    )
    provider = IBKRCandleClient(client=client)
    with pytest.raises(ValueError, match="numeric conid"):
        provider.fetch_candle_rows(
            product_id="AAPL",
            instr_id=1,
            start_time=_START,
            end_time=_END,
        )


def test_ibkr_remote_gateway_requires_pinned_ca_certificate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IBKR_CA_CERT", raising=False)

    with pytest.raises(ValueError, match="requires IBKR_CA_CERT"):
        IBKRCandleClient(gateway_url="https://gateway.internal:5000")


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (timedelta(minutes=30), "30min"),
        (timedelta(minutes=31), "1h"),
        (timedelta(hours=8), "8h"),
        (timedelta(hours=8, seconds=1), "1d"),
        (timedelta(days=350), "350d"),
    ],
)
def test_ibkr_history_period_uses_supported_units(
    duration: timedelta,
    expected: str,
) -> None:
    assert IBKRCandleClient._period_for(_START, _START + duration) == expected


def test_ibkr_unauthenticated_session_fails_loud() -> None:
    """A tickle reporting authenticated=false must fail loud, not return empty."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if "tickle" in request.url.path:
            return httpx.Response(200, json={"iserver": {"authStatus": {"authenticated": False}}})
        return httpx.Response(200, json=[{"conid": 1, "symbol": "AAPL"}])

    client = httpx.Client(
        transport=httpx.MockTransport(_handler), base_url="https://localhost:5000"
    )
    provider = IBKRCandleClient(client=client)
    with pytest.raises(IBKRGatewayError, match="not authenticated"):
        provider.fetch_candle_rows(
            product_id="265598",
            instr_id=1,
            start_time=_START,
            end_time=_END,
        )


def test_ibkr_expired_session_401_fails_loud() -> None:
    """A 401 from tickle (expired session) must fail loud, not return empty."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if "tickle" in request.url.path:
            return httpx.Response(401)
        return httpx.Response(200, json=[{"conid": 1, "symbol": "AAPL"}])

    client = httpx.Client(
        transport=httpx.MockTransport(_handler), base_url="https://localhost:5000"
    )
    provider = IBKRCandleClient(client=client)
    with pytest.raises(IBKRGatewayError, match="not authenticated"):
        provider.fetch_candle_rows(
            product_id="265598",
            instr_id=1,
            start_time=_START,
            end_time=_END,
        )


def test_ibkr_unreachable_gateway_fails_loud() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(
        transport=httpx.MockTransport(_handler), base_url="https://localhost:5000"
    )
    provider = IBKRCandleClient(client=client)
    with pytest.raises(IBKRGatewayError):
        provider.fetch_candle_rows(
            product_id="265598",
            instr_id=1,
            start_time=_START,
            end_time=_END,
        )


def test_deribit_normalizes_columnar_response() -> None:
    columnar = {
        "result": {
            "status": "ok",
            "ticks": [1_780_000_000_000, 1_780_000_060_000],
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.5],
            "close": [101.0, 102.5],
            "volume": [5.0, 6.0],
        }
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        assert "get_tradingview_chart_data" in request.url.path
        assert request.url.params["instrument_name"] == "BTC-PERPETUAL"
        assert request.url.params["resolution"] == "1"
        return httpx.Response(200, json=columnar)

    client = httpx.Client(
        transport=httpx.MockTransport(_handler), base_url="https://www.deribit.com"
    )
    provider = DeribitCandleClient(client=client)
    rows = provider.fetch_candle_rows(
        product_id="BTC-PERPETUAL",
        instr_id=7,
        start_time=_START,
        end_time=_END,
        granularity="ONE_MINUTE",
    )

    assert len(rows) == 2
    assert rows[0].instr_id == 7
    assert rows[0].source == "deribit"
    assert rows[0].timeframe == "1m"
    assert rows[0].close == 101.0
    assert rows[1].close == 102.5
    assert rows[0].ts < rows[1].ts  # ascending


def test_deribit_empty_on_non_ok_status() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"status": "no_data", "ticks": []}})

    client = httpx.Client(
        transport=httpx.MockTransport(_handler), base_url="https://www.deribit.com"
    )
    provider = DeribitCandleClient(client=client)
    rows = provider.fetch_candle_rows(
        product_id="BTC-PERPETUAL", instr_id=1, start_time=_START, end_time=_END
    )
    assert rows == []


# ---------------------------------------------------------------------------
# EODHD daily equities client. Reference:
# strategies/indicator/USQualityCompounder/README.md, "Data and Failure Rules"
# ---------------------------------------------------------------------------

_EODHD_TOKEN = "sekrit-token"
# 2024-07-10/11 are regular XNYS sessions (EDT: close 20:00 UTC); 2024-07-13 is a Saturday.
_EOD_START = datetime(2024, 7, 8, tzinfo=UTC)
_EOD_END = datetime(2024, 7, 12, tzinfo=UTC)


class _MonotonicTestClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def _eodhd_row(day: str, close: float) -> dict[str, object]:
    return {
        "date": day,
        "open": close - 1.0,
        "high": close + 1.0,
        "low": close - 2.0,
        "close": close,
        "adjusted_close": close - 50.0,  # deliberately different from raw close
        "volume": 1000,
    }


def _eodhd_provider(handler) -> EODHDClient:  # type: ignore[no-untyped-def]
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://eodhd.com")
    return EODHDClient(
        api_token=_EODHD_TOKEN,
        client=client,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
        random_uniform=lambda _minimum, _maximum: 0.0,
    )


def test_resolver_dispatch_eodhd_requires_token() -> None:
    provider = get_market_data_provider("eodhd", api_key=_EODHD_TOKEN)
    assert isinstance(provider, EODHDClient)
    assert isinstance(provider, IMarketDataProvider)
    assert EODHDClient.source == "eodhd"
    with pytest.raises(ValueError, match="EODHD requires an API token"):
        get_market_data_provider("eodhd")


def test_eodhd_symbol_suffix_and_raw_close() -> None:
    seen_paths: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        assert request.url.params["period"] == "d"
        assert request.url.params["from"] == "2024-07-08"
        assert request.url.params["to"] == "2024-07-12"
        return httpx.Response(200, json=[_eodhd_row("2024-07-10", 100.0)])

    provider = _eodhd_provider(_handler)
    rows = provider.fetch_candle_rows(
        product_id="AAPL",
        instr_id=8,
        start_time=_EOD_START,
        end_time=_EOD_END,
        granularity="ONE_DAY",
    )
    assert seen_paths == ["/api/eod/AAPL.US"]
    assert len(rows) == 1
    assert rows[0].close == 100.0  # raw close, never adjusted_close
    assert rows[0].source == "eodhd"
    assert rows[0].timeframe == "1d"
    assert rows[0].ts == datetime(2024, 7, 10, 20, 0)  # XNYS close, naive UTC (EDT)
    assert rows[0].ts.tzinfo is None

    def _index_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/eod/GSPC.INDX"
        return httpx.Response(200, json=[])

    index_provider = _eodhd_provider(_index_handler)
    assert (
        index_provider.fetch_candle_rows(
            product_id="GSPC.INDX",
            instr_id=9,
            start_time=_EOD_START,
            end_time=_EOD_END,
            granularity="ONE_DAY",
        )
        == []
    )


def test_eodhd_rejects_non_daily_granularity() -> None:
    provider = EODHDClient(api_token=_EODHD_TOKEN, client=httpx.Client())
    with pytest.raises(ValueError, match="supports only ONE_DAY"):
        provider.fetch_candle_rows(
            product_id="AAPL",
            instr_id=1,
            start_time=_EOD_START,
            end_time=_EOD_END,
            granularity="ONE_MINUTE",
        )


def test_eodhd_skips_non_trading_days_and_unclosed_sessions() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                _eodhd_row("2024-07-10", 100.0),
                _eodhd_row("2024-07-13", 101.0),  # Saturday: must be skipped
                _eodhd_row("2024-07-11", 102.0),
            ],
        )

    provider = _eodhd_provider(_handler)
    rows = provider.fetch_candle_rows(
        product_id="AAPL",
        instr_id=8,
        start_time=_EOD_START,
        # Bound before the 2024-07-11 session close (20:00 UTC): that bar is
        # not yet closed relative to the bound and must be excluded.
        end_time=datetime(2024, 7, 11, 19, 0, tzinfo=UTC),
        granularity="ONE_DAY",
    )
    assert [row.ts for row in rows] == [datetime(2024, 7, 10, 20, 0)]


def test_eodhd_retries_429_then_succeeds() -> None:
    calls = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json=[_eodhd_row("2024-07-10", 100.0)])

    provider = _eodhd_provider(_handler)
    rows = provider.fetch_candle_rows(
        product_id="AAPL",
        instr_id=8,
        start_time=_EOD_START,
        end_time=_EOD_END,
        granularity="ONE_DAY",
    )
    assert calls["n"] == 2
    assert len(rows) == 1


def test_eodhd_honors_retry_after_and_server_rate_limit_without_token_leak() -> None:
    calls = {"n": 0}
    timer = _MonotonicTestClock()

    def _handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(
            200,
            json=[_eodhd_row("2024-07-10", 100.0)],
            headers={"X-RateLimit-Limit": "60"},
        )

    provider = EODHDClient(
        api_token=_EODHD_TOKEN,
        client=httpx.Client(
            transport=httpx.MockTransport(_handler),
            base_url="https://eodhd.com",
        ),
        sleep=timer.sleep,
        monotonic=timer.monotonic,
        random_uniform=lambda _minimum, _maximum: 0.0,
    )
    rows = provider.fetch_candle_rows(
        product_id="AAPL",
        instr_id=8,
        start_time=_EOD_START,
        end_time=_EOD_END,
        granularity="ONE_DAY",
    )
    provider.fetch_candle_rows(
        product_id="AAPL",
        instr_id=8,
        start_time=_EOD_START,
        end_time=_EOD_END,
        granularity="ONE_DAY",
    )

    assert len(rows) == 1
    assert calls["n"] == 3
    assert timer.sleeps[0] == pytest.approx(2.0)
    assert timer.sleeps[-1] == pytest.approx(60.0 / 54.0)


def test_eodhd_long_retry_after_returns_bounded_rate_limit_error() -> None:
    observed_at = datetime(2026, 8, 2, 20, 0, tzinfo=UTC)
    calls = {"n": 0}

    def _handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "120"})

    provider = EODHDClient(
        api_token=_EODHD_TOKEN,
        client=httpx.Client(
            transport=httpx.MockTransport(_handler),
            base_url="https://eodhd.com",
        ),
        clock=lambda: observed_at,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )

    with pytest.raises(EODHDMarketDataError, match="beyond inline retry") as excinfo:
        provider.fetch_candle_rows(
            product_id="AAPL",
            instr_id=8,
            start_time=_EOD_START,
            end_time=_EOD_END,
            granularity="ONE_DAY",
        )

    assert calls["n"] == 1
    assert excinfo.value.kind is EODHDErrorKind.RATE_LIMIT
    assert excinfo.value.retry_at == observed_at + timedelta(seconds=120)
    assert _EODHD_TOKEN not in str(excinfo.value)


def test_eodhd_402_is_daily_quota_with_next_midnight_utc_retry() -> None:
    observed_at = datetime(2026, 8, 2, 23, 58, tzinfo=UTC)
    response_marker = "provider-response-must-not-be-retained"
    provider = EODHDClient(
        api_token=_EODHD_TOKEN,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(402, text=response_marker)
            ),
            base_url="https://eodhd.com",
        ),
        clock=lambda: observed_at,
    )

    with pytest.raises(EODHDMarketDataError, match="HTTP 402") as excinfo:
        provider.fetch_candle_rows(
            product_id="AAPL",
            instr_id=8,
            start_time=_EOD_START,
            end_time=_EOD_END,
            granularity="ONE_DAY",
        )

    error = excinfo.value
    assert error.kind is EODHDErrorKind.DAILY_QUOTA
    assert error.status_code == 402
    assert error.retry_at == datetime(2026, 8, 3, tzinfo=UTC)
    assert response_marker not in str(error)
    assert _EODHD_TOKEN not in str(error)


@pytest.mark.parametrize(
    ("status_code", "expected_kind"),
    [
        (401, EODHDErrorKind.AUTHENTICATION),
        (403, EODHDErrorKind.ENTITLEMENT),
    ],
)
def test_eodhd_auth_and_entitlement_are_not_daily_quota(
    status_code: int,
    expected_kind: EODHDErrorKind,
) -> None:
    provider = _eodhd_provider(
        lambda _request: httpx.Response(status_code, text="non-quota failure")
    )

    with pytest.raises(EODHDMarketDataError) as excinfo:
        provider.fetch_candle_rows(
            product_id="AAPL",
            instr_id=8,
            start_time=_EOD_START,
            end_time=_EOD_END,
            granularity="ONE_DAY",
        )

    assert excinfo.value.kind is expected_kind
    assert excinfo.value.status_code == status_code
    assert excinfo.value.retry_at is None


def test_eodhd_parses_us_extended_delayed_quote_event_times() -> None:
    retrieved_at = datetime(2026, 7, 15, 21, 10, tzinfo=UTC)

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/us-quote-delayed"
        assert request.url.params["s"] == "AAPL.US"
        return httpx.Response(
            200,
            json={
                "meta": {"count": 1},
                "data": {
                    "AAPL.US": {
                        "symbol": "AAPL.US",
                        "exchange": "XNAS",
                        "currency": "USD",
                        "lastTradePrice": 327.5,
                        "lastTradeTime": 1784147349117,
                        "size": 48,
                        "bidPrice": 327.43,
                        "bidSize": 520,
                        "bidTime": 1784147337000,
                        "askPrice": 327.5,
                        "askSize": 320,
                        "askTime": 1784147349000,
                        "timestamp": 1784161740,
                    }
                },
                "links": {"next": None},
            },
        )

    provider = EODHDClient(
        api_token=_EODHD_TOKEN,
        client=httpx.Client(
            transport=httpx.MockTransport(_handler),
            base_url="https://eodhd.com",
        ),
        clock=lambda: retrieved_at,
    )

    batch = provider.fetch_us_extended_delayed_quotes(product_ids=("AAPL",))

    assert batch.retrieved_at == retrieved_at
    assert len(batch.raw_response_sha256) == 64
    assert len(batch.quotes) == 1
    quote = batch.quotes[0]
    assert quote.symbol == "AAPL.US"
    assert str(quote.last_trade_price) == "327.5"
    assert quote.last_trade_at == datetime.fromtimestamp(1784147349.117, tz=UTC)
    assert quote.bid_at == datetime.fromtimestamp(1784147337, tz=UTC)
    assert quote.ask_at == datetime.fromtimestamp(1784147349, tz=UTC)
    assert quote.snapshot_at == datetime.fromtimestamp(1784161740, tz=UTC)
    assert len(quote.content_sha256) == 64


def test_eodhd_delayed_quote_fails_closed_on_partial_or_malformed_bbo() -> None:
    payload = {
        "meta": {"count": 1},
        "data": {
            "AAPL.US": {
                "symbol": "AAPL.US",
                "exchange": "XNAS",
                "currency": "USD",
                "lastTradePrice": 100,
                "lastTradeTime": 1784147349117,
                "bidPrice": 99,
                "bidTime": 1784147337000,
                "timestamp": 1784161740,
            }
        },
        "links": {"next": None},
    }
    provider = _eodhd_provider(lambda _request: httpx.Response(200, json=payload))

    with pytest.raises(EODHDMarketDataError, match="malformed delayed quote"):
        provider.fetch_us_extended_delayed_quotes(product_ids=("AAPL",))

    with pytest.raises(EODHDMarketDataError, match="exactly match"):
        provider.fetch_us_extended_delayed_quotes(product_ids=("AAPL", "MSFT"))


def test_eodhd_delayed_quote_preserves_exact_json_decimal_price() -> None:
    content = b"""{
      "meta":{"count":1},
      "data":{"AAPL.US":{
        "symbol":"AAPL.US","exchange":"XNAS","currency":"USD",
        "lastTradePrice":123.123456789012345678,
        "lastTradeTime":1784147349117,
        "bidPrice":123.1234,"bidSize":10,"bidTime":1784147348000,
        "askPrice":123.1235,"askSize":20,"askTime":1784147349000,
        "timestamp":1784161740
      }},
      "links":{"next":null}
    }"""
    provider = _eodhd_provider(
        lambda _request: httpx.Response(
            200,
            content=content,
            headers={"content-type": "application/json"},
        )
    )

    quote = provider.fetch_us_extended_delayed_quotes(product_ids=("AAPL",)).quotes[0]

    assert quote.last_trade_price == Decimal("123.123456789012345678")


def test_eodhd_delayed_quote_error_never_leaks_token() -> None:
    provider = _eodhd_provider(lambda _request: httpx.Response(403, text="forbidden"))

    with pytest.raises(EODHDMarketDataError, match="HTTP 403") as excinfo:
        provider.fetch_us_extended_delayed_quotes(product_ids=("AAPL",))

    assert _EODHD_TOKEN not in str(excinfo.value)
    assert _EODHD_TOKEN not in repr(excinfo.value)


def test_eodhd_split_and_dividend_parsing() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/splits/AAPL.US":
            return httpx.Response(200, json=[{"date": "2020-08-31", "split": "4.000000/1.000000"}])
        assert request.url.path == "/api/div/AAPL.US"
        return httpx.Response(
            200,
            json=[
                {"date": "2020-11-06", "value": 0.205, "currency": "USD"},
                {"date": "2020-08-07", "value": 0.205, "currency": "USD"},
            ],
        )

    provider = _eodhd_provider(_handler)
    splits = provider.fetch_splits(
        product_id="AAPL",
        start=datetime(2020, 1, 1, tzinfo=UTC).date(),
        end=datetime(2021, 1, 1, tzinfo=UTC).date(),
    )
    assert len(splits) == 1
    assert splits[0].ratio == Decimal(4)
    assert splits[0].ex_date.isoformat() == "2020-08-31"

    dividends = provider.fetch_dividends(
        product_id="AAPL",
        start=datetime(2020, 1, 1, tzinfo=UTC).date(),
        end=datetime(2021, 1, 1, tzinfo=UTC).date(),
    )
    assert [d.ex_date.isoformat() for d in dividends] == ["2020-08-07", "2020-11-06"]  # sorted
    assert dividends[0].amount == Decimal("0.205")
    assert dividends[0].currency == "USD"


@pytest.mark.parametrize(
    "payload",
    [
        [{"date": "2020-08-31", "split": "0/1.000000"}],  # non-positive ratio
        [{"date": "2020-08-31", "split": "garbage"}],
        [{"date": "2020-08-31"}],  # missing split field
    ],
)
def test_eodhd_malformed_splits_fail_loud(payload: list) -> None:
    provider = _eodhd_provider(lambda _request: httpx.Response(200, json=payload))
    with pytest.raises(EODHDMarketDataError):
        provider.fetch_splits(
            product_id="AAPL",
            start=datetime(2020, 1, 1, tzinfo=UTC).date(),
            end=datetime(2021, 1, 1, tzinfo=UTC).date(),
        )


def test_eodhd_errors_never_leak_the_token() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    provider = _eodhd_provider(_handler)
    with pytest.raises(EODHDMarketDataError, match="HTTP 403") as excinfo:
        provider.fetch_candle_rows(
            product_id="AAPL",
            instr_id=8,
            start_time=_EOD_START,
            end_time=_EOD_END,
            granularity="ONE_DAY",
        )
    assert _EODHD_TOKEN not in str(excinfo.value)
    assert _EODHD_TOKEN not in repr(excinfo.value)


def test_delta_normalizes_public_history() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/history/candles"
        assert request.url.params["resolution"] == "1m"
        assert request.url.params["symbol"] == "BTCUSD"
        assert request.url.params["start"] == str(int(_START.timestamp()))
        assert request.url.params["end"] == str(int(_END.timestamp()))
        assert "Authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": [
                    {
                        "time": int((_START.replace(minute=1)).timestamp()),
                        "open": 101,
                        "high": 103,
                        "low": 100,
                        "close": 102,
                        "volume": 6,
                    },
                    {
                        "time": int(_START.timestamp()),
                        "open": 100,
                        "high": 102,
                        "low": 99,
                        "close": 101,
                        "volume": 5,
                    },
                ],
            },
        )

    provider = DeltaCandleClient(
        client=httpx.Client(
            transport=httpx.MockTransport(_handler),
            base_url="https://api.delta.exchange",
        )
    )
    rows = provider.fetch_candle_rows(
        product_id="BTCUSD",
        instr_id=21,
        start_time=_START,
        end_time=_END,
        granularity="ONE_MINUTE",
    )

    assert len(rows) == 2
    assert rows[0].instr_id == 21
    assert rows[0].source == "delta"
    assert rows[0].timeframe == "1m"
    assert rows[0].close == 101.0
    assert rows[0].ts < rows[1].ts


def test_eodhd_current_index_components_evidence_is_exact_and_credential_free() -> None:
    content = b'{"0":{"Code":"AAPL","Exchange":"US","Name":"Apple Inc.","Sector":"Technology","Industry":"Consumer Electronics","Weight":0.071}}'

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1.1/fundamentals/GSPC.INDX"
        assert request.url.params["filter"] == "Components"
        assert request.url.params["fmt"] == "json"
        assert request.url.params["api_token"] == "licensed-token"
        return httpx.Response(
            200,
            content=content,
            headers={"content-type": "application/json"},
        )

    provider = EODHDClient(
        "licensed-token",
        client=httpx.Client(
            transport=httpx.MockTransport(_handler),
            base_url="https://eodhd.com",
        ),
        clock=lambda: _END,
    )

    evidence = provider.fetch_current_index_components_evidence(index_symbol="GSPC.INDX")

    assert evidence.content == content
    assert evidence.content_sha256 == hashlib.sha256(content).hexdigest()
    assert "licensed-token" not in evidence.endpoint
    assert evidence.retrieved_at == _END


def test_delta_india_keeps_distinct_provenance() -> None:
    provider = DeltaCandleClient(
        india=True,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"success": True, "result": []})
            ),
            base_url="https://api.india.delta.exchange",
        ),
    )
    rows = provider.fetch_candle_rows(
        product_id="BTCUSD",
        instr_id=22,
        start_time=_START,
        end_time=_END,
    )
    assert rows == []
    assert provider.source == "delta_india"


@pytest.mark.parametrize("payload", [{}, {"success": False}, {"success": True, "result": {}}])
def test_delta_rejects_failed_or_malformed_contract(payload: object) -> None:
    provider = DeltaCandleClient(
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
            base_url="https://api.delta.exchange",
        )
    )
    with pytest.raises((RuntimeError, TypeError), match="Delta candle response"):
        provider.fetch_candle_rows(
            product_id="BTCUSD",
            instr_id=21,
            start_time=_START,
            end_time=_END,
        )


def test_delta_rejects_unsupported_granularity_before_request() -> None:
    provider = DeltaCandleClient(
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: (_ for _ in ()).throw(
                    AssertionError("unsupported granularity must not reach Delta")
                )
            ),
            base_url="https://api.delta.exchange",
        )
    )
    with pytest.raises(ValueError, match="Unsupported Delta granularity"):
        provider.fetch_candle_rows(
            product_id="BTCUSD",
            instr_id=21,
            start_time=_START,
            end_time=_END,
            granularity="ONE_WEEK",
        )


def test_zerodha_normalizes_offset_history_and_authenticates() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/instruments/historical/408065/minute"
        assert request.headers["Authorization"] == "token kite-key:daily-token"
        assert request.headers["X-Kite-Version"] == "3"
        assert request.url.params["from"] == "2026-06-01 05:30:00"
        assert request.url.params["to"] == "2026-06-01 05:33:00"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "candles": [
                        ["2026-06-01T05:31:00+0530", 101, 103, 100, 102, 6],
                        ["2026-06-01T05:30:00+0530", 100, 102, 99, 101, 5],
                    ]
                },
            },
        )

    provider = ZerodhaCandleClient(
        api_key="kite-key",
        access_token="daily-token",
        client=httpx.Client(
            transport=httpx.MockTransport(_handler),
            base_url="https://api.kite.trade",
        ),
    )
    rows = provider.fetch_candle_rows(
        product_id="408065",
        instr_id=31,
        start_time=_START,
        end_time=_END,
        granularity="ONE_MINUTE",
    )

    assert len(rows) == 2
    assert rows[0].instr_id == 31
    assert rows[0].source == "zerodha"
    assert rows[0].timeframe == "1m"
    assert rows[0].ts == datetime(2026, 6, 1)
    assert rows[0].close == 101.0


@pytest.mark.parametrize(
    ("api_key", "access_token"),
    [(None, None), ("kite-key", None), (None, "daily-token"), ("", "daily-token")],
)
def test_zerodha_requires_complete_current_session(
    api_key: str | None,
    access_token: str | None,
) -> None:
    with pytest.raises(ValueError, match="requires both api_key"):
        ZerodhaCandleClient(api_key=api_key, access_token=access_token)


def test_zerodha_requires_numeric_instrument_token_before_request() -> None:
    provider = ZerodhaCandleClient(
        api_key="kite-key",
        access_token="daily-token",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: (_ for _ in ()).throw(
                    AssertionError("ambiguous symbol must not reach Zerodha")
                )
            ),
            base_url="https://api.kite.trade",
        ),
    )
    with pytest.raises(ValueError, match="numeric instrument_token"):
        provider.fetch_candle_rows(
            product_id="NSE:INFY",
            instr_id=31,
            start_time=_START,
            end_time=_END,
        )


def test_zerodha_expired_session_fails_loud() -> None:
    provider = ZerodhaCandleClient(
        api_key="kite-key",
        access_token="expired-token",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(403)),
            base_url="https://api.kite.trade",
        ),
    )
    with pytest.raises(httpx.HTTPStatusError):
        provider.fetch_candle_rows(
            product_id="408065",
            instr_id=31,
            start_time=_START,
            end_time=_END,
        )


def test_saxo_live_normalizes_explicit_uic_asset_type_chart() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/openapi/chart/v3/charts"
        assert request.headers["Authorization"] == "Bearer oauth-token"
        assert request.url.params["Uic"] == "211"
        assert request.url.params["AssetType"] == "Stock"
        assert request.url.params["Horizon"] == "1"
        assert request.url.params["Mode"] == "From"
        assert request.url.params["Time"] == "2026-06-01T00:00:00Z"
        assert request.url.params["Count"] == "4"
        assert request.url.params["AccountKey"] == "account-key"
        return httpx.Response(
            200,
            json={
                "ChartInfo": {"Horizon": 1, "DelayedByMinutes": 0},
                "DataVersion": 7,
                "Data": [
                    {
                        "Time": "2026-06-01T00:01:00Z",
                        "Open": 101,
                        "High": 103,
                        "Low": 100,
                        "Close": 102,
                        "Volume": 6,
                    },
                    {
                        "Time": "2026-06-01T00:00:00Z",
                        "Open": 100,
                        "High": 102,
                        "Low": 99,
                        "Close": 101,
                        "Volume": 5,
                    },
                    {
                        "Time": "2026-06-01T00:03:00Z",
                        "Open": 103,
                        "High": 104,
                        "Low": 102,
                        "Close": 103,
                        "Volume": 2,
                    },
                ],
            },
        )

    provider = SaxoCandleClient(
        environment="live",
        access_token="oauth-token",
        access_token_expires_at=_SAXO_EXPIRY,
        account_key="account-key",
        client=httpx.Client(
            transport=httpx.MockTransport(_handler),
            base_url=SaxoCandleClient.LIVE_BASE_URL,
        ),
        clock=lambda: _START,
    )
    rows = provider.fetch_candle_rows(
        product_id="211",
        instr_id=41,
        start_time=_START,
        end_time=_END,
        granularity="ONE_MINUTE",
        broker_instrument_type="Stock",
    )

    assert provider.source == "saxo_live"
    assert [row.ts for row in rows] == [
        datetime(2026, 6, 1),
        datetime(2026, 6, 1, 0, 1),
    ]
    assert all(row.source == "saxo_live" for row in rows)
    assert rows[1].close == 102.0


def test_saxo_simulation_has_distinct_provenance() -> None:
    provider = SaxoCandleClient(
        environment="simulation",
        access_token="oauth-token",
        access_token_expires_at=_SAXO_EXPIRY,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"ChartInfo": {"Horizon": 1, "DelayedByMinutes": 15}, "Data": []},
                )
            ),
            base_url=SaxoCandleClient.SIMULATION_BASE_URL,
        ),
        clock=lambda: _START,
    )

    assert (
        provider.fetch_candle_rows(
            product_id="211",
            instr_id=41,
            start_time=_START,
            end_time=_END,
            broker_instrument_type="Stock",
        )
        == []
    )
    assert provider.source == "saxo_simulation"


@pytest.mark.parametrize(
    ("product_id", "asset_type"),
    [("AAPL", "Stock"), ("211", None), ("0", "Stock")],
)
def test_saxo_requires_explicit_uic_and_asset_type(
    product_id: str,
    asset_type: str | None,
) -> None:
    provider = SaxoCandleClient(
        environment="live",
        access_token="oauth-token",
        access_token_expires_at=_SAXO_EXPIRY,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    AssertionError(f"invalid mapping reached Saxo: {request.url}")
                )
            ),
            base_url=SaxoCandleClient.LIVE_BASE_URL,
        ),
        clock=lambda: _START,
    )

    with pytest.raises(ValueError, match="Saxo"):
        provider.fetch_candle_rows(
            product_id=product_id,
            instr_id=41,
            start_time=_START,
            end_time=_END,
            broker_instrument_type=asset_type,
        )


def test_saxo_expired_token_fails_before_request() -> None:
    provider = SaxoCandleClient(
        environment="live",
        access_token="oauth-token",
        access_token_expires_at=_START + timedelta(seconds=20),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    AssertionError(f"expired credential reached Saxo: {request.url}")
                )
            ),
            base_url=SaxoCandleClient.LIVE_BASE_URL,
        ),
        clock=lambda: _START,
    )

    with pytest.raises(SaxoTokenExpiredError, match="expires within 30 seconds"):
        provider.fetch_candle_rows(
            product_id="211",
            instr_id=41,
            start_time=_START,
            end_time=_END,
            broker_instrument_type="Stock",
        )


def test_saxo_bid_ask_only_chart_fails_closed() -> None:
    provider = SaxoCandleClient(
        environment="live",
        access_token="oauth-token",
        access_token_expires_at=_SAXO_EXPIRY,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={
                        "ChartInfo": {"Horizon": 1, "DelayedByMinutes": 0},
                        "Data": [
                            {
                                "Time": "2026-06-01T00:00:00Z",
                                "OpenBid": 1.0,
                                "OpenAsk": 1.1,
                                "HighBid": 1.2,
                                "HighAsk": 1.3,
                                "LowBid": 0.9,
                                "LowAsk": 1.0,
                                "CloseBid": 1.05,
                                "CloseAsk": 1.15,
                            }
                        ],
                    },
                )
            ),
            base_url=SaxoCandleClient.LIVE_BASE_URL,
        ),
        clock=lambda: _START,
    )

    with pytest.raises(SaxoChartShapeError, match="bid/ask"):
        provider.fetch_candle_rows(
            product_id="21",
            instr_id=42,
            start_time=_START,
            end_time=_END,
            broker_instrument_type="FxSpot",
        )


@pytest.mark.parametrize("delayed_by", [None, 15])
def test_saxo_live_rejects_unknown_or_delayed_market_data(
    delayed_by: int | None,
) -> None:
    chart_info: dict[str, int] = {"Horizon": 1}
    if delayed_by is not None:
        chart_info["DelayedByMinutes"] = delayed_by
    provider = SaxoCandleClient(
        environment="live",
        access_token="oauth-token",
        access_token_expires_at=_SAXO_EXPIRY,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"ChartInfo": chart_info, "Data": []},
                )
            ),
            base_url=SaxoCandleClient.LIVE_BASE_URL,
        ),
        clock=lambda: _START,
    )

    with pytest.raises(SaxoDelayedMarketDataError, match="DelayedByMinutes=0"):
        provider.fetch_candle_rows(
            product_id="211",
            instr_id=41,
            start_time=_START,
            end_time=_END,
            broker_instrument_type="Stock",
        )
