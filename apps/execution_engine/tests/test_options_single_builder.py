from __future__ import annotations

import site
import sys
from datetime import UTC, datetime
from decimal import Decimal

import pytest

for site_path in site.getsitepackages():
    if site_path not in sys.path:
        sys.path.append(site_path)

from execution_engine.brokers.paper import PaperBroker
from execution_engine.config import (
    AccountState,
    ExecutionMode,
    OptionsConfig,
    UserExecutionConfig,
)
from execution_engine.models import ExecutionRequest, OrderCurrencyContext
from execution_engine.options_builder import OptionsOrderBuilder, to_options_intent
from execution_engine.options_single_builder import (
    OptionsSingleOrderBuilder,
    resolve_option_contract_spec,
)
from execution_engine.order_builder import OrderBuilder
from lib_strategy.signals.signal import Signal, SignalAction


def _signal(
    *,
    action: SignalAction = SignalAction.SHORT,
    entry_price: float = 240.0,
    metadata_overrides: dict[str, object] | None = None,
) -> Signal:
    metadata = {
        "underlying_symbol": "NIFTY",
        "underlying_asset_class": "index",
        "underlying_price": 22000.0,
        "expiry": "2026-04-09",
        "strike": 22000.0,
        "option_type": "CALL",
        "marked_level": 250.0,
        "session_date": "2026-04-02",
        "side_bucket": "CE",
        "entry_index": 1,
        "lot_size": 50,
        "contract_multiplier": 1,
        "price_timeframe": "3m",
    }
    if metadata_overrides:
        metadata.update(metadata_overrides)
    return Signal(
        strategy_id="options_test_strategy_v1",
        strategy_type="indicator",
        symbol="NIFTY:2026-04-09:22000:CALL",
        asset_class="options",
        action=action,
        confidence=0.8,
        timestamp=datetime(2026, 4, 2, 9, 18, tzinfo=UTC),
        entry_price=entry_price,
        stop_loss=250.0,
        take_profit=192.0,
        metadata=metadata,
    )


def _config_payload() -> dict:
    return {
        "user_id": "user-1",
        "strategy_id": "options_test_strategy_v1",
        "broker": "paper",
        "execution_mode": "options_single",
        "sizing": {
            "method": "risk_pct",
            "risk_pct": 0.01,
            "fixed_pct": 0.02,
            "fixed_amount": 1000.0,
            "max_position_pct": 0.25,
            "min_position_size": 1.0,
            "kelly_fraction": 0.25,
        },
    }


def test_user_execution_config_supports_options_single_roundtrip() -> None:
    payload = _config_payload()
    config = UserExecutionConfig.from_dict(payload)
    assert config.execution_mode == ExecutionMode.OPTIONS_SINGLE
    assert config.to_dict()["execution_mode"] == "options_single"


def test_resolve_option_contract_spec_reads_canonical_symbol_and_metadata() -> None:
    spec = resolve_option_contract_spec(_signal())
    assert spec.option_symbol == "NIFTY:2026-04-09:22000:CALL"
    assert spec.underlying_symbol == "NIFTY"
    assert spec.expiry == "2026-04-09"
    assert spec.strike == 22000.0
    assert spec.option_type == "CALL"
    assert spec.lot_size == 50


def test_resolve_option_contract_spec_requires_observed_contract_economics() -> None:
    with pytest.raises(ValueError, match="contract_multiplier"):
        resolve_option_contract_spec(_signal(metadata_overrides={"contract_multiplier": None}))

    with pytest.raises(ValueError, match="lot_size"):
        resolve_option_contract_spec(_signal(metadata_overrides={"lot_size": 1.5}))


def test_resolve_option_contract_spec_rejects_identity_conflict() -> None:
    with pytest.raises(ValueError, match="conflicts with canonical identity"):
        resolve_option_contract_spec(_signal(metadata_overrides={"strike": 22_100.0}))


def test_options_single_builder_builds_risk_sized_entry_intent() -> None:
    builder = OptionsSingleOrderBuilder()
    config = UserExecutionConfig.from_dict(_config_payload())
    account = AccountState(
        broker=config.broker,
        equity=100000.0,
        available_cash=100000.0,
        open_positions=0,
        daily_trades=0,
    )

    intent = builder.build_entry(
        signal=_signal(),
        config=config,
        account=account,
        market_data=None,
    )

    assert intent is not None
    assert intent.side == "SELL"
    assert intent.quantity == 100
    assert intent.spread_type == "options_single"
    assert intent.max_loss == 1000.0
    assert intent.metadata["contract_multiplier"] == 1
    assert intent.metadata["lot_size"] == 50
    assert intent.metadata["to_open_close"] == "to_open"


def test_order_builder_routes_options_single_entry_and_close() -> None:
    builder = OrderBuilder()
    signal = _signal()
    req = type(
        "Req",
        (),
        {
            "user_id": "user-1",
            "profile": {"equity": 100000.0},
            "user_strategy_config": _config_payload(),
            "signal": signal,
        },
    )()
    account = AccountState(
        broker=UserExecutionConfig.from_dict(_config_payload()).broker,
        equity=100000.0,
        available_cash=100000.0,
        open_positions=0,
        daily_trades=0,
        positions=[
            {
                "symbol": signal.symbol,
                "side": "SHORT",
                "quantity": 100,
                "entry_price": 240.0,
                "current_price": 240.0,
            }
        ],
    )

    entry_intents = builder.build(req, account=account, market_data=None)
    assert len(entry_intents) == 1
    assert entry_intents[0].side == "SELL"

    close_req = type(
        "Req",
        (),
        {
            "user_id": "user-1",
            "profile": {"equity": 100000.0},
            "user_strategy_config": _config_payload(),
            "signal": _signal(action=SignalAction.CLOSE, entry_price=200.0),
        },
    )()
    close_intents = builder.build(close_req, account=account, market_data=None)
    assert len(close_intents) == 1
    assert close_intents[0].side == "BUY"
    assert close_intents[0].quantity == 100


def test_options_single_builder_builds_partial_close_intent() -> None:
    builder = OptionsSingleOrderBuilder()
    config = UserExecutionConfig.from_dict(_config_payload())

    close_intent = builder.build_close(
        signal=_signal(
            action=SignalAction.CLOSE,
            entry_price=200.0,
            metadata_overrides={"partial_exit": True, "exit_qty_fraction": 0.50},
        ),
        config=config,
        position={
            "symbol": "NIFTY:2026-04-09:22000:CALL",
            "side": "SHORT",
            "quantity": 100,
        },
    )

    assert close_intent is not None
    assert close_intent.side == "BUY"
    assert close_intent.quantity == 50
    assert close_intent.metadata["partial_exit"] is True
    assert close_intent.metadata["exit_qty_fraction"] == 0.5
    assert close_intent.metadata["to_open_close"] == "to_close"


def test_options_single_builder_partial_close_falls_back_to_full_when_not_representable() -> None:
    builder = OptionsSingleOrderBuilder()
    config = UserExecutionConfig.from_dict(_config_payload())

    close_intent = builder.build_close(
        signal=_signal(
            action=SignalAction.CLOSE,
            entry_price=200.0,
            metadata_overrides={"partial_exit": True, "exit_qty_fraction": 0.50},
        ),
        config=config,
        position={
            "symbol": "NIFTY:2026-04-09:22000:CALL",
            "side": "SHORT",
            "quantity": 50,
        },
    )

    assert close_intent is not None
    assert close_intent.quantity == 50
    assert close_intent.metadata["partial_exit"] is False
    assert close_intent.metadata["exit_qty_fraction"] == 1.0


def test_paper_broker_tracks_single_leg_option_position_lifecycle() -> None:
    builder = OptionsSingleOrderBuilder()
    config = UserExecutionConfig.from_dict(_config_payload())
    account = AccountState(
        broker=config.broker,
        equity=100000.0,
        available_cash=100000.0,
        open_positions=0,
        daily_trades=0,
    )
    intent = builder.build_entry(
        signal=_signal(),
        config=config,
        account=account,
        market_data=None,
    )
    assert intent is not None
    currency_context = OrderCurrencyContext(
        account_currency="USD",
        settlement_currency="USD",
        account_to_settlement_rate=Decimal("1"),
        requested_at=datetime(2026, 4, 2, tzinfo=UTC),
        observed_at=datetime(2026, 4, 2, tzinfo=UTC),
        source="identity",
    )
    intent.currency_context = currency_context

    broker = PaperBroker(
        account_id="options-single-paper-account",
        starting_balance=100000.0,
        available_cash=100000.0,
        currency="USD",
        slippage_pct=0.0,
        commission_pct=0.0,
    )

    async def _run() -> tuple[object, object, object]:
        open_result = await broker.submit_options_order(intent)
        broker.set_price(intent.symbol, 200.0)
        mid_account = await broker.get_account_info()
        close_intent = builder.build_close(
            signal=_signal(action=SignalAction.CLOSE, entry_price=200.0),
            config=config,
            position={
                "symbol": intent.symbol,
                "side": "SHORT",
                "quantity": intent.quantity,
            },
        )
        assert close_intent is not None
        close_intent.currency_context = currency_context
        close_result = await broker.submit_options_order(close_intent)
        final_account = await broker.get_account_info()
        return open_result, close_result, mid_account, final_account

    import asyncio

    open_result, close_result, mid_account, final_account = asyncio.run(_run())

    assert open_result.success is True
    assert close_result.success is True
    assert len(mid_account.positions) == 1
    assert mid_account.positions[0].symbol == "NIFTY:2026-04-09:22000:CALL"
    assert mid_account.positions[0].side == "SHORT"
    assert mid_account.positions[0].quantity == 100
    assert mid_account.positions[0].unrealized_pnl == 4000.0
    assert final_account.positions == []
    assert final_account.realized_pnl == 4000.0


def test_options_builder_preserves_priced_spread_legs() -> None:
    spread = OptionsOrderBuilder(OptionsConfig()).build_spread(
        ExecutionMode.BULL_CALL,
        underlying_price=50_000.0,
        is_long=True,
        implied_volatility=0.6,
        risk_amount=5_000.0,
        contract_multiplier=100.0,
        underlying_symbol="BTC-USDC",
    )

    intent = to_options_intent("BTC-USDC", spread, "paper")

    assert intent.legs
    assert all(leg.premium is not None and leg.premium > 0 for leg in intent.legs)
    assert intent.metadata
    assert intent.metadata["contract_multiplier"] == spread.contract_multiplier
    assert intent.metadata["leg1_premium"] == intent.legs[0].premium
    assert intent.metadata["leg2_premium"] == intent.legs[1].premium


def test_order_builder_requires_explicit_spread_contract_multiplier() -> None:
    signal = Signal(
        strategy_id="options_spread_strategy_v1",
        strategy_type="indicator",
        symbol="BTC-USDC",
        asset_class="options",
        action=SignalAction.LONG,
        confidence=0.8,
        entry_price=50_000.0,
        metadata={"implied_volatility": 0.6},
    )
    request = ExecutionRequest(
        user_id="user-1",
        profile={},
        user_strategy_config={
            "broker": "paper",
            "execution_mode": "bull_call",
            "risk_amount": 5_000.0,
        },
        signal=signal,
    )

    assert OrderBuilder().build(request, account=None, market_data=None) == []

    signal.metadata["contract_multiplier"] = 100.0
    account = AccountState(
        broker=UserExecutionConfig.from_dict(
            {
                "user_id": "user-1",
                "strategy_id": signal.strategy_id,
                "broker": "paper",
            }
        ).broker,
        equity=100_000.0,
        available_cash=100_000.0,
    )
    intents = OrderBuilder().build(request, account=account, market_data=None)

    assert len(intents) == 1
    assert intents[0].metadata
    assert intents[0].metadata["contract_multiplier"] == 100.0
