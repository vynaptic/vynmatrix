"""Unit tests for the live broker adapters (C-3).

IBKR / Zerodha / Deribit / Delta translate platform OrderIntents into real
money-moving broker calls and previously had ZERO tests. Writing these also
surfaced that Delta and Zerodha were abstract (missing capabilities +
_do_get_open_orders) and could not be instantiated at all — now fixed.

These tests cover the deterministic money-path surfaces: construction/identity,
capability metadata, host selection, order-type/status mapping, the IBKR order
payload builder, and open-order response parsing (with a stubbed client).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from lib_infrastructure.brokers.adapters import ibkr as ibkr_module
from lib_infrastructure.brokers.adapters.coinbase import CoinbaseAdapter
from lib_infrastructure.brokers.adapters.delta import DeltaExchangeAdapter
from lib_infrastructure.brokers.adapters.deribit import DeribitAdapter
from lib_infrastructure.brokers.adapters.ibkr import IBKRAdapter
from lib_infrastructure.brokers.adapters.saxo import SaxoAdapter
from lib_infrastructure.brokers.adapters.zerodha import ZerodhaAdapter
from lib_infrastructure.brokers.capabilities import BROKER_SPECS
from lib_infrastructure.brokers.ports import BrokerFillRetrievalUnsupportedError

ALL_ADAPTERS = [
    DeribitAdapter,
    DeltaExchangeAdapter,
    IBKRAdapter,
    SaxoAdapter,
    ZerodhaAdapter,
]
ADAPTERS_BY_CODE = {
    "coinbase": CoinbaseAdapter,
    "deribit": DeribitAdapter,
    "delta": DeltaExchangeAdapter,
    "ibkr": IBKRAdapter,
    "saxo": SaxoAdapter,
    "zerodha": ZerodhaAdapter,
}


# ── Construction + identity (all four must be instantiable) ────────────────
@pytest.mark.parametrize("cls", ALL_ADAPTERS)
def test_adapter_instantiable_with_identity_and_capabilities(cls: type) -> None:
    adapter = cls(environment="paper")
    assert isinstance(adapter.broker_code, str)
    assert adapter.broker_code
    assert adapter.supported_asset_classes  # non-empty
    caps = adapter.capabilities
    assert caps.get("asset_classes")
    assert caps.get("execution_methods")


def test_broker_codes_match_factory_registration() -> None:
    # broker_code is the DB Broker.code / factory key — must be stable.
    assert DeribitAdapter(environment="paper").broker_code == "deribit"
    assert DeltaExchangeAdapter(environment="paper").broker_code == "delta_exchange"
    assert IBKRAdapter(environment="paper").broker_code == "interactive_brokers"
    assert SaxoAdapter(environment="paper").broker_code == "saxo"
    assert ZerodhaAdapter(environment="paper").broker_code == "zerodha"


@pytest.mark.parametrize("spec", BROKER_SPECS, ids=lambda spec: spec.code)
def test_runtime_capability_matrix_matches_adapter_contract(spec: Any) -> None:
    adapter_class = ADAPTERS_BY_CODE[spec.code]
    adapter = adapter_class(environment="paper")
    advertised = adapter.capabilities
    matrix = spec.capabilities

    assert set(adapter.supported_asset_classes) == set(matrix.supported_assets)
    assert {method.value for method in adapter.supported_execution_methods} == set(
        matrix.execution_methods()
    )
    assert bool(adapter.supports_multi_leg_orders) is matrix.supports_multi_leg_native
    assert set(advertised["asset_classes"]) == set(matrix.supported_assets)
    assert set(advertised["execution_methods"]) == set(matrix.execution_methods())
    assert bool(advertised["multi_leg_orders"]) is matrix.supports_multi_leg_native
    assert set(advertised["supported_order_types"]) == set(matrix.supported_order_types)
    assert set(advertised["regions"]) == set(matrix.regions)
    assert bool(advertised.get("exact_fill_retrieval")) is (matrix.supports_exact_fill_retrieval)


def test_deribit_exact_trade_retrieval_preserves_complete_economics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = DeribitAdapter(environment="paper")

    async def _authenticated() -> None:
        return None

    async def _private_request(
        method: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert method == "private/get_user_trades_by_order"
        assert params == {"order_id": "deribit-order-1", "historical": True}
        return {
            "result": [
                {
                    "trade_id": "deribit-trade-1",
                    "order_id": "deribit-order-1",
                    "instrument_name": "BTC-PERPETUAL",
                    "direction": "buy",
                    "amount": "10",
                    "price": "63500.5",
                    "fee": "-0.0000025",
                    "fee_currency": "BTC",
                    "timestamp": 1784974530123,
                }
            ]
        }

    monkeypatch.setattr(adapter, "_ensure_authenticated", _authenticated)
    monkeypatch.setattr(adapter, "_private_request", _private_request)

    fills = asyncio.run(adapter._do_get_fills("deribit-order-1"))

    assert fills == [
        {
            "trade_id": "deribit-trade-1",
            "broker_order_id": "deribit-order-1",
            "symbol": "BTC-PERPETUAL",
            "side": "buy",
            "quantity": "10",
            "price": "63500.5",
            "fee_amount": "-0.0000025",
            "fee_currency": "BTC",
            "timestamp": "2026-07-25T10:15:30.123000+00:00",
            "venue": "deribit",
            "raw_response": {
                "trade_id": "deribit-trade-1",
                "order_id": "deribit-order-1",
                "instrument_name": "BTC-PERPETUAL",
                "direction": "buy",
                "amount": "10",
                "price": "63500.5",
                "fee": "-0.0000025",
                "fee_currency": "BTC",
                "timestamp": 1784974530123,
            },
        }
    ]


@pytest.mark.parametrize(
    "adapter_class",
    [IBKRAdapter, SaxoAdapter, ZerodhaAdapter, DeltaExchangeAdapter],
)
def test_uncertifiable_brokers_fail_exact_fill_retrieval_explicitly(
    adapter_class: type,
) -> None:
    adapter = adapter_class(environment="paper")

    with pytest.raises(BrokerFillRetrievalUnsupportedError, match="certification-grade"):
        asyncio.run(adapter._do_get_fills("venue-order-1"))


# ── Host selection (paper vs live must never be confused) ──────────────────
def test_deribit_host_selection() -> None:
    assert DeribitAdapter(environment="paper")._base_url == "https://test.deribit.com"
    assert DeribitAdapter(environment="live")._base_url == "https://www.deribit.com"


def test_delta_host_selection() -> None:
    assert (
        DeltaExchangeAdapter._resolve_base_url(environment="paper", region="global")
        == "https://testnet-api.delta.exchange"
    )
    assert (
        DeltaExchangeAdapter._resolve_base_url(environment="live", region="global")
        == "https://api.delta.exchange"
    )
    assert (
        DeltaExchangeAdapter._resolve_base_url(environment="paper", region="india")
        == "https://cdn-ind.testnet.deltaex.org"
    )
    assert (
        DeltaExchangeAdapter._resolve_base_url(environment="live", region="india")
        == "https://api.india.delta.exchange"
    )


@pytest.mark.parametrize(
    ("environment", "region"),
    [("paper", "eu"), ("live", ""), ("backtest", "global")],
)
def test_delta_host_selection_rejects_unknown_route(environment: str, region: str) -> None:
    with pytest.raises(ValueError, match="requires environment paper/live"):
        DeltaExchangeAdapter._resolve_base_url(environment=environment, region=region)


def test_delta_connection_requires_explicit_region_before_network() -> None:
    adapter = DeltaExchangeAdapter(environment="live")

    connected = asyncio.run(adapter._do_connect(api_key="delta-key", api_secret="delta-secret"))

    assert connected is False
    assert adapter._client is None


def test_zerodha_paper_connection_fails_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ZerodhaAdapter(environment="paper")

    async def forbidden_connect(*_args: Any, **_kwargs: Any) -> bool:
        pytest.fail("paper mode must never call the production Kite connector")

    monkeypatch.setattr(adapter, "_do_connect", forbidden_connect)

    connected = asyncio.run(adapter.connect(api_key="unused", api_secret="unused"))

    assert connected is False
    assert adapter.supports_paper_trading is False
    assert adapter._client is None


def test_zerodha_requires_control_plane_managed_access_token() -> None:
    adapter = ZerodhaAdapter(environment="live")

    assert (
        asyncio.run(
            adapter._do_connect(
                api_key="kite-key",
                api_secret="kite-secret",
                request_token="one-time-request-token",
            )
        )
        is False
    )
    assert adapter._client is None


@pytest.mark.parametrize(
    "expires_at",
    [
        "",
        "2035-01-01T00:00:00",
        "2020-01-01T00:00:00+00:00",
        "not-a-timestamp",
    ],
)
def test_zerodha_rejects_invalid_or_expired_token_expiry(expires_at: str) -> None:
    with pytest.raises(ValueError, match=r"expiry|expired|RFC 3339"):
        ZerodhaAdapter._parse_access_token_expiry(expires_at)


def test_zerodha_accepts_future_aware_token_expiry() -> None:
    expires_at = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()

    assert ZerodhaAdapter._parse_access_token_expiry(expires_at).tzinfo is not None


def test_zerodha_disconnect_does_not_revoke_daily_token() -> None:
    class _Client:
        def __init__(self) -> None:
            self.closed = False

        async def delete(self, _path: str) -> None:
            pytest.fail("service shutdown must not revoke the external Kite session")

        async def aclose(self) -> None:
            self.closed = True

    adapter = ZerodhaAdapter(environment="live")
    client = _Client()
    adapter._client = client  # type: ignore[assignment]

    asyncio.run(adapter._do_disconnect())

    assert client.closed is True
    assert adapter._client is None


def test_ibkr_requires_exact_subaccount_before_network() -> None:
    adapter = IBKRAdapter(environment="paper")

    connected = asyncio.run(
        adapter._do_connect(
            api_key="",
            api_secret="",
            gateway_url="https://localhost:5000",
        )
    )

    assert connected is False
    assert adapter._client is None


def test_ibkr_live_requires_pinned_ca_before_network() -> None:
    adapter = IBKRAdapter(environment="live")

    connected = asyncio.run(
        adapter._do_connect(
            api_key="",
            api_secret="",
            subaccount="U1234567",
            gateway_url="https://localhost:5000",
        )
    )

    assert connected is False
    assert adapter._client is None


def test_ibkr_connect_verifies_exact_trading_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []

    class _ConnectClient:
        async def get(self, path: str, **_: Any) -> _StubResponse:
            paths.append(path)
            if path.endswith("/auth/status"):
                return _StubResponse({"authenticated": True, "connected": True})
            if path.endswith("/iserver/accounts"):
                return _StubResponse(
                    {
                        "accounts": ["DU123", "DU999"],
                        "selectedAccount": "DU999",
                    }
                )
            pytest.fail(f"Unexpected IBKR endpoint: {path}")

        async def aclose(self) -> None:
            return None

    client = _ConnectClient()
    monkeypatch.setattr(ibkr_module.httpx, "AsyncClient", lambda **_: client)
    adapter = IBKRAdapter(environment="paper")

    connected = asyncio.run(
        adapter._do_connect(
            api_key="",
            api_secret="",
            subaccount="DU123",
            gateway_url="https://localhost:5000",
        )
    )

    assert connected is True
    assert paths == [
        "/v1/api/iserver/auth/status",
        "/v1/api/iserver/accounts",
    ]
    assert adapter._account_id == "DU123"


def test_ibkr_connect_rejects_account_not_authorized_by_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ConnectClient:
        def __init__(self) -> None:
            self.closed = False

        async def get(self, path: str, **_: Any) -> _StubResponse:
            if path.endswith("/auth/status"):
                return _StubResponse({"authenticated": True, "connected": True})
            if path.endswith("/iserver/accounts"):
                return _StubResponse({"accounts": ["DU999"]})
            pytest.fail(f"Unexpected IBKR endpoint: {path}")

        async def aclose(self) -> None:
            self.closed = True

    client = _ConnectClient()
    monkeypatch.setattr(ibkr_module.httpx, "AsyncClient", lambda **_: client)
    adapter = IBKRAdapter(environment="paper")

    connected = asyncio.run(
        adapter._do_connect(
            api_key="",
            api_secret="",
            subaccount="DU123",
            gateway_url="https://localhost:5000",
        )
    )

    assert connected is False
    assert client.closed is True
    assert adapter._client is None


# ── Order-type mapping (platform → broker; money-critical) ─────────────────
def test_deribit_order_type_mapping() -> None:
    a = DeribitAdapter(environment="paper")
    assert a._map_order_type("market") == "market"
    assert a._map_order_type("limit") == "limit"
    assert a._map_order_type("stop") == "stop_market"
    assert a._map_order_type("stop_limit") == "stop_limit"


def test_delta_order_type_mapping() -> None:
    a = DeltaExchangeAdapter(environment="paper")
    assert a._map_order_type("market") == "market_order"
    assert a._map_order_type("limit") == "limit_order"
    assert a._map_order_type("stop") == "stop_market_order"
    assert a._map_order_type("stop_limit") == "stop_limit_order"


def test_zerodha_order_type_and_validity_mapping() -> None:
    a = ZerodhaAdapter(environment="paper")
    assert a._map_order_type("market") == "MARKET"
    assert a._map_order_type("limit") == "LIMIT"
    assert a._map_order_type("stop") == "SL-M"
    assert a._map_order_type("stop_limit") == "SL"
    # Zerodha has no GTC for regular orders → must fall back to DAY.
    assert a._map_validity("gtc") == "DAY"
    assert a._map_validity("ioc") == "IOC"


# ── Order-status mapping (broker → platform) ───────────────────────────────
def test_order_status_mapping_per_broker() -> None:
    assert DeribitAdapter(environment="paper")._map_order_status("filled") == "filled"
    assert DeribitAdapter(environment="paper")._map_order_status("rejected") == "rejected"
    assert DeltaExchangeAdapter(environment="paper")._map_order_status("closed") == "filled"
    assert ZerodhaAdapter(environment="paper")._map_order_status("COMPLETE") == "filled"
    assert ZerodhaAdapter(environment="paper")._map_order_status("REJECTED") == "rejected"


# ── IBKR order payload builder (the OrderIntent → IBKR translation) ────────
def test_ibkr_build_order_market_buy() -> None:
    a = IBKRAdapter(environment="paper")
    a._account_id = "DU123"
    order = a._build_order({"side": "buy", "quantity": "2", "order_type": "market"}, conid=99)
    assert order == {
        "acctId": "DU123",
        "conid": 99,
        "orderType": "MKT",
        "side": "BUY",
        "quantity": 2.0,
        "tif": "DAY",
    }


def test_ibkr_build_order_limit_sell_with_prices_and_tif() -> None:
    a = IBKRAdapter(environment="paper")
    a._account_id = "DU123"
    order = a._build_order(
        {
            "side": "sell",
            "quantity": "1.5",
            "order_type": "stop_limit",
            "time_in_force": "ioc",
            "limit_price": "101.5",
            "stop_price": "100.0",
            "client_order_id": "abc",
        },
        conid=7,
    )
    assert order["orderType"] == "STP LMT"
    assert order["side"] == "SELL"
    assert order["quantity"] == 1.5
    assert order["tif"] == "IOC"
    assert order["price"] == 101.5
    assert order["auxPrice"] == 100.0
    assert order["cOID"] == "abc"


def test_ibkr_stock_order_requires_exact_catalogue_lot_multiple() -> None:
    adapter = IBKRAdapter(environment="paper")
    adapter._account_id = "DU123"

    order = adapter._build_order(
        {
            "side": "buy",
            "quantity": "2",
            "order_type": "market",
            "broker_instrument_type": "STK",
            "lot_size": "1",
        },
        conid=99,
    )

    assert order["quantity"] == 2


@pytest.mark.parametrize(
    ("quantity", "lot_size", "message"),
    [
        ("1.5", "1", "lot multiple"),
        ("1", "", "positive catalogue lot_size"),
    ],
)
def test_ibkr_stock_order_rejects_unsafe_quantity(
    quantity: str,
    lot_size: str,
    message: str,
) -> None:
    adapter = IBKRAdapter(environment="paper")
    adapter._account_id = "DU123"

    with pytest.raises(ValueError, match=message):
        adapter._build_order(
            {
                "side": "buy",
                "quantity": quantity,
                "order_type": "market",
                "broker_instrument_type": "STK",
                "lot_size": lot_size,
            },
            conid=99,
        )


def test_ibkr_order_uses_persisted_conid_without_contract_search() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/api/tickle":
            return httpx.Response(
                200,
                json={
                    "iserver": {
                        "authStatus": {
                            "authenticated": True,
                            "connected": True,
                        }
                    }
                },
            )
        if request.url.path == "/v1/api/iserver/account/DU123/orders":
            payload = json.loads(request.content)
            assert payload["orders"][0]["conid"] == 265598
            return httpx.Response(200, json=[{"order_id": "ibkr-order-1"}])
        pytest.fail(f"Unexpected IBKR endpoint: {request.url.path}")

    async def scenario() -> dict[str, Any]:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://localhost:5000",
        )
        adapter = IBKRAdapter(environment="paper")
        adapter._client = client
        adapter._session_valid = True
        adapter._account_id = "DU123"
        try:
            return await adapter._do_place_order(
                {
                    "symbol": "AAPL",
                    "broker_instrument_id": "265598",
                    "side": "buy",
                    "quantity": "2",
                    "order_type": "market",
                }
            )
        finally:
            await client.aclose()

    result = asyncio.run(scenario())

    assert result["success"] is True
    assert [request.url.path for request in requests] == [
        "/v1/api/tickle",
        "/v1/api/iserver/account/DU123/orders",
    ]


@pytest.mark.parametrize(
    "conid",
    [None, False, True, 0, -1, "AAPL", "01", "+1", "1.0", "\u0661"],
)
def test_ibkr_rejects_noncanonical_conid_before_network(conid: object) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Invalid conid reached IBKR network: {request.url}")

    async def scenario() -> dict[str, Any]:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(forbidden),
            base_url="https://localhost:5000",
        )
        adapter = IBKRAdapter(environment="paper")
        adapter._client = client
        adapter._session_valid = True
        adapter._account_id = "DU123"
        try:
            return await adapter._do_place_order(
                {
                    "symbol": "AAPL",
                    "broker_instrument_id": conid,  # type: ignore[typeddict-item]
                    "side": "buy",
                    "quantity": "2",
                    "order_type": "market",
                }
            )
        finally:
            await client.aclose()

    result = asyncio.run(scenario())

    assert result["success"] is False
    assert result["status"] == "rejected"
    assert "conid" in result["error_message"]


def test_ibkr_unauthenticated_tickle_blocks_order_submission() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/api/tickle":
            return httpx.Response(
                200,
                json={
                    "iserver": {
                        "authStatus": {
                            "authenticated": False,
                            "connected": True,
                        }
                    }
                },
            )
        pytest.fail(f"Unauthenticated session reached order endpoint: {request.url}")

    async def scenario() -> tuple[dict[str, Any], bool]:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://localhost:5000",
        )
        adapter = IBKRAdapter(environment="paper")
        adapter._client = client
        adapter._session_valid = True
        adapter._account_id = "DU123"
        try:
            result = await adapter._do_place_order(
                {
                    "symbol": "AAPL",
                    "broker_instrument_id": "265598",
                    "side": "buy",
                    "quantity": "2",
                    "order_type": "market",
                }
            )
            return result, adapter._session_valid
        finally:
            await client.aclose()

    result, session_valid = asyncio.run(scenario())

    assert result["success"] is False
    assert "not authenticated" in result["error_message"]
    assert session_valid is False
    assert [request.url.path for request in requests] == ["/v1/api/tickle"]


def test_ibkr_multi_leg_rejects_reused_conid_before_network() -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Ambiguous IBKR combo reached network: {request.url}")

    async def scenario() -> dict[str, Any]:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(forbidden),
            base_url="https://localhost:5000",
        )
        adapter = IBKRAdapter(environment="paper")
        adapter._client = client
        adapter._session_valid = True
        adapter._account_id = "DU123"
        legs = [
            {
                "symbol": "AAPL:2026-09-18:200:CALL",
                "broker_instrument_id": "1001",
                "side": "buy",
                "quantity": "1",
                "order_type": "limit",
            },
            {
                "symbol": "AAPL:2026-09-18:210:CALL",
                "broker_instrument_id": "1001",
                "side": "sell",
                "quantity": "1",
                "order_type": "limit",
            },
        ]
        try:
            return await adapter._do_place_multi_leg_order(legs)
        finally:
            await client.aclose()

    result = asyncio.run(scenario())

    assert result["success"] is False
    assert "distinct persisted conid" in result["error_message"]


# ── Open-order parsing (broker response → BrokerOrderResult) ───────────────
class _StubResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= httpx.codes.BAD_REQUEST:
            request = httpx.Request("GET", "https://localhost:5000")
            response = httpx.Response(self.status_code, request=request)
            response.raise_for_status()


class _StubGetClient:
    """Stub httpx client exposing only async get() for Zerodha."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def get(self, _url: str, **_: Any) -> _StubResponse:
        return _StubResponse(self._payload)


def test_zerodha_open_orders_parsing_filters_terminal() -> None:
    a = ZerodhaAdapter(environment="paper")
    a._client = _StubGetClient(
        {
            "status": "success",
            "data": [
                {"order_id": "1", "status": "OPEN", "filled_quantity": 0, "average_price": 0},
                {"order_id": "2", "status": "COMPLETE", "filled_quantity": 5, "average_price": 100},
            ],
        }
    )
    orders = asyncio.run(a._do_get_open_orders())
    # Only the OPEN order is returned; the COMPLETE (terminal) one is filtered.
    assert len(orders) == 1
    assert orders[0]["broker_order_id"] == "1"
    assert orders[0]["status"] == "submitted"


def test_delta_open_orders_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    a = DeltaExchangeAdapter(environment="paper")
    a._client = object()  # truthy; _request is stubbed below

    async def _fake_request(method: str, path: str, **_: Any) -> dict[str, Any]:
        assert method == "GET"
        assert path == "/v2/orders"
        return {
            "success": True,
            "result": [
                {"id": 11, "state": "open", "size_filled": 0, "average_fill_price": 0},
            ],
        }

    monkeypatch.setattr(a, "_request", _fake_request)
    orders = asyncio.run(a._do_get_open_orders())
    assert len(orders) == 1
    assert orders[0]["broker_order_id"] == "11"
    assert orders[0]["status"] == "submitted"


def test_ibkr_positions_preserve_broker_currency_metadata() -> None:
    adapter = IBKRAdapter(environment="paper")
    adapter._account_id = "DU123"
    adapter._session_valid = True
    adapter._client = _StubGetClient(
        [
            {
                "contractDesc": "ASML",
                "position": -2,
                "avgCost": 1300,
                "avgPrice": 650,
                "mktPrice": 675,
                "mktValue": -1350,
                "unrealizedPnl": 50,
                "realizedPnl": 25,
                "assetClass": "STK",
                "currency": "EUR",
            }
        ]
    )

    positions = asyncio.run(adapter._do_get_positions())

    assert positions == [
        {
            "symbol": "ASML",
            "quantity": "-2",
            "quantity_unit": "asset",
            "side": "short",
            "entry_price": "650",
            "current_price": "675",
            "unrealized_pnl": "50",
            "realized_pnl": "25",
            "asset_class": "STK",
            "currency": "EUR",
            "quote_currency": "EUR",
            "settlement_currency": "EUR",
            "pnl_currency": "EUR",
            "notional_currency": "EUR",
            "gross_notional": "1350",
            "notional_type": "broker_mark_value",
            "is_quanto": False,
        }
    ]


def test_ibkr_positions_do_not_mask_missing_pnl_as_zero() -> None:
    adapter = IBKRAdapter(environment="paper")
    adapter._account_id = "DU123"
    adapter._session_valid = True
    adapter._client = _StubGetClient(
        [
            {
                "contractDesc": "ASML",
                "position": 2,
                "avgPrice": 650,
                "mktPrice": 675,
                "mktValue": 1350,
                "assetClass": "STK",
                "currency": "EUR",
            }
        ]
    )

    positions = asyncio.run(adapter._do_get_positions())

    assert "unrealized_pnl" not in positions[0]
    assert "realized_pnl" not in positions[0]


def test_ibkr_snapshot_uses_authoritative_summary_fields() -> None:
    adapter = IBKRAdapter(environment="paper")
    adapter._account_id = "DU123"
    adapter._session_valid = True

    class _SummaryClient:
        async def get(self, path: str, **_: Any) -> _StubResponse:
            if path.endswith("/summary"):
                return _StubResponse(
                    {
                        "netliquidation": {"amount": 10000, "currency": "EUR"},
                        "availablefunds": {"amount": 7500, "currency": "EUR"},
                        "maintmarginreq": {"amount": 500, "currency": "EUR"},
                    }
                )
            if path.endswith("/positions/0"):
                return _StubResponse([])
            if path.endswith("/ledger"):
                return _StubResponse(
                    {
                        "BASE": {"cashbalance": 9000, "settledcash": 9000},
                        "EUR": {"cashbalance": 2500, "settledcash": 2500},
                        "USD": {"cashbalance": 7000, "settledcash": 6900},
                    }
                )
            pytest.fail(f"Unexpected IBKR endpoint: {path}")

    adapter._client = _SummaryClient()

    snapshot = asyncio.run(adapter._do_get_account_snapshot())

    assert snapshot == {
        "balances": [
            {"currency": "EUR", "total": "2500", "available": "2500", "locked": "0"},
            {"currency": "USD", "total": "7000", "available": "6900", "locked": "100"},
        ],
        "positions": [],
        "equity": [{"value": "10000", "currency": "EUR"}],
        "available_cash": [{"value": "7500", "currency": "EUR"}],
        "margin_used": [{"value": "500", "currency": "EUR"}],
        "equity_source": "broker",
    }


def test_ibkr_snapshot_keeps_positions_but_omits_failed_ledger() -> None:
    adapter = IBKRAdapter(environment="paper")
    adapter._account_id = "DU123"
    adapter._session_valid = True

    class _LedgerFailureClient:
        async def get(self, path: str, **_: Any) -> _StubResponse:
            if path.endswith("/summary"):
                return _StubResponse(
                    {
                        "netliquidation": {"amount": 10000, "currency": "EUR"},
                        "availablefunds": {"amount": 7500, "currency": "EUR"},
                        "maintmarginreq": {"amount": 500, "currency": "EUR"},
                    }
                )
            if path.endswith("/positions/0"):
                return _StubResponse([])
            if path.endswith("/ledger"):
                raise RuntimeError("ledger unavailable")
            pytest.fail(f"Unexpected IBKR endpoint: {path}")

    adapter._client = _LedgerFailureClient()

    snapshot = asyncio.run(adapter._do_get_account_snapshot())

    assert snapshot["balances"] == []
    assert snapshot["positions"] == []
    assert snapshot["equity"] == [{"value": "10000", "currency": "EUR"}]


def test_ibkr_ledger_caps_settled_cash_at_nonnegative_total_cash() -> None:
    adapter = IBKRAdapter(environment="paper")

    balances = adapter._ledger_balances({"USD": {"cashbalance": 100, "settledcash": 1000}})

    assert balances == [{"currency": "USD", "total": "100", "available": "100", "locked": "0"}]

    with pytest.raises(ValueError, match="negative cash"):
        adapter._ledger_balances({"USD": {"cashbalance": -100, "settledcash": 1000}})


def test_zerodha_balances_use_iso_currency_for_all_segments() -> None:
    adapter = ZerodhaAdapter(environment="live")
    adapter._client = _StubGetClient(
        {
            "status": "success",
            "data": {
                "equity": {
                    "net": 1000,
                    "available": {"cash": 750},
                    "utilised": {"debits": 250},
                },
                "commodity": {
                    "net": 500,
                    "available": {"cash": 400},
                    "utilised": {"debits": 100},
                },
            },
        }
    )

    balances = asyncio.run(adapter._do_get_account_balance())

    assert [balance["currency"] for balance in balances] == ["INR", "INR"]
    assert [balance["available"] for balance in balances] == ["750", "400"]


def test_zerodha_positions_declare_inr_price_and_pnl_currency() -> None:
    adapter = ZerodhaAdapter(environment="live")
    adapter._client = _StubGetClient(
        {
            "status": "success",
            "data": {
                "net": [
                    {
                        "tradingsymbol": "NIFTY26JULFUT",
                        "exchange": "NFO",
                        "quantity": -75,
                        "average_price": 25000,
                        "last_price": 25100,
                        "pnl": 7500,
                        "unrealised": 6500,
                        "realised": 1000,
                        "product": "NRML",
                        "multiplier": 1,
                    }
                ]
            },
        }
    )

    positions = asyncio.run(adapter._do_get_positions())

    assert positions[0]["quantity"] == "-75"
    assert positions[0]["side"] == "short"
    assert positions[0]["current_price"] == "25100"
    assert positions[0]["unrealized_pnl"] == "6500"
    assert positions[0]["realized_pnl"] == "1000"
    assert "last_price" not in positions[0]
    assert "pnl" not in positions[0]
    assert positions[0]["currency"] == "INR"
    assert positions[0]["settlement_currency"] == "INR"
    assert positions[0]["pnl_currency"] == "INR"
    assert positions[0]["quantity_unit"] == "contracts"
    assert positions[0]["contract_multiplier"] == "1"
    assert positions[0]["gross_notional"] == "1882500"


def test_delta_positions_preserve_product_quote_and_settlement_currencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = DeltaExchangeAdapter(environment="live")
    adapter._client = object()
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def _fake_request(
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        assert method == "GET"
        calls.append((path, params))
        if path == "/v2/positions/margined":
            return {
                "success": True,
                "result": [
                    {
                        "size": 2,
                        "entry_price": "60000",
                        "margin": "1200",
                        "liquidation_price": "45000",
                        "product_id": 139,
                        "product_symbol": "BTCUSDT",
                        "realized_pnl": "25",
                    }
                ],
                "meta": {"after": None},
            }
        if path == "/v2/products/139":
            return {
                "success": True,
                "result": {
                    "id": 139,
                    "symbol": "BTCUSDT",
                    "quoting_asset": {"symbol": "USDT"},
                    "settling_asset": {"symbol": "USDT"},
                    "contract_value": "0.001",
                    "notional_type": "vanilla",
                    "contract_type": "perpetual_futures",
                    "is_quanto": False,
                },
            }
        if path == "/v2/tickers/BTCUSDT":
            return {
                "success": True,
                "result": {"symbol": "BTCUSDT", "mark_price": "61000"},
            }
        pytest.fail(f"Unexpected Delta endpoint: {path}")

    monkeypatch.setattr(adapter, "_request", _fake_request)

    positions = asyncio.run(adapter._do_get_positions())

    assert calls == [
        ("/v2/positions/margined", {"page_size": 50}),
        ("/v2/products/139", None),
        ("/v2/tickers/BTCUSDT", None),
    ]
    assert positions[0]["symbol"] == "BTCUSDT"
    assert positions[0]["mark_price"] == "61000"
    assert positions[0]["realized_pnl"] == "25"
    assert positions[0]["unrealized_pnl"] == "2.000"
    assert positions[0]["margin_used"] == "1200"
    assert positions[0]["quote_currency"] == "USDT"
    assert positions[0]["settlement_currency"] == "USDT"
    assert positions[0]["pnl_currency"] == "USDT"
    assert positions[0]["quantity_unit"] == "contracts"
    assert positions[0]["contract_multiplier"] == "0.001"
    assert positions[0]["gross_notional"] == "122.000"


@pytest.mark.parametrize(
    ("notional_type", "contract_type", "is_quanto"),
    [
        ("inverse", "perpetual_futures", False),
        ("vanilla", "call_options", False),
        ("vanilla", "perpetual_futures", True),
    ],
)
def test_delta_positions_reject_unsupported_contract_valuation(
    monkeypatch: pytest.MonkeyPatch,
    notional_type: str,
    contract_type: str,
    is_quanto: bool,
) -> None:
    adapter = DeltaExchangeAdapter(environment="live")
    adapter._client = object()

    async def _fake_request(method: str, path: str, **_: Any) -> dict[str, Any]:
        assert method == "GET"
        if path == "/v2/positions/margined":
            return {
                "success": True,
                "result": [
                    {
                        "size": 2,
                        "entry_price": "60000",
                        "margin": "1200",
                        "product_id": 139,
                        "product_symbol": "BTCUSDT",
                        "realized_pnl": "25",
                    }
                ],
            }
        if path == "/v2/products/139":
            return {
                "success": True,
                "result": {
                    "id": 139,
                    "symbol": "BTCUSDT",
                    "quoting_asset": {"symbol": "USDT"},
                    "settling_asset": {"symbol": "USDT"},
                    "contract_value": "0.001",
                    "notional_type": notional_type,
                    "contract_type": contract_type,
                    "is_quanto": is_quanto,
                },
            }
        if path == "/v2/tickers/BTCUSDT":
            return {
                "success": True,
                "result": {"symbol": "BTCUSDT", "mark_price": "61000"},
            }
        pytest.fail(f"Unexpected Delta endpoint: {path}")

    monkeypatch.setattr(adapter, "_request", _fake_request)

    with pytest.raises(RuntimeError, match="unsupported valuation model"):
        asyncio.run(adapter._do_get_positions())


def test_delta_positions_require_realized_pnl_and_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = DeltaExchangeAdapter(environment="live")
    adapter._client = object()

    async def _fake_request(method: str, path: str, **_: Any) -> dict[str, Any]:
        assert method == "GET"
        if path == "/v2/positions/margined":
            return {
                "success": True,
                "result": [
                    {
                        "size": 2,
                        "entry_price": "60000",
                        "margin": "1200",
                        "product_id": 139,
                        "product_symbol": "BTCUSDT",
                    }
                ],
            }
        if path == "/v2/products/139":
            return {
                "success": True,
                "result": {
                    "id": 139,
                    "symbol": "BTCUSDT",
                    "quoting_asset": {"symbol": "USDT"},
                    "settling_asset": {"symbol": "USDT"},
                    "contract_value": "0.001",
                    "notional_type": "vanilla",
                    "contract_type": "perpetual_futures",
                    "is_quanto": False,
                },
            }
        if path == "/v2/tickers/BTCUSDT":
            return {
                "success": True,
                "result": {"symbol": "BTCUSDT", "mark_price": "61000"},
            }
        pytest.fail(f"Unexpected Delta endpoint: {path}")

    monkeypatch.setattr(adapter, "_request", _fake_request)

    with pytest.raises(RuntimeError, match="omitted realized P&L or margin"):
        asyncio.run(adapter._do_get_positions())


def test_delta_positions_fail_closed_without_product_currency_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = DeltaExchangeAdapter(environment="live")
    adapter._client = object()

    async def _fake_request(method: str, path: str, **_: Any) -> dict[str, Any]:
        assert method == "GET"
        if path == "/v2/positions/margined":
            return {
                "success": True,
                "result": [
                    {
                        "size": -1,
                        "entry_price": "100",
                        "product_id": 139,
                        "product_symbol": "BTCUSDT",
                    }
                ],
            }
        if path == "/v2/products/139":
            return {
                "success": True,
                "result": {
                    "id": 139,
                    "symbol": "BTCUSDT",
                    "quoting_asset": {"symbol": "USDT"},
                },
            }
        if path == "/v2/tickers/BTCUSDT":
            return {
                "success": True,
                "result": {"symbol": "BTCUSDT", "mark_price": "90"},
            }
        pytest.fail(f"Unexpected Delta endpoint: {path}")

    monkeypatch.setattr(adapter, "_request", _fake_request)

    with pytest.raises(RuntimeError, match="currency metadata unavailable"):
        asyncio.run(adapter._do_get_positions())


def test_delta_positions_follow_cursor_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = DeltaExchangeAdapter(environment="live")
    adapter._client = object()
    requested_params: list[dict[str, Any] | None] = []

    async def _fake_request(
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        assert method == "GET"
        assert path == "/v2/positions/margined"
        requested_params.append(params)
        if params == {"page_size": 50}:
            return {
                "success": True,
                "result": [{"product_id": 139, "product_symbol": "BTCUSDT", "size": 0}],
                "meta": {"after": "next-page"},
            }
        return {
            "success": True,
            "result": [{"product_id": 176, "product_symbol": "ETHUSDT", "size": 0}],
            "meta": {"after": None},
        }

    monkeypatch.setattr(adapter, "_request", _fake_request)

    positions = asyncio.run(adapter._do_get_positions())

    assert positions == []
    assert requested_params == [
        {"page_size": 50},
        {"page_size": 50, "after": "next-page"},
    ]


def test_deribit_positions_use_broker_notional_and_base_currency_pnl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = DeribitAdapter(environment="live")

    async def _authenticated() -> None:
        return None

    async def _private_request(
        method: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert method == "private/get_positions"
        if params == {"currency": "ETH"}:
            return {"result": []}
        assert params == {"currency": "BTC"}
        return {
            "result": [
                {
                    "instrument_name": "BTC-PERPETUAL",
                    "kind": "future",
                    "size": 1000,
                    "average_price": "60000",
                    "mark_price": "61000",
                    "floating_profit_loss": "0.00027322",
                    "realized_profit_loss": "0.0001",
                    "initial_margin": "0.001",
                    "estimated_liquidation_price": "45000",
                }
            ]
        }

    monkeypatch.setattr(adapter, "_ensure_authenticated", _authenticated)
    monkeypatch.setattr(adapter, "_private_request", _private_request)

    positions = asyncio.run(adapter._do_get_positions())

    assert positions == [
        {
            "symbol": "BTC-PERPETUAL",
            "quantity": "1000",
            "quantity_unit": "quote_notional",
            "side": "long",
            "entry_price": "60000",
            "mark_price": "61000",
            "unrealized_pnl": "0.00027322",
            "realized_pnl": "0.0001",
            "margin_used": "0.001",
            "liquidation_price": "45000",
            "asset_currency": "BTC",
            "quote_currency": "USD",
            "settlement_currency": "BTC",
            "pnl_currency": "BTC",
            "notional_currency": "USD",
            "gross_notional": "1000",
            "notional_type": "broker_notional",
            "contract_type": "future",
            "is_quanto": False,
        }
    ]


def test_deribit_positions_reject_options_without_guessing_notional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = DeribitAdapter(environment="live")

    async def _authenticated() -> None:
        return None

    async def _private_request(
        method: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert method == "private/get_positions"
        return {
            "result": [
                {
                    "instrument_name": "BTC-30JUL26-60000-C",
                    "kind": "option",
                    "size": 1,
                }
            ]
        }

    monkeypatch.setattr(adapter, "_ensure_authenticated", _authenticated)
    monkeypatch.setattr(adapter, "_private_request", _private_request)

    with pytest.raises(RuntimeError, match="unsupported valuation kind"):
        asyncio.run(adapter._do_get_positions())


def test_deribit_snapshot_requires_all_currency_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = DeribitAdapter(environment="live")

    async def _authenticated() -> None:
        return None

    async def _private_request(
        method: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if method == "private/get_account_summary":
            currency = str((params or {})["currency"])
            if currency == "ETH":
                return {"result": {"equity": "2", "initial_margin": "0.1"}}
            return {
                "result": {
                    "equity": "1",
                    "available_funds": "0.8",
                    "initial_margin": "0.2",
                }
            }
        assert method == "private/get_positions"
        return {"result": []}

    monkeypatch.setattr(adapter, "_ensure_authenticated", _authenticated)
    monkeypatch.setattr(adapter, "_private_request", _private_request)

    with pytest.raises(RuntimeError, match="omitted available_funds"):
        asyncio.run(adapter._do_get_account_snapshot())


def test_delta_request_non_json_response_is_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 5xx proxy/HTML (non-JSON) response must surface a structured error, not
    # crash the caller (which only understands JSON).
    a = DeltaExchangeAdapter(environment="paper")
    a._api_key = "k"
    a._api_secret = "s"

    class _BadJSONResponse:
        status_code = 502

        def json(self) -> dict[str, Any]:
            raise ValueError("not json")

    class _Client:
        async def request(self, **_: Any) -> _BadJSONResponse:
            return _BadJSONResponse()

    a._client = _Client()
    result = asyncio.run(a._request("GET", "/v2/orders"))
    assert result["success"] is False
    assert result["error"]["code"] == 502
