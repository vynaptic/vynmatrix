"""Contract tests for official broker/exchange market-calendar adapters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from lib_infrastructure.market_data.ibkr_client import IBKRCandleClient
from lib_infrastructure.market_data.market_calendars import (
    IBKR_SCHEDULE_SOURCE,
    NSE_MARKET_STATUS_SOURCE,
    SAXO_LIVE_SCHEDULE_SOURCE,
    NSEOfficialMarketCalendarProvider,
    OfficialMarketCalendarError,
    normalize_ibkr_trading_schedule,
    normalize_saxo_trading_schedule,
)
from lib_infrastructure.market_data.saxo_client import SaxoCandleClient

_OBSERVED_AT = datetime(2026, 7, 24, 12, tzinfo=UTC)


def _epoch(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp())


def test_ibkr_uses_only_regular_liquid_hours_and_covers_internal_non_trading_days() -> None:
    payload = {
        "exchange_time_zone": "America/New_York",
        "schedules": {
            "20260724": {
                "extended_hours": [
                    {
                        "opening": _epoch("2026-07-24T08:00:00Z"),
                        "closing": _epoch("2026-07-24T23:59:00Z"),
                    }
                ],
                "liquid_hours": [
                    {
                        "opening": _epoch("2026-07-24T13:30:00Z"),
                        "closing": _epoch("2026-07-24T20:00:00Z"),
                    }
                ],
            },
            "20260727": {
                "extended_hours": [],
                "liquid_hours": [
                    {
                        "opening": _epoch("2026-07-27T13:30:00Z"),
                        "closing": _epoch("2026-07-27T20:00:00Z"),
                    }
                ],
            },
        },
    }

    observation = normalize_ibkr_trading_schedule(payload, observed_at=_OBSERVED_AT)

    assert observation.provider == "ibkr"
    assert observation.source_reference == IBKR_SCHEDULE_SOURCE
    assert [window.opens_at for window in observation.sessions] == [
        datetime(2026, 7, 24, 13, 30, tzinfo=UTC),
        datetime(2026, 7, 27, 13, 30, tzinfo=UTC),
    ]
    weekend = datetime(2026, 7, 26, 12, tzinfo=UTC)
    assert observation.coverage_start < weekend < observation.coverage_end
    assert all(
        not (window.opens_at <= weekend < window.closes_at) for window in observation.sessions
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"schedules": {"20260724": {"liquid_hours": []}}},
        {"exchange_time_zone": "Not/A-Timezone", "schedules": {"20260724": {}}},
        {"exchange_time_zone": "UTC", "schedules": {}},
    ],
)
def test_ibkr_rejects_unbounded_or_unparseable_schedule(payload: dict[str, Any]) -> None:
    with pytest.raises(OfficialMarketCalendarError):
        normalize_ibkr_trading_schedule(payload, observed_at=_OBSERVED_AT)


def test_saxo_authorizes_only_automated_trading_inside_complete_state_coverage() -> None:
    payload = {
        "Sessions": [
            {
                "StartTime": "2026-07-24T07:00:00Z",
                "EndTime": "2026-07-24T07:30:00Z",
                "State": "PreMarket",
            },
            {
                "StartTime": "2026-07-24T07:30:00Z",
                "EndTime": "2026-07-24T14:00:00Z",
                "State": "AutomatedTrading",
            },
            {
                "StartTime": "2026-07-24T14:00:00Z",
                "EndTime": "2026-07-24T14:05:00Z",
                "State": "Halt",
            },
            {
                "StartTime": "2026-07-24T14:05:00Z",
                "EndTime": "2026-07-24T15:00:00Z",
                "State": "AutomatedTrading",
            },
        ]
    }

    observation = normalize_saxo_trading_schedule(payload, observed_at=_OBSERVED_AT)

    assert observation.provider == "saxo_live"
    assert observation.source_reference == SAXO_LIVE_SCHEDULE_SOURCE
    assert observation.coverage_start == datetime(2026, 7, 24, 7, tzinfo=UTC)
    assert observation.coverage_end == datetime(2026, 7, 24, 15, tzinfo=UTC)
    assert len(observation.sessions) == 2
    assert observation.sessions[0].opens_at == datetime(
        2026,
        7,
        24,
        7,
        30,
        tzinfo=UTC,
    )


def test_nse_open_state_creates_only_a_short_current_date_lease() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/marketStatus"
        assert request.headers["cache-control"] == "no-cache"
        return httpx.Response(
            200,
            json={
                "marketState": [
                    {
                        "market": "Capital Market",
                        "marketStatus": "Open",
                        "tradeDate": "24-Jul-2026 12:00",
                    }
                ]
            },
        )

    client = httpx.Client(
        base_url="https://www.nseindia.com",
        transport=httpx.MockTransport(handler),
    )
    provider = NSEOfficialMarketCalendarProvider(
        market="Capital Market",
        lease_seconds=120,
        client=client,
        clock=lambda: _OBSERVED_AT,
    )

    observation = provider.fetch(product_id="1", broker_instrument_type=None)

    assert observation.provider == "nse"
    assert observation.source_reference == NSE_MARKET_STATUS_SOURCE
    assert observation.coverage_start == _OBSERVED_AT
    assert observation.coverage_end == _OBSERVED_AT + timedelta(seconds=120)
    assert observation.sessions[0].opens_at == _OBSERVED_AT
    assert observation.sessions[0].closes_at == observation.coverage_end
    provider.close()


def test_nse_stale_open_state_is_rejected() -> None:
    client = httpx.Client(
        base_url="https://www.nseindia.com",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "marketState": [
                        {
                            "market": "Capital Market",
                            "marketStatus": "Open",
                            "tradeDate": "23-Jul-2026 12:00",
                        }
                    ]
                },
            )
        ),
    )
    provider = NSEOfficialMarketCalendarProvider(
        market="Capital Market",
        client=client,
        clock=lambda: _OBSERVED_AT,
    )

    with pytest.raises(OfficialMarketCalendarError, match="stale"):
        provider.fetch(product_id="1", broker_instrument_type=None)
    provider.close()


def test_ibkr_client_calls_official_contract_schedule_after_session_check() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/v1/api/tickle":
            return httpx.Response(
                200,
                json={"iserver": {"authStatus": {"authenticated": True}}},
            )
        assert request.url.params["conid"] == "265598"
        return httpx.Response(
            200,
            json={"exchange_time_zone": "UTC", "schedules": {}},
        )

    client = httpx.Client(
        base_url="https://localhost:5000",
        transport=httpx.MockTransport(handler),
    )
    provider = IBKRCandleClient(client=client)

    payload = provider.fetch_trading_schedule(product_id="265598")

    assert payload["exchange_time_zone"] == "UTC"
    assert requested_paths == [
        "/v1/api/tickle",
        "/v1/api/contract/trading-schedule",
    ]
    provider.close()


def test_saxo_client_calls_typed_live_schedule_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/openapi/ref/v1/instruments/tradingschedule/21/FxSpot"
        assert request.headers["authorization"] == "Bearer token"
        return httpx.Response(200, json={"Sessions": []})

    client = httpx.Client(
        base_url=SaxoCandleClient.LIVE_BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    provider = SaxoCandleClient(
        environment="live",
        access_token="token",
        access_token_expires_at=_OBSERVED_AT + timedelta(hours=1),
        client=client,
        clock=lambda: _OBSERVED_AT,
    )

    assert provider.fetch_trading_schedule(
        product_id="21",
        broker_instrument_type="FxSpot",
    ) == {"Sessions": []}
    provider.close()
