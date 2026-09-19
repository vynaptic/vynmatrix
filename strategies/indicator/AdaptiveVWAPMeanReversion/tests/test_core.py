from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


from lib_strategy.signals.loading import load_pure_strategy_core
from lib_strategy.signals.pure_strategy import MarketState
from lib_strategy.signals.signal import SignalAction


def bar(index, close, **values):
    payload = {
        "symbol": "BTC-USD",
        "timestamp": datetime(2026, 6, 10, tzinfo=UTC) + timedelta(days=index),
        "open": close,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": 10,
    }
    payload.update(values)
    return MarketState(**payload)


def core(**parameters):
    strategy = load_pure_strategy_core(Path(__file__).resolve().parents[1])
    return strategy(config=parameters)


def test_vwap_reversion_keeps_intended_initial_protection_and_mean_exit():
    strategy = core(
        vwap_period="2",
        std_dev_period="2",
        adx_period="2",
        rsi_period="2",
        atr_period="2",
        adx_weak_threshold="90",
        adx_strong_threshold="100",
        entry_std_threshold="0.5",
        atr_stop_mult="1",
        volume_semantics="traded",
        trade_direction_mode="long_only",
    )
    bars = [bar(i, price) for i, price in enumerate([10, 12, 10, 12, 8, 10])]
    signals = strategy.run(bars)
    assert [(s.action, s.timestamp) for s in signals] == [
        (SignalAction.LONG, bars[4].timestamp),
        (SignalAction.CLOSE, bars[5].timestamp),
    ]
    assert signals[0].stop_loss == 4.5
    assert signals[-1].metadata["exit_reason"] == "vwap_reversion"


def test_zero_volume_cannot_be_substituted_with_unweighted_price():
    strategy = core(volume_semantics="traded")
    assert strategy.run([bar(i, 10 + i % 3, volume=0) for i in range(80)]) == []


def test_existing_stop_remains_active_when_rolling_volume_disappears():
    strategy = core(
        vwap_period="2",
        std_dev_period="2",
        adx_period="2",
        rsi_period="2",
        atr_period="2",
        adx_weak_threshold="90",
        adx_strong_threshold="100",
        entry_std_threshold="0.5",
        atr_stop_mult="1",
        volume_semantics="traded",
        trade_direction_mode="long_only",
    )
    strategy.run([bar(i, price) for i, price in enumerate([10, 12, 10, 12, 8])])
    assert strategy.state_for("BTC-USD").position == 1
    assert strategy.run([bar(5, 7.5, volume=0)]) == []
    signals = strategy.run([bar(6, 3, volume=0)])
    assert [signal.action for signal in signals] == [SignalAction.CLOSE]
    assert signals[0].metadata["exit_reason"] == "trailing_stop"


@pytest.mark.parametrize("invalid", [None, "false", 0])
def test_open_checkpoint_requires_a_boolean_trailing_mode(invalid):
    from lib_strategy.signals.pure_strategy import ModelStateContractError

    strategy = core(
        vwap_period="2",
        std_dev_period="2",
        adx_period="2",
        rsi_period="2",
        atr_period="2",
        adx_weak_threshold="90",
        adx_strong_threshold="100",
        entry_std_threshold="0.5",
        atr_stop_mult="1",
        volume_semantics="traded",
        trade_direction_mode="long_only",
    )
    signals = strategy.run([bar(i, price) for i, price in enumerate([10, 12, 10, 12, 8])])
    assert [signal.action for signal in signals] == [SignalAction.LONG]
    snapshot = strategy.serialize_model_state()
    snapshot["symbol_states"]["BTC-USD"]["custom"]["tight_trail"] = invalid
    with pytest.raises(ModelStateContractError, match="tight_trail"):
        strategy.restore_model_state(snapshot)
