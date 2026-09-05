"""Contract tests for the Saxo OpenAPI money-moving boundary."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from execution_engine._execute import _attach_broker_instrument_identity
from execution_engine.broker_bridge import to_broker_order_intent
from execution_engine.config import BrokerType
from execution_engine.engine import ExecutionEngine
from execution_engine.execution_routing import ExecutionRouteResolver
from execution_engine.models import OrderIntent
from lib_application.db.models import (
    Base,
    Broker,
    Instrument,
    InstrumentBrokerSymbol,
    LinkedBrokerAccount,
    User,
)
from lib_common.config_validation import ExecutionDedupConfig, ExecutionRuntimeConfig
from lib_infrastructure.brokers.adapters.saxo import SaxoAdapter, SaxoSessionError
from lib_infrastructure.brokers.capabilities import get_broker_capabilities
from lib_infrastructure.brokers.factory import BrokerFactory, register_default_adapters
from lib_infrastructure.brokers.secrets import BrokerCredentials, ISecretsProvider
from lib_strategy.signals.signal import Signal, SignalAction


def _future_expiry(minutes: int = 20) -> str:
    return (datetime.now(tz=UTC) + timedelta(minutes=minutes)).isoformat()


def _credentials(*, token: str = "access-1", account_key: str = "account-key") -> dict[str, str]:
    return {
        "access_token": token,
        "access_token_expires_at": _future_expiry(),
        "account_key": account_key,
        "client_key": "client-key",
    }


def _connected_adapter(environment: str = "paper") -> SaxoAdapter:
    adapter = SaxoAdapter(environment=environment)
    adapter._connected = True
    adapter._client = object()  # type: ignore[assignment]
    adapter._access_token = "access-1"
    adapter._access_token_expires_at = datetime.now(tz=UTC) + timedelta(minutes=20)
    adapter._account_key = "account-key"
    adapter._client_key = "client-key"
    adapter._account_currency = "EUR"
    return adapter


def _saxo_catalogue_factory(
    *,
    include_mapping: bool,
    broker_instrument_id: str = "21",
) -> Any:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(
            User(
                user_id="saxo-user",
                email="saxo-user@example.com",
                base_ccy="EUR",
            )
        )
        session.add(Broker(broker_id=7, code="saxo", name="Saxo Bank", capabilities={}))
        session.add(
            LinkedBrokerAccount(
                account_id=41,
                user_id="saxo-user",
                broker_id=7,
                environment="paper",
                display_name="Saxo SIM",
                base_ccy="EUR",
                paper_initial_equity=10_000,
                paper_initial_cash=10_000,
            )
        )
        session.add(
            Instrument(
                instr_id=1,
                canonical="EUR/USD",
                asset_class="fx",
                settlement_currency="USD",
            )
        )
        if include_mapping:
            session.add(
                InstrumentBrokerSymbol(
                    instr_id=1,
                    broker_id=7,
                    broker_symbol="EURUSD",
                    broker_instrument_id=broker_instrument_id,
                    broker_instrument_type="FxSpot",
                )
            )
    return factory


def _saxo_signal(*, action: SignalAction = SignalAction.LONG) -> Signal:
    return Signal(
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol="EUR/USD",
        asset_class="fx",
        action=action,
        confidence=0.8,
        timestamp=datetime.now(tz=UTC),
        entry_price=1.16,
        instrument_id=1,
        external_signal_id=f"saxo-eurusd-{action.value.lower()}",
    )


def test_saxo_hosts_are_environment_specific() -> None:
    paper = SaxoAdapter(environment="paper")
    live = SaxoAdapter(environment="live")

    assert paper._base_url == "https://gateway.saxobank.com/sim/openapi"
    assert live._base_url == "https://gateway.saxobank.com/openapi"
    with pytest.raises(ValueError, match=r"paper.*live"):
        SaxoAdapter(environment="production")


def test_saxo_connect_verifies_exact_account_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SaxoAdapter(environment="paper")
    calls: list[tuple[str, str]] = []

    async def request(method: str, path: str, **_: Any) -> dict[str, Any]:
        calls.append((method, path))
        return {
            "AccountKey": "account-key",
            "ClientKey": "client-key",
            "Active": True,
            "Currency": "EUR",
        }

    monkeypatch.setattr(adapter, "_request", request)
    connected = asyncio.run(
        adapter.connect(
            api_key="app-key",
            api_secret="app-secret",
            **_credentials(),
        )
    )

    assert connected is True
    assert calls == [("GET", "/port/v1/accounts/account-key")]
    assert adapter._account_currency == "EUR"
    asyncio.run(adapter.disconnect())


def test_saxo_connect_blocks_expired_token_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SaxoAdapter(environment="live")

    async def forbidden_request(*_: Any, **__: Any) -> dict[str, Any]:
        pytest.fail("an expired Saxo token must be rejected before network I/O")

    monkeypatch.setattr(adapter, "_request", forbidden_request)
    connected = asyncio.run(
        adapter.connect(
            api_key="app-key",
            api_secret="app-secret",
            access_token="expired",
            access_token_expires_at=(datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
            account_key="account-key",
            client_key="client-key",
        )
    )

    assert connected is False
    assert adapter._client is None


def test_saxo_durable_loader_adopts_only_same_account_identity() -> None:
    adapter = _connected_adapter()
    adapter._access_token_expires_at = datetime.now(tz=UTC) + timedelta(seconds=10)

    async def replacement() -> BrokerCredentials:
        return BrokerCredentials(
            api_key="app-key",
            api_secret="app-secret",
            additional=_credentials(token="access-2"),
        )

    adapter.configure_credential_loader(replacement)
    asyncio.run(adapter._ensure_access_token())
    assert adapter._access_token == "access-2"

    adapter._access_token_expires_at = datetime.now(tz=UTC) + timedelta(seconds=10)

    async def wrong_account() -> BrokerCredentials:
        return BrokerCredentials(
            api_key="app-key",
            api_secret="app-secret",
            additional=_credentials(token="access-3", account_key="other-account"),
        )

    adapter.configure_credential_loader(wrong_account)
    with pytest.raises(SaxoSessionError, match="changed AccountKey or ClientKey"):
        asyncio.run(adapter._ensure_access_token())


def test_saxo_order_payload_requires_typed_catalogue_identity() -> None:
    adapter = _connected_adapter()
    payload = adapter._build_order_payload(
        {
            "symbol": "ASML:xams",
            "side": "buy",
            "quantity": "3",
            "order_type": "stop_limit",
            "execution_method": "spot",
            "time_in_force": "gtc",
            "limit_price": "650.25",
            "stop_price": "645.50",
            "client_order_id": "signal-123",
            "broker_instrument_id": "1636",
            "broker_instrument_type": "Stock",
        }
    )

    assert payload == {
        "AccountKey": "account-key",
        "Amount": 3,
        "AssetType": "Stock",
        "BuySell": "Buy",
        "ManualOrder": False,
        "OrderDuration": {"DurationType": "GoodTillCancel"},
        "OrderType": "StopLimit",
        "Uic": 1636,
        "WithAdvice": False,
        "ExternalReference": "signal-123",
        "OrderPrice": 650.25,
        "StopLimitPrice": 645.5,
    }

    with pytest.raises(ValueError, match="canonical positive integer"):
        adapter._build_order_payload(
            {
                "symbol": "ASML:xams",
                "side": "buy",
                "quantity": "1",
                "order_type": "market",
                "execution_method": "spot",
                "broker_instrument_type": "Stock",
            }
        )


@pytest.mark.parametrize(
    "invalid_uic",
    [True, False, 0, -1, "01", "+1", "1.0", "\u0661"],
)
def test_saxo_rejects_noncanonical_uic(invalid_uic: object) -> None:
    adapter = _connected_adapter()

    with pytest.raises((TypeError, ValueError), match="canonical positive integer"):
        adapter._build_order_payload(
            {
                "symbol": "ASML:xams",
                "side": "buy",
                "quantity": "1",
                "order_type": "market",
                "execution_method": "spot",
                "broker_instrument_id": invalid_uic,  # type: ignore[typeddict-item]
                "broker_instrument_type": "Stock",
            }
        )


def test_saxo_option_payload_requires_explicit_open_close() -> None:
    adapter = _connected_adapter()
    order = {
        "symbol": "SPX option",
        "side": "buy",
        "quantity": "1",
        "order_type": "limit",
        "execution_method": "options_single",
        "time_in_force": "day",
        "limit_price": "12.50",
        "broker_instrument_id": "98765",
        "broker_instrument_type": "StockIndexOption",
    }

    with pytest.raises(ValueError, match="to_open_close"):
        adapter._build_order_payload(order)

    order["to_open_close"] = "to_open"
    payload = adapter._build_order_payload(order)
    assert payload["ToOpenClose"] == "ToOpen"


def test_execution_bridge_preserves_saxo_catalogue_identity() -> None:
    payload = to_broker_order_intent(
        OrderIntent(
            broker_code="saxo",
            symbol="ASML:xams",
            side="BUY",
            quantity=2,
            metadata={
                "signal_id": "signal-1",
                "execution_mode": "spot",
                "broker_symbol": "ASML:xams",
                "broker_instrument_id": "1636",
                "broker_instrument_type": "Stock",
                "to_open_close": "to_open",
            },
        )
    )

    assert payload["symbol"] == "ASML:xams"
    assert payload["broker_instrument_id"] == "1636"
    assert payload["broker_instrument_type"] == "Stock"
    assert payload["to_open_close"] == "to_open"


def test_saxo_order_path_uses_exact_database_catalogue_identity() -> None:
    factory = _saxo_catalogue_factory(include_mapping=True)
    route_resolver = ExecutionRouteResolver(
        default_mode="paper",
        session_factory=factory,
        canonical_execution_store=None,
    )
    identity = route_resolver.resolve_broker_instrument(
        user_id="saxo-user",
        account_id=41,
        broker_type=BrokerType.SAXO,
        signal=_saxo_signal(),
    )
    intent = OrderIntent(
        broker_code="saxo",
        symbol="EUR/USD",
        side="BUY",
        quantity=1_000,
        order_type="market",
        metadata={"signal_id": "saxo-catalogue-route"},
    )

    _attach_broker_instrument_identity([intent], identity)
    wire_order = to_broker_order_intent(intent)
    payload = _connected_adapter()._build_order_payload(wire_order)

    assert intent.symbol == "EUR/USD"
    assert intent.metadata == {
        "signal_id": "saxo-catalogue-route",
        "broker_symbol": "EURUSD",
        "broker_instrument_id": "21",
        "broker_instrument_type": "FxSpot",
    }
    assert wire_order["symbol"] == "EURUSD"
    assert payload["Uic"] == 21
    assert payload["AssetType"] == "FxSpot"


def test_noncanonical_saxo_catalogue_identity_blocks_before_broker_resolution() -> None:
    factory = _saxo_catalogue_factory(
        include_mapping=True,
        broker_instrument_id="01",
    )
    route_resolver = ExecutionRouteResolver(
        default_mode="paper",
        session_factory=factory,
        canonical_execution_store=None,
    )

    with pytest.raises(ValueError, match="noncanonical saxo broker_instrument_id"):
        route_resolver.resolve_broker_instrument(
            user_id="saxo-user",
            account_id=41,
            broker_type=BrokerType.SAXO,
            signal=_saxo_signal(),
        )


def test_missing_saxo_catalogue_identity_blocks_before_broker_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _saxo_catalogue_factory(include_mapping=False)
    engine = ExecutionEngine(
        default_mode="paper",
        allow_live=False,
        session_factory=factory,
        runtime_config=ExecutionRuntimeConfig(
            risk_guard_enabled=False,
            dedup=ExecutionDedupConfig(use_memory_only=True),
        ),
    )
    broker_resolution_calls = 0

    async def forbidden_broker_resolution(*_: Any, **__: Any) -> None:
        nonlocal broker_resolution_calls
        broker_resolution_calls += 1
        pytest.fail("missing Saxo catalogue identity must block before broker I/O")

    monkeypatch.setattr(engine, "_get_broker", forbidden_broker_resolution)
    profile = {
        "broker": "saxo",
        "broker_account_id": 41,
        "sandbox": True,
        "live_enabled": False,
        "accounts": {
            "41": {
                "user_id": "saxo-user",
                "broker": "saxo",
                "environment": "paper",
                "status": "connected",
                "base_ccy": "EUR",
                "paper_initial_equity": 10_000,
                "paper_initial_cash": 10_000,
            }
        },
        "brokers": {"saxo": {"credential_ref": "saxo-sim-account-41"}},
        "_broker_route_snapshot": {
            "broker": "saxo",
            "broker_environment": "paper",
            "broker_account_id": 41,
            "credential_ref": "saxo-sim-account-41",
            "sandbox": True,
            "allowed_brokers": ["saxo"],
            "route_source": "scoring_policy",
            "live_enabled": False,
            "asset_class": "fx",
            "execution_mode": "spot",
        },
    }

    result = asyncio.run(
        engine.handle_signal(
            user_id="saxo-user",
            profile=profile,
            user_strategy_config={
                "broker": "saxo",
                "execution_mode": "spot",
                "mode": "paper",
            },
            signal=_saxo_signal(action=SignalAction.CLOSE),
        )
    )

    assert broker_resolution_calls == 0
    assert result.success is False
    assert result.execution_mode == "blocked"
    assert "has no saxo catalogue mapping" in str(result.error_message)


def test_saxo_precheck_disclaimer_blocks_placement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _connected_adapter()
    calls: list[str] = []

    async def request(method: str, path: str, **_: Any) -> dict[str, Any]:
        assert method == "POST"
        calls.append(path)
        return {
            "PreCheckResult": "Ok",
            "PreTradeDisclaimers": {
                "DisclaimerContext": "context",
                "DisclaimerTokens": ["token"],
            },
        }

    monkeypatch.setattr(adapter, "_request", request)
    result = asyncio.run(
        adapter.place_order(
            {
                "symbol": "ASML:xams",
                "side": "buy",
                "quantity": "1",
                "order_type": "market",
                "execution_method": "spot",
                "broker_instrument_id": "1636",
                "broker_instrument_type": "Stock",
            }
        )
    )

    assert calls == ["/trade/v2/orders/precheck"]
    assert result["success"] is False
    assert result["error_code"] == "pre_trade_disclaimer_required"


def test_saxo_placement_transport_is_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _connected_adapter()
    calls: list[str] = []

    async def request(method: str, path: str, **_: Any) -> dict[str, Any]:
        assert method == "POST"
        calls.append(path)
        if path.endswith("/precheck"):
            return {"PreCheckResult": "Ok"}
        request_context = httpx.Request("POST", adapter.LIVE_BASE_URL + path)
        raise httpx.ReadTimeout("uncertain placement", request=request_context)

    monkeypatch.setattr(adapter, "_request", request)
    result = asyncio.run(
        adapter.place_order(
            {
                "symbol": "ASML:xams",
                "side": "buy",
                "quantity": "1",
                "order_type": "market",
                "execution_method": "spot",
                "broker_instrument_id": "1636",
                "broker_instrument_type": "Stock",
            }
        )
    )

    assert calls == ["/trade/v2/orders/precheck", "/trade/v2/orders"]
    assert result["status"] == "pending"
    assert result["error_code"] == "order_transport_uncertain"
    assert adapter._last_order_request_at > 0


def test_saxo_trade_not_completed_with_order_id_remains_reconcilable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _connected_adapter()

    async def request(_method: str, path: str, **_: Any) -> dict[str, Any]:
        if path.endswith("/precheck"):
            return {"PreCheckResult": "Ok"}
        return {
            "OrderId": "67762872",
            "ErrorInfo": {
                "ErrorCode": "TradeNotCompleted",
                "Message": "Order state must be queried",
            },
        }

    monkeypatch.setattr(adapter, "_request", request)
    result = asyncio.run(
        adapter.place_order(
            {
                "symbol": "ASML:xams",
                "side": "buy",
                "quantity": "1",
                "order_type": "market",
                "execution_method": "spot",
                "broker_instrument_id": "1636",
                "broker_instrument_type": "Stock",
            }
        )
    )

    assert result["success"] is True
    assert result["status"] == "pending"
    assert result["broker_order_id"] == "67762872"


def test_saxo_order_audit_maps_partial_and_rejected_states() -> None:
    partial = SaxoAdapter._order_activity_result(
        {
            "OrderId": "1",
            "Amount": 10,
            "FilledAmount": 4,
            "AveragePrice": 100.25,
            "ActivityTime": "2026-07-25T10:00:00Z",
            "Status": "Fill",
            "SubStatus": "Confirmed",
        },
        fallback_order_id="1",
    )
    rejected = SaxoAdapter._order_activity_result(
        {
            "OrderId": "2",
            "Amount": 10,
            "Status": "Placed",
            "SubStatus": "Rejected",
        },
        fallback_order_id="2",
    )

    assert partial["status"] == "partially_filled"
    assert partial["filled_quantity"] == "4"
    assert partial["filled_price"] == "100.25"
    assert rejected["success"] is False
    assert rejected["status"] == "rejected"


def test_saxo_snapshot_uses_authoritative_broker_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _connected_adapter()
    balance = {
        "CalculationReliability": "Ok",
        "Currency": "EUR",
        "CashBalance": 5000,
        "CashAvailableForTrading": 4200,
        "CashBlocked": 800,
        "TotalValue": 12000,
        "MarginUsedByCurrentPositions": -1250,
    }

    async def fetch_balance() -> dict[str, Any]:
        return balance

    async def positions() -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(adapter, "_fetch_balance", fetch_balance)
    monkeypatch.setattr(adapter, "_do_get_positions", positions)
    snapshot = asyncio.run(adapter._do_get_account_snapshot())

    assert snapshot == {
        "balances": [
            {
                "currency": "EUR",
                "total": "5000",
                "available": "4200",
                "locked": "800",
            }
        ],
        "positions": [],
        "equity": [{"value": "12000", "currency": "EUR"}],
        "available_cash": [{"value": "4200", "currency": "EUR"}],
        "margin_used": [{"value": "1250", "currency": "EUR"}],
        "equity_source": "broker",
    }


def test_saxo_positions_preserve_broker_valuation_without_fabricated_realized_pnl() -> None:
    adapter = _connected_adapter()
    spot = adapter._position_result(
        {
            "DisplayAndFormat": {"Symbol": "ASML:xams", "Currency": "EUR"},
            "PositionBase": {
                "Amount": 2,
                "AssetType": "Stock",
                "OpenPrice": 620,
                "Status": "Open",
                "Uic": 1636,
            },
            "PositionView": {
                "CurrentPrice": 650,
                "ExposureInBaseCurrency": 1300,
                "ProfitLossOnTradeInBaseCurrency": 60,
            },
        }
    )
    future = adapter._position_result(
        {
            "DisplayAndFormat": {"Symbol": "FESXU6", "Currency": "EUR"},
            "PositionBase": {
                "Amount": -1,
                "AssetType": "ContractFutures",
                "OpenPrice": 5350,
                "Status": "Open",
                "Uic": 12345,
            },
            "PositionView": {
                "CurrentPrice": 5340,
                "ExposureInBaseCurrency": -53400,
                "ProfitLossOnTradeInBaseCurrency": 100,
            },
        }
    )

    assert spot is not None
    assert spot["quantity_unit"] == "asset"
    assert spot["notional_type"] == "spot"
    assert spot["gross_notional"] == "1300"
    assert future is not None
    assert future["side"] == "short"
    assert future["quantity_unit"] == "contracts"
    assert future["notional_type"] == "broker_mark_value"
    assert future["unrealized_pnl"] == "100"
    assert "realized_pnl" not in future


class _Secrets(ISecretsProvider):
    async def get_secret(self, _secret_ref: str) -> str | None:
        return None


def test_saxo_is_registered_in_the_single_broker_registry() -> None:
    factory = BrokerFactory(_Secrets())
    register_default_adapters(factory)
    capabilities = get_broker_capabilities("saxo")

    assert "saxo" in factory.get_registered_brokers()
    assert capabilities is not None
    assert capabilities.has_native_paper is True
    assert capabilities.paper_base_url == "https://gateway.saxobank.com/sim/openapi"
