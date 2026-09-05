from __future__ import annotations

import asyncio
import base64
import json
from decimal import Decimal
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from lib_infrastructure.brokers.adapters import coinbase as coinbase_module
from lib_infrastructure.brokers.adapters.coinbase import CoinbaseAdapter


def _urlsafe_b64decode(value: str) -> dict:
    padding = "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(value + padding).decode("utf-8"))


def _private_key_pem() -> str:
    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


def test_coinbase_adapter_uses_advanced_trade_hosts() -> None:
    paper = CoinbaseAdapter(environment="paper")
    live = CoinbaseAdapter(environment="live")

    assert paper.SANDBOX_BASE_URL == "https://api-sandbox.coinbase.com"
    assert live.LIVE_BASE_URL == "https://api.coinbase.com"
    assert paper._base_url == "https://api-sandbox.coinbase.com"
    assert live._base_url == "https://api.coinbase.com"


def test_coinbase_exact_fills_preserve_trade_identity_fee_and_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = CoinbaseAdapter(environment="live")
    calls: list[dict[str, str]] = []

    async def _request(
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        assert method == "GET"
        assert path == "/api/v3/brokerage/orders/historical/fills"
        calls.append(dict(params or {}))
        if len(calls) == 1:
            return {
                "fills": [
                    {
                        "trade_id": "trade-101",
                        "order_id": "order-7",
                        "product_id": "BTC-USDC",
                        "side": "BUY",
                        "size": "0.12500000",
                        "price": "64000.25",
                        "commission": "1.75",
                        "trade_time": "2026-07-25T10:15:30.123456Z",
                    }
                ],
                "cursor": "page-2",
            }
        return {
            "fills": [
                {
                    "trade_id": "trade-102",
                    "order_id": "order-7",
                    "product_id": "BTC-USDC",
                    "side": "BUY",
                    "size": "0.025",
                    "price": "64001",
                    "commission": "0",
                    "trade_time": "2026-07-25T10:15:31Z",
                }
            ]
        }

    monkeypatch.setattr(adapter, "_request", _request)

    fills = asyncio.run(adapter._do_get_fills("order-7"))

    assert calls == [
        {"order_ids": "order-7", "limit": "100"},
        {"order_ids": "order-7", "limit": "100", "cursor": "page-2"},
    ]
    assert [fill["trade_id"] for fill in fills] == ["trade-101", "trade-102"]
    assert fills[0]["quantity"] == "0.12500000"
    assert fills[0]["price"] == "64000.25"
    assert fills[0]["fee_amount"] == "1.75"
    assert fills[0]["fee_currency"] == "USDC"
    assert fills[0]["timestamp"] == "2026-07-25T10:15:30.123456+00:00"
    assert fills[0]["venue"] == "coinbase"


@pytest.mark.parametrize("missing_field", ["commission", "trade_time"])
def test_coinbase_exact_fills_fail_closed_when_economics_are_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    missing_field: str,
) -> None:
    adapter = CoinbaseAdapter(environment="live")
    row: dict[str, Any] = {
        "trade_id": "trade-incomplete",
        "order_id": "order-8",
        "product_id": "BTC-USD",
        "side": "SELL",
        "size": "0.1",
        "price": "65000",
        "commission": "2.0",
        "trade_time": "2026-07-25T10:15:30Z",
    }
    row.pop(missing_field)

    async def _request(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"fills": [row]}

    monkeypatch.setattr(adapter, "_request", _request)

    with pytest.raises(RuntimeError, match=f"omitted {missing_field}"):
        asyncio.run(adapter._do_get_fills("order-8"))


def test_coinbase_symbol_normalization_preserves_selected_settlement_currency() -> None:
    adapter = CoinbaseAdapter(environment="paper")

    assert adapter._normalize_symbol("BTCUSD") == "BTC-USD"
    assert adapter._normalize_symbol("BTC-USDC") == "BTC-USDC"
    assert adapter._normalize_symbol("ETHUSDC") == "ETH-USDC"


def test_coinbase_adapter_builds_bearer_jwt_headers() -> None:
    adapter = CoinbaseAdapter(environment="live")
    adapter._api_key = "organizations/test-org/apiKeys/test-key"
    adapter._api_secret = _private_key_pem()

    headers = adapter._build_auth_headers(
        method="GET",
        path="/api/v3/brokerage/accounts",
    )

    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Bearer ")
    assert "CB-ACCESS-KEY" not in headers
    assert "CB-ACCESS-SIGN" not in headers
    assert "CB-ACCESS-TIMESTAMP" not in headers

    token = headers["Authorization"].split(" ", 1)[1]
    encoded_header, encoded_payload, encoded_signature = token.split(".")
    decoded_header = _urlsafe_b64decode(encoded_header)
    decoded_payload = _urlsafe_b64decode(encoded_payload)

    assert decoded_header["alg"] == "ES256"
    assert decoded_header["kid"] == "organizations/test-org/apiKeys/test-key"
    assert decoded_payload["iss"] == "cdp"
    assert decoded_payload["sub"] == "organizations/test-org/apiKeys/test-key"
    assert decoded_payload["uri"] == "GET api.coinbase.com/api/v3/brokerage/accounts"
    assert encoded_signature


def test_place_order_extracts_order_id_from_success_response() -> None:
    # Regression: Coinbase Advanced Trade's create-order response nests the
    # created order under "success_response" (no top-level "order" key — that
    # shape is only returned by the GET historical-order endpoint). Reading the
    # wrong key left broker_order_id=None on every placement, which would break
    # order tracking, status polling, reconciliation, and cancellation in live.
    adapter = CoinbaseAdapter(environment="paper")
    adapter.configure_settlement_currency("USD")

    # Market BUYs are quote-denominated: the adapter derives quote_size from the
    # product's current price, so a product payload with a valid price is required
    # before the create-order request (and its response parse) is ever reached.
    product_info = {
        "price": "50000",
        "base_increment": "0.00000001",
        "quote_increment": "0.01",
    }

    async def _fake_product_info(_product_id: str) -> dict[str, Any]:
        return product_info

    adapter._get_product_info = _fake_product_info  # type: ignore[method-assign]

    create_response = {
        "success": True,
        "success_response": {
            "order_id": "f898eaf4-6ffc-47be-a159-7ff292e5cdcf",
            "product_id": "BTC-USD",
            "side": "BUY",
            "client_order_id": "abc-123",
        },
        "order_configuration": {"market_market_ioc": {"base_size": "0.0001"}},
    }

    submitted_payloads: list[dict[str, Any]] = []

    async def _fake_request(_method: str, _path: str, **_kwargs: Any) -> dict:
        submitted_payloads.append(_kwargs["json_body"])
        return create_response

    adapter._request = _fake_request  # type: ignore[method-assign]

    # Market orders poll for the async fill; return the fallback (the parsed
    # create response) immediately, capturing the order id it was invoked with
    # to prove the id was extracted from success_response before polling.
    awaited_order_ids: list[str] = []

    async def _fake_await_market_fill(broker_order_id: str, fallback: Any) -> Any:
        awaited_order_ids.append(broker_order_id)
        return fallback

    adapter._await_market_fill = _fake_await_market_fill  # type: ignore[method-assign]

    order: dict[str, Any] = {
        "symbol": "BTC-USD",
        "side": "buy",
        "order_type": "market",
        "quantity": "0.0001",
        "client_order_id": "abc-123",
    }

    result = asyncio.run(adapter._do_place_order(order))

    assert result["success"] is True
    assert result["broker_order_id"] == "f898eaf4-6ffc-47be-a159-7ff292e5cdcf"
    assert result["client_order_id"] == "abc-123"
    assert awaited_order_ids == ["f898eaf4-6ffc-47be-a159-7ff292e5cdcf"]
    assert submitted_payloads[0]["order_configuration"] == {
        "market_market_ioc": {"quote_size": "5.00"}
    }


def test_get_order_status_preserves_total_fee_decimal_and_quote_currency() -> None:
    adapter = CoinbaseAdapter(environment="paper")

    async def _fake_request(_method: str, _path: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "order": {
                "order_id": "cb-fee-1",
                "product_id": "BTC-EUR",
                "status": "FILLED",
                "filled_size": "0.015",
                "average_filled_price": "61234.56",
                "total_fees": "1.230000000000000007",
            }
        }

    adapter._request = _fake_request  # type: ignore[method-assign]

    result = asyncio.run(adapter._do_get_order_status("cb-fee-1"))

    assert result["commission"] == "1.230000000000000007"
    assert Decimal(result["commission"]) == Decimal("1.230000000000000007")
    assert result["commission_currency"] == "EUR"


def test_commission_detail_total_takes_precedence_over_total_fees() -> None:
    adapter = CoinbaseAdapter(environment="paper")

    async def _fake_request(_method: str, _path: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "order": {
                "order_id": "cb-fee-2",
                "product_id": "BTC-USDC",
                "status": "FILLED",
                "filled_size": "0.01",
                "average_filled_price": "60000",
                "total_fees": "9.99",
                "commission_detail_total": {"total_commission": "0.75"},
            }
        }

    adapter._request = _fake_request  # type: ignore[method-assign]

    result = asyncio.run(adapter._do_get_order_status("cb-fee-2"))

    assert result["commission"] == "0.75"
    assert result["commission_currency"] == "USDC"


def test_market_sell_uses_only_base_size_precision() -> None:
    adapter = CoinbaseAdapter(environment="paper")
    adapter.configure_settlement_currency("USD")

    async def _fake_product_info(_product_id: str) -> dict[str, Any]:
        return {
            "price": "50000.123",
            "base_increment": "0.001",
            "quote_increment": "100",
        }

    submitted: list[dict[str, Any]] = []

    async def _fake_request(_method: str, _path: str, **kwargs: Any) -> dict[str, Any]:
        submitted.append(kwargs["json_body"])
        return {
            "success": True,
            "success_response": {"order_id": "sell-order"},
        }

    async def _fake_await_market_fill(_order_id: str, fallback: Any) -> Any:
        return fallback

    adapter._get_product_info = _fake_product_info  # type: ignore[method-assign]
    adapter._request = _fake_request  # type: ignore[method-assign]
    adapter._await_market_fill = _fake_await_market_fill  # type: ignore[method-assign]

    result = asyncio.run(
        adapter._do_place_order(
            {
                "symbol": "BTC-USD",
                "side": "sell",
                "order_type": "market",
                "quantity": "0.3039",
                "client_order_id": "sell-client",
            }
        )
    )

    assert result["success"] is True
    assert submitted[0]["order_configuration"] == {"market_market_ioc": {"base_size": "0.303"}}


def test_coinbase_positions_use_account_scoped_settlement_product() -> None:
    adapter = CoinbaseAdapter(environment="paper")
    adapter.configure_settlement_currency("USDC")
    requested_products: list[str] = []

    async def _fake_product_info(product_id: str) -> dict[str, Any]:
        requested_products.append(product_id)
        return {"price": "50000"}

    adapter._get_product_info = _fake_product_info  # type: ignore[method-assign]

    positions = asyncio.run(
        adapter._positions_from_balances(
            [
                {"currency": "USDC", "total": "1000", "available": "1000", "locked": "0"},
                {"currency": "BTC", "total": "0.04", "available": "0.04", "locked": "0"},
            ]
        )
    )

    assert requested_products == ["BTC-USDC"]
    assert positions == [
        {
            "symbol": "BTC-USDC",
            "quantity": "0.04",
            "quantity_unit": "asset",
            "side": "long",
            "current_price": "50000",
            "asset_currency": "BTC",
            "quote_currency": "USDC",
            "settlement_currency": "USDC",
            "notional_currency": "USDC",
            "gross_notional": "2000.00",
            "contract_multiplier": "1",
            "notional_type": "spot",
            "is_quanto": False,
        }
    ]


def test_coinbase_order_rejects_product_outside_scoped_settlement() -> None:
    adapter = CoinbaseAdapter(environment="paper")
    adapter.configure_settlement_currency("USDC")

    result = asyncio.run(
        adapter._do_place_order(
            {
                "symbol": "BTC-USD",
                "side": "buy",
                "order_type": "market",
                "quantity": "0.01",
            }
        )
    )

    assert result["success"] is False
    assert "does not settle in USDC" in str(result["error_message"])


def test_coinbase_positions_fail_closed_without_scoped_settlement() -> None:
    adapter = CoinbaseAdapter(environment="paper")

    with pytest.raises(RuntimeError, match="requires an explicit settlement currency"):
        asyncio.run(adapter._positions_from_balances([]))


def test_coinbase_adapter_rejects_settlement_scope_mutation() -> None:
    adapter = CoinbaseAdapter(environment="paper")
    adapter.configure_settlement_currency("USDC")
    adapter.configure_settlement_currency("USDC")

    with pytest.raises(ValueError, match="already scoped to USDC"):
        adapter.configure_settlement_currency("USD")


# ---------------------------------------------------------------------------
# _await_market_fill: poll to a TERMINAL status; cancel the remainder on timeout
# so a late fill can never create an unprotected (naked) position.
# ---------------------------------------------------------------------------


def _fill_poll_adapter(
    statuses: list[dict[str, Any]], *, cancel_result: bool = True
) -> tuple[CoinbaseAdapter, list[str]]:
    """Adapter whose _do_get_order_status pops from ``statuses`` sequentially.

    The last entry is re-served once the list is exhausted (final-state fetch).
    Returns the adapter and the list of cancelled order ids.
    """
    adapter = CoinbaseAdapter(environment="paper")
    cancelled: list[str] = []

    async def _fake_status(_order_id: str) -> dict[str, Any]:
        if len(statuses) > 1:
            return statuses.pop(0)
        return statuses[0]

    async def _fake_cancel(order_id: str) -> bool:
        cancelled.append(order_id)
        return cancel_result

    adapter._do_get_order_status = _fake_status  # type: ignore[method-assign]
    adapter._do_cancel_order = _fake_cancel  # type: ignore[method-assign]
    return adapter, cancelled


def _fallback_result() -> dict[str, Any]:
    return {"success": True, "broker_order_id": "oid", "status": "new", "filled_quantity": "0"}


@pytest.fixture
def _fast_fill_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coinbase_module, "_MARKET_FILL_POLL_ATTEMPTS", 2)
    monkeypatch.setattr(coinbase_module, "_MARKET_FILL_POLL_DELAY_S", 0.0)


@pytest.mark.usefixtures("_fast_fill_poll")
def test_await_market_fill_does_not_return_on_non_terminal_partial_fill() -> None:
    # C1b regression: a partial fill on a still-OPEN order must NOT end the poll —
    # exits would be sized to the partial while the remainder fills unprotected.
    adapter, cancelled = _fill_poll_adapter(
        [
            {"success": True, "status": "working", "filled_quantity": "0.03"},
            {"success": True, "status": "filled", "filled_quantity": "0.07"},
        ]
    )

    result = asyncio.run(adapter._await_market_fill("oid", _fallback_result()))

    assert result["status"] == "filled"
    assert result["filled_quantity"] == "0.07"
    assert cancelled == []


@pytest.mark.usefixtures("_fast_fill_poll")
def test_await_market_fill_timeout_cancels_then_reports_raced_fill() -> None:
    # C1a regression: on timeout the order is still LIVE at Coinbase. The adapter
    # must cancel the remainder; if the cancel raced a fill, the REAL filled
    # result is returned so the caller still places correctly-sized exits.
    adapter, cancelled = _fill_poll_adapter(
        [
            {"success": True, "status": "working", "filled_quantity": "0"},
            {"success": True, "status": "working", "filled_quantity": "0"},
            # final-state fetch after the cancel: the order actually filled
            {"success": True, "status": "filled", "filled_quantity": "0.07"},
        ],
        cancel_result=False,  # cancel rejected because the order already filled
    )

    result = asyncio.run(adapter._await_market_fill("oid", _fallback_result()))

    assert cancelled == ["oid"]
    assert result["status"] == "filled"
    assert result["filled_quantity"] == "0.07"


@pytest.mark.usefixtures("_fast_fill_poll")
def test_await_market_fill_timeout_cancel_reports_partial_fill() -> None:
    # Timeout → cancel succeeded with a partial fill: the partial quantity is a
    # live position, so it must be reported for exit sizing (never filled=0).
    adapter, cancelled = _fill_poll_adapter(
        [
            {"success": True, "status": "working", "filled_quantity": "0.03"},
            {"success": True, "status": "working", "filled_quantity": "0.03"},
            {"success": True, "status": "canceled", "filled_quantity": "0.03"},
        ]
    )

    result = asyncio.run(adapter._await_market_fill("oid", _fallback_result()))

    assert cancelled == ["oid"]
    assert result["status"] == "canceled"
    assert result["filled_quantity"] == "0.03"


@pytest.mark.usefixtures("_fast_fill_poll")
def test_await_market_fill_returns_last_seen_when_final_fetch_fails() -> None:
    adapter, cancelled = _fill_poll_adapter(
        [
            {"success": True, "status": "working", "filled_quantity": "0.02"},
            {"success": True, "status": "working", "filled_quantity": "0.02"},
            {"success": False, "status": "rejected", "filled_quantity": "0"},
        ]
    )

    result = asyncio.run(adapter._await_market_fill("oid", _fallback_result()))

    # Cancel was still attempted; the best-known (last successful) state is
    # returned rather than the create-time fallback with filled=0.
    assert cancelled == ["oid"]
    assert result["status"] == "working"
    assert result["filled_quantity"] == "0.02"
