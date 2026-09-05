from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from execution_engine.broker_bridge import (
    BrokerBridge,
    BrokerValuationError,
    from_broker_order_result,
    to_broker_order_intent,
)
from execution_engine.brokers.base import AccountInfo, Position
from execution_engine.config import BrokerType
from execution_engine.metrics.fx_rates import FXRateUnavailableError, ObservedFXRate
from execution_engine.models import OrderIntent
from execution_engine.state_provider import StateProvider


class _FakeInfraAdapter:
    capabilities = {  # noqa: RUF012
        "asset_classes": ["crypto"],
        "execution_methods": ["spot"],
        "multi_leg_orders": False,
        "paper_trading": {"type": "sandbox", "base_url": "https://sandbox"},
        "max_leverage": 1.0,
        "exact_fill_retrieval": True,
    }

    def configure_settlement_currency(self, currency: str) -> None:
        self.settlement_currency = currency

    async def get_account_balance(self):  # type: ignore[no-untyped-def]
        return [
            {"currency": "USD", "available": "1000", "total": "1000", "locked": "0"},
            {"currency": "BTC", "available": "0.5", "total": "0.5", "locked": "0"},
        ]

    async def get_positions(self):  # type: ignore[no-untyped-def]
        return [
            {
                "symbol": "BTC-USD",
                "quantity": "0.5",
                "quantity_unit": "asset",
                "side": "long",
                "entry_price": "40000",
                "current_price": "42000",
                "unrealized_pnl": "1000",
                "realized_pnl": "0",
                "asset_currency": "BTC",
                "quote_currency": "USD",
                "settlement_currency": "USD",
                "pnl_currency": "USD",
                "notional_currency": "USD",
                "gross_notional": "21000",
                "notional_type": "spot",
                "is_quanto": False,
            }
        ]

    async def get_account_snapshot(self):  # type: ignore[no-untyped-def]
        return {
            "balances": await self.get_account_balance(),
            "positions": await self.get_positions(),
            "equity": [],
            "available_cash": [],
            "margin_used": [],
            "equity_source": "spot_holdings",
        }

    async def place_order(self, order):  # type: ignore[no-untyped-def]
        return {
            "success": True,
            "status": "filled",
            "broker_order_id": "cb-1",
            "filled_quantity": order["quantity"],
            "filled_price": "42000",
            "commission": "1.25",
            "commission_currency": "USD",
        }

    async def place_multi_leg_order(self, legs):  # type: ignore[no-untyped-def]
        return {
            "success": True,
            "status": "filled",
            "broker_order_id": "multi-1",
            "filled_quantity": "1",
            "filled_price": "1.0",
            "commission": "0.0",
        }

    async def cancel_order(self, order_id: str) -> bool:
        return order_id == "cb-1"

    async def get_order_status(self, order_id: str):  # type: ignore[no-untyped-def]
        return {
            "success": True,
            "status": "filled",
            "broker_order_id": order_id,
            "filled_quantity": "0.5",
            "filled_price": "42000",
            "commission": "1.25",
            "commission_currency": "USD",
        }

    async def get_fills(self, order_id: str):  # type: ignore[no-untyped-def]
        return [
            {
                "trade_id": "cb-trade-1",
                "broker_order_id": order_id,
                "symbol": "BTC-USD",
                "side": "buy",
                "quantity": "0.5",
                "price": "42000.125",
                "fee_amount": "1.25",
                "fee_currency": "USD",
                "timestamp": "2026-07-25T10:15:30.123456Z",
                "venue": "coinbase",
            }
        ]

    async def get_open_orders(self, symbol=None):  # type: ignore[no-untyped-def]
        return [{"broker_order_id": "open-1", "symbol": symbol or "BTC-USD"}]


class _FakeFactory:
    def __init__(self) -> None:
        self.adapter = _FakeInfraAdapter()
        self.calls: list[tuple[str, str, str]] = []
        self.disconnected: list[tuple[str, str, str]] = []

    async def get_adapter(self, broker_code: str, credential_ref: str, environment: str):
        self.calls.append((broker_code, credential_ref, environment))
        return self.adapter

    async def disconnect_adapter(self, broker_code: str, credential_ref: str, environment: str):
        self.disconnected.append((broker_code, credential_ref, environment))


def test_coinbase_bridge_uses_infrastructure_factory(monkeypatch) -> None:
    factory = _FakeFactory()
    monkeypatch.setattr(
        "execution_engine.broker_bridge.create_execution_broker_factory",
        lambda secrets_provider: factory,
    )

    bridge = BrokerBridge(
        broker_type=BrokerType.COINBASE,
        environment="paper",
        credential_ref="coinbase-paper",
        credentials={"api_key": "k", "api_secret": "s", "passphrase": "p"},
        account_currency="USD",
        settlement_currency="USD",
    )

    async def _run() -> tuple[bool, object, object, object, object, bool]:
        connected = await bridge.connect()
        account = await bridge.get_account_info()
        result = await bridge.submit_order(
            OrderIntent(
                broker_code="coinbase",
                symbol="BTC-USD",
                side="BUY",
                quantity=0.1,
                order_type="market",
                metadata={"signal_id": "sig-1", "execution_mode": "spot"},
            )
        )
        status = await bridge.get_order_status("cb-1")
        open_orders = await bridge.get_open_orders("BTC-USD")
        cancelled = await bridge.cancel_order("cb-1")
        await bridge.disconnect()
        return connected, account, result, status, open_orders, cancelled

    connected, account, result, status, open_orders, cancelled = asyncio.run(_run())

    assert connected is True
    assert factory.calls == [("coinbase", "coinbase-paper", "paper")]
    assert factory.adapter.settlement_currency == "USD"
    assert account.account_id == "coinbase-paper"
    assert account.equity == 22000.0
    assert result.success is True
    assert result.order_id == "cb-1"
    assert result.commission == Decimal("1.25")
    assert result.commission_currency == "USD"
    assert status.order_id == "cb-1"
    assert open_orders == [{"broker_order_id": "open-1", "symbol": "BTC-USD"}]
    assert cancelled is True
    assert factory.disconnected == [("coinbase", "coinbase-paper", "paper")]


def test_bridge_explicitly_converts_capabilities_and_exact_fill_wire_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _FakeFactory()
    monkeypatch.setattr(
        "execution_engine.broker_bridge.create_execution_broker_factory",
        lambda secrets_provider: factory,
    )
    bridge = BrokerBridge(
        broker_type=BrokerType.COINBASE,
        environment="paper",
        credential_ref="coinbase-paper",
        credentials={"api_key": "k", "api_secret": "s"},
        account_currency="USD",
        settlement_currency="USD",
    )

    async def _run():  # type: ignore[no-untyped-def]
        await bridge.connect()
        return bridge.capabilities, await bridge.get_fills("cb-order-1")

    capabilities, fills = asyncio.run(_run())

    assert capabilities.supports_spot is True
    assert capabilities.supports_perpetual is False
    assert capabilities.supports_exact_fill_retrieval is True
    assert len(fills) == 1
    assert fills[0].trade_id == "cb-trade-1"
    assert fills[0].order_id == "cb-order-1"
    assert fills[0].quantity == Decimal("0.5")
    assert fills[0].price == Decimal("42000.125")
    assert fills[0].commission == Decimal("1.25")
    assert fills[0].commission_currency == "USD"
    assert fills[0].timestamp == datetime(2026, 7, 25, 10, 15, 30, 123456, tzinfo=UTC)


@pytest.mark.parametrize("missing_field", ["trade_id", "timestamp", "fee_currency"])
def test_bridge_exact_fill_conversion_has_no_identity_or_economics_defaults(
    monkeypatch: pytest.MonkeyPatch,
    missing_field: str,
) -> None:
    factory = _FakeFactory()
    original = factory.adapter.get_fills

    async def _incomplete(order_id: str):  # type: ignore[no-untyped-def]
        rows = await original(order_id)
        rows[0].pop(missing_field)
        return rows

    factory.adapter.get_fills = _incomplete  # type: ignore[method-assign]
    monkeypatch.setattr(
        "execution_engine.broker_bridge.create_execution_broker_factory",
        lambda secrets_provider: factory,
    )
    bridge = BrokerBridge(
        broker_type=BrokerType.COINBASE,
        environment="paper",
        credential_ref="coinbase-paper",
        credentials={"api_key": "k", "api_secret": "s"},
        account_currency="USD",
        settlement_currency="USD",
    )

    async def _run():  # type: ignore[no-untyped-def]
        await bridge.connect()
        return await bridge.get_fills("cb-order-1")

    with pytest.raises(ValueError, match=r"Broker fill|timestamp"):
        asyncio.run(_run())


def test_bridge_converts_all_cash_balances_to_account_currency(monkeypatch) -> None:
    factory = _FakeFactory()

    async def _inr_balances():  # type: ignore[no-untyped-def]
        return [
            {"currency": "INR", "available": "10000", "total": "10000", "locked": "0"},
            {"currency": "EUR", "available": "50", "total": "50", "locked": "0"},
        ]

    async def _no_positions():  # type: ignore[no-untyped-def]
        return []

    factory.adapter.get_account_balance = _inr_balances  # type: ignore[method-assign]
    factory.adapter.get_positions = _no_positions  # type: ignore[method-assign]
    monkeypatch.setattr(
        "execution_engine.broker_bridge.create_execution_broker_factory",
        lambda secrets_provider: factory,
    )

    class _ObservedProvider:
        def get_rate(self, **kwargs):  # type: ignore[no-untyped-def]
            assert kwargs["base_currency"] == "INR"
            assert kwargs["quote_currency"] == "EUR"
            return ObservedFXRate(
                base_currency="INR",
                quote_currency="EUR",
                rate=Decimal("0.011"),
                observed_at=datetime.now(tz=UTC),
                source="cross:EUR:ecb_reference",
            )

    bridge = BrokerBridge(
        broker_type=BrokerType.COINBASE,
        environment="paper",
        credential_ref="coinbase-paper",
        credentials={"api_key": "k", "api_secret": "s"},
        account_currency="EUR",
        settlement_currency="EUR",
        fx_rate_provider=_ObservedProvider(),
    )

    async def _account():  # type: ignore[no-untyped-def]
        await bridge.connect()
        return await bridge.get_account_info()

    account = asyncio.run(_account())

    assert account.currency == "EUR"
    assert account.available_balance == 160.0
    assert account.equity == 160.0


def test_bridge_uses_authoritative_broker_equity_and_contract_notional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _FakeFactory()

    async def _snapshot():  # type: ignore[no-untyped-def]
        return {
            "balances": [],
            "positions": [
                {
                    "symbol": "BTC-PERP",
                    "quantity": "2",
                    "quantity_unit": "contracts",
                    "side": "long",
                    "entry_price": "24000",
                    "current_price": "25000",
                    "unrealized_pnl": "2000",
                    "realized_pnl": "100",
                    "margin_used": "1000",
                    "quote_currency": "USD",
                    "settlement_currency": "USD",
                    "pnl_currency": "USD",
                    "notional_currency": "USD",
                    "gross_notional": "50000",
                    "contract_multiplier": "1",
                    "notional_type": "broker_notional",
                    "is_quanto": False,
                }
            ],
            "equity": [{"value": "10000", "currency": "USD"}],
            "available_cash": [{"value": "5000", "currency": "USD"}],
            "margin_used": [{"value": "1000", "currency": "USD"}],
            "equity_source": "broker",
        }

    factory.adapter.get_account_snapshot = _snapshot  # type: ignore[method-assign]
    monkeypatch.setattr(
        "execution_engine.broker_bridge.create_execution_broker_factory",
        lambda secrets_provider: factory,
    )
    bridge = BrokerBridge(
        broker_type=BrokerType.DERIBIT,
        environment="live",
        credential_ref="deribit-live",
        credentials={"api_key": "k", "api_secret": "s"},
        account_currency="USD",
        settlement_currency="USD",
    )

    async def _account():  # type: ignore[no-untyped-def]
        await bridge.connect()
        return await bridge.get_account_info()

    account = asyncio.run(_account())

    assert account.equity == 10000.0
    assert account.available_balance == 5000.0
    assert account.margin_used == 1000.0
    assert account.unrealized_pnl == 2000.0
    assert account.realized_pnl == 100.0
    assert account.positions[0].quantity == 2.0
    assert account.positions[0].quantity_unit == "contracts"
    assert account.positions[0].gross_notional == 50000.0
    assert account.positions[0].contract_multiplier == 1.0


def test_bridge_blocks_when_authoritative_equity_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _FakeFactory()

    async def _snapshot():  # type: ignore[no-untyped-def]
        return {
            "balances": [],
            "positions": [],
            "equity": [],
            "available_cash": [],
            "margin_used": [],
            "equity_source": "unavailable",
        }

    factory.adapter.get_account_snapshot = _snapshot  # type: ignore[method-assign]
    monkeypatch.setattr(
        "execution_engine.broker_bridge.create_execution_broker_factory",
        lambda secrets_provider: factory,
    )
    bridge = BrokerBridge(
        broker_type=BrokerType.DELTA,
        environment="live",
        credential_ref="delta-live",
        credentials={"api_key": "k", "api_secret": "s"},
        account_currency="USD",
        settlement_currency="USD",
    )

    async def _account():  # type: ignore[no-untyped-def]
        await bridge.connect()
        return await bridge.get_account_info()

    with pytest.raises(BrokerValuationError, match="does not expose authoritative account equity"):
        asyncio.run(_account())


def test_bridge_preserves_unknown_spot_pnl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _FakeFactory()

    async def _snapshot():  # type: ignore[no-untyped-def]
        return {
            "balances": [
                {"currency": "USD", "available": "1000", "total": "1000", "locked": "0"},
                {"currency": "BTC", "available": "0.5", "total": "0.5", "locked": "0"},
            ],
            "positions": [
                {
                    "symbol": "BTC-USD",
                    "quantity": "0.5",
                    "quantity_unit": "asset",
                    "side": "long",
                    "entry_price": "40000",
                    "current_price": "42000",
                    "asset_currency": "BTC",
                    "quote_currency": "USD",
                    "settlement_currency": "USD",
                    "notional_currency": "USD",
                    "gross_notional": "21000",
                    "notional_type": "spot",
                    "is_quanto": False,
                }
            ],
            "equity": [],
            "available_cash": [],
            "margin_used": [],
            "equity_source": "spot_holdings",
        }

    factory.adapter.get_account_snapshot = _snapshot  # type: ignore[method-assign]
    monkeypatch.setattr(
        "execution_engine.broker_bridge.create_execution_broker_factory",
        lambda secrets_provider: factory,
    )
    bridge = BrokerBridge(
        broker_type=BrokerType.COINBASE,
        environment="live",
        credential_ref="coinbase-live",
        credentials={"api_key": "k", "api_secret": "s"},
        account_currency="USD",
        settlement_currency="USD",
    )

    async def _account():  # type: ignore[no-untyped-def]
        await bridge.connect()
        return await bridge.get_account_info()

    account = asyncio.run(_account())

    assert account.positions[0].unrealized_pnl is None
    assert account.positions[0].realized_pnl is None
    assert account.unrealized_pnl is None
    assert account.realized_pnl is None


@pytest.mark.parametrize(
    ("removed_field", "error_pattern"),
    [
        ("entry_price", "omitted a positive entry price"),
        ("unrealized_pnl", "omitted unrealized_pnl"),
        ("gross_notional", "omitted gross_notional"),
    ],
)
def test_bridge_rejects_incomplete_derivative_valuation(
    monkeypatch: pytest.MonkeyPatch,
    removed_field: str,
    error_pattern: str,
) -> None:
    factory = _FakeFactory()
    position = {
        "symbol": "BTC-PERP",
        "quantity": "2",
        "quantity_unit": "contracts",
        "side": "long",
        "entry_price": "24000",
        "current_price": "25000",
        "unrealized_pnl": "2000",
        "realized_pnl": "100",
        "margin_used": "1000",
        "quote_currency": "USD",
        "settlement_currency": "USD",
        "pnl_currency": "USD",
        "notional_currency": "USD",
        "gross_notional": "50000",
        "notional_type": "broker_notional",
        "is_quanto": False,
    }
    position.pop(removed_field)

    async def _snapshot():  # type: ignore[no-untyped-def]
        return {
            "balances": [],
            "positions": [position],
            "equity": [{"value": "10000", "currency": "USD"}],
            "available_cash": [{"value": "5000", "currency": "USD"}],
            "margin_used": [{"value": "1000", "currency": "USD"}],
            "equity_source": "broker",
        }

    factory.adapter.get_account_snapshot = _snapshot  # type: ignore[method-assign]
    monkeypatch.setattr(
        "execution_engine.broker_bridge.create_execution_broker_factory",
        lambda secrets_provider: factory,
    )
    bridge = BrokerBridge(
        broker_type=BrokerType.DERIBIT,
        environment="live",
        credential_ref="deribit-live",
        credentials={"api_key": "k", "api_secret": "s"},
        account_currency="USD",
        settlement_currency="USD",
    )

    async def _account():  # type: ignore[no-untyped-def]
        await bridge.connect()
        return await bridge.get_account_info()

    with pytest.raises(BrokerValuationError, match=error_pattern):
        asyncio.run(_account())


def test_bridge_requires_observed_fx_for_account_valuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _FakeFactory()

    async def _snapshot():  # type: ignore[no-untyped-def]
        return {
            "balances": [],
            "positions": [],
            "equity": [{"value": "10000", "currency": "USD"}],
            "available_cash": [{"value": "5000", "currency": "USD"}],
            "margin_used": [{"value": "0", "currency": "USD"}],
            "equity_source": "broker",
        }

    factory.adapter.get_account_snapshot = _snapshot  # type: ignore[method-assign]
    monkeypatch.setattr(
        "execution_engine.broker_bridge.create_execution_broker_factory",
        lambda secrets_provider: factory,
    )
    bridge = BrokerBridge(
        broker_type=BrokerType.IBKR,
        environment="live",
        credential_ref="ibkr-live",
        credentials={"api_key": "k", "api_secret": "s"},
        account_currency="EUR",
        settlement_currency="USD",
    )

    async def _account():  # type: ignore[no-untyped-def]
        await bridge.connect()
        return await bridge.get_account_info()

    with pytest.raises(FXRateUnavailableError, match="observed USD/EUR FX rate"):
        asyncio.run(_account())


def test_broker_result_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="Unsupported broker order status"):
        from_broker_order_result(
            {
                "success": True,
                "status": "mystery",
                "broker_order_id": "order-1",
            }
        )


def test_broker_result_rejects_malformed_supplied_timestamp() -> None:
    with pytest.raises(ValueError, match="Invalid broker order timestamp"):
        from_broker_order_result(
            {
                "success": True,
                "status": "submitted",
                "broker_order_id": "order-1",
                "timestamp": "not-a-timestamp",
            }
        )


def test_broker_result_rejects_naive_supplied_timestamp() -> None:
    with pytest.raises(ValueError, match="must include a timezone"):
        from_broker_order_result(
            {
                "success": True,
                "status": "submitted",
                "broker_order_id": "order-1",
                "timestamp": "2026-07-25T12:00:00",
            }
        )


def test_bridge_positions_remain_available_when_equity_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _FakeFactory()

    async def _positions():  # type: ignore[no-untyped-def]
        return [
            {
                "symbol": "BTCUSDT",
                "quantity": "2",
                "quantity_unit": "contracts",
                "side": "long",
                "entry_price": "60000",
                "mark_price": "61000",
                "unrealized_pnl": "2",
                "realized_pnl": "25",
                "margin_used": "1200",
                "quote_currency": "USDT",
                "settlement_currency": "USDT",
                "pnl_currency": "USDT",
                "notional_currency": "USDT",
                "gross_notional": "122",
                "contract_multiplier": "0.001",
                "notional_type": "vanilla",
                "is_quanto": False,
            }
        ]

    factory.adapter.get_positions = _positions  # type: ignore[method-assign]
    monkeypatch.setattr(
        "execution_engine.broker_bridge.create_execution_broker_factory",
        lambda secrets_provider: factory,
    )
    bridge = BrokerBridge(
        broker_type=BrokerType.DELTA,
        environment="live",
        credential_ref="delta-live",
        credentials={"api_key": "k", "api_secret": "s"},
        account_currency="USD",
        settlement_currency="USDT",
    )

    async def _positions_only():  # type: ignore[no-untyped-def]
        await bridge.connect()
        return await bridge.get_positions()

    positions = asyncio.run(_positions_only())

    assert positions[0].symbol == "BTCUSDT"
    assert positions[0].gross_notional == 122.0
    assert positions[0].notional_currency == "USDT"


def test_state_provider_preserves_canonical_position_valuation_fields() -> None:
    class _CanonicalBroker:
        async def get_account_info(self) -> AccountInfo:
            return AccountInfo(
                broker=BrokerType.DERIBIT,
                account_id="deribit-live",
                equity=10_000.0,
                available_balance=5_000.0,
                margin_used=1_000.0,
                unrealized_pnl=2_000.0,
                realized_pnl=100.0,
                positions=[
                    Position(
                        symbol="BTC-PERP",
                        side="LONG",
                        quantity=2.0,
                        entry_price=24_000.0,
                        current_price=25_000.0,
                        unrealized_pnl=2_000.0,
                        realized_pnl=100.0,
                        margin_used=1_000.0,
                        liquidation_price=15_000.0,
                        quantity_unit="contracts",
                        contract_multiplier=1.0,
                        gross_notional=50_000.0,
                        notional_currency="USD",
                    )
                ],
                balances=[
                    {
                        "currency": "USD",
                        "total": "5000",
                        "available": "4900",
                        "locked": "100",
                    }
                ],
            )

    state = asyncio.run(
        StateProvider().get_account_state(
            user_id="user-1",
            broker=_CanonicalBroker(),  # type: ignore[arg-type]
            profile={"broker": "deribit"},
            allow_profile_fallback=False,
        )
    )

    assert state.positions == [
        {
            "symbol": "BTC-PERP",
            "side": "LONG",
            "quantity": 2.0,
            "entry_price": 24_000.0,
            "current_price": 25_000.0,
            "unrealized_pnl": 2_000.0,
            "realized_pnl": 100.0,
            "margin_used": 1_000.0,
            "liquidation_price": 15_000.0,
            "quantity_unit": "contracts",
            "contract_multiplier": 1.0,
            "leverage": None,
            "contract_type": None,
            "gross_notional": 50_000.0,
            "notional_currency": "USD",
        }
    ]
    assert state.balances == [
        {"currency": "USD", "total": "5000", "available": "4900", "locked": "100"}
    ]


def test_state_provider_preserves_explicitly_unknown_profile_pnl() -> None:
    state = StateProvider.account_from_profile(
        {
            "broker": "coinbase",
            "equity": 10_000,
            "available_cash": 10_000,
            "margin_used": 0,
            "unrealized_pnl": None,
            "broker_account_id": 101,
            "base_ccy": "USD",
            "last_synced_at": datetime(2026, 3, 7, tzinfo=UTC).isoformat(),
        },
        BrokerType.COINBASE,
    )

    assert state.unrealized_pnl is None


def test_to_broker_order_intent_translates_stop_orders() -> None:
    payload = to_broker_order_intent(
        OrderIntent(
            broker_code="coinbase",
            symbol="BTC-USD",
            side="SELL",
            quantity=0.2,
            order_type="stop",
            stop_price=39500.0,
            metadata={"signal_id": "sig-2", "execution_mode": "spot"},
        )
    )

    assert payload["order_type"] == "stop_limit"
    assert payload["stop_price"] == "39500.0"
    assert payload["limit_price"] == "39500.0"
    assert payload["client_order_id"] == "sig-2"


def _intent(metadata: dict, **kw: object) -> OrderIntent:
    return OrderIntent(
        broker_code="coinbase",
        symbol="BTC-USD",
        side="SELL",
        quantity=0.2,
        order_type="market",
        metadata=metadata,
        **kw,  # type: ignore[arg-type]
    )


def test_protective_legs_get_stable_distinct_client_order_ids() -> None:
    # EX-3: protective legs carry only parent_signal_id + purpose (no signal_id).
    # They must still get a stable, leg-distinct client_order_id, otherwise the
    # broker mints a fresh random id per retry and duplicates resting orders.
    entry = to_broker_order_intent(_intent({"signal_id": "sig-1"}))
    stop = to_broker_order_intent(_intent({"parent_signal_id": "sig-1", "purpose": "stop_loss"}))
    take = to_broker_order_intent(_intent({"parent_signal_id": "sig-1", "purpose": "take_profit"}))

    assert entry["client_order_id"] == "sig-1"
    assert stop["client_order_id"] == "sig-1:stop_loss"
    assert take["client_order_id"] == "sig-1:take_profit"
    # All three legs are non-empty and distinct (no collision, no random fallback).
    ids = {entry["client_order_id"], stop["client_order_id"], take["client_order_id"]}
    assert len(ids) == 3
    assert "" not in ids


def test_explicit_client_order_id_wins() -> None:
    payload = to_broker_order_intent(
        _intent({"client_order_id": "explicit-id", "signal_id": "sig-1"})
    )
    assert payload["client_order_id"] == "explicit-id"


def test_client_order_id_empty_when_no_identity() -> None:
    # No signal_id / client_order_id / (parent_signal_id + purpose) -> empty.
    payload = to_broker_order_intent(_intent({"execution_mode": "spot"}))
    assert payload["client_order_id"] == ""
