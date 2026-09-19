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


def test_causal_adaptive_cross_updates_even_while_model_is_open():
    strategy = core(
        vol_calc_window="2",
        vol_ema_period="2",
        alpha_base_period="2",
        max_period_cap="5",
        lambda_decay="0",
        atr_period="2",
    )
    bars = [bar(i, price) for i, price in enumerate([10, 11, 10, 12, 13])]
    signals = strategy.run(bars)
    assert [(s.action, s.timestamp) for s in signals] == [(SignalAction.LONG, bars[3].timestamp)]
    assert strategy._indicators["BTC-USD"]["aema"] == pytest.approx(112 / 9)
    assert [s.action for s in strategy.run([bar(5, 8)])] == [SignalAction.CLOSE]


def test_adaptive_period_bounds_are_validated():
    with pytest.raises(ValueError, match="max_period_cap"):
        core(alpha_base_period="20", max_period_cap="10").initialize()


def test_open_checkpoint_requires_its_entry_time_trailing_distance():
    from lib_strategy.signals.pure_strategy import ModelStateContractError

    strategy = core(
        vol_calc_window="2",
        vol_ema_period="2",
        alpha_base_period="2",
        max_period_cap="5",
        lambda_decay="0",
        atr_period="2",
    )
    strategy.run([bar(i, price) for i, price in enumerate([10, 11, 10, 12, 13])])
    snapshot = strategy.serialize_model_state()
    assert snapshot["symbol_states"]["BTC-USD"]["position"] == 1
    del snapshot["symbol_states"]["BTC-USD"]["custom"]["trail_distance"]
    with pytest.raises(ModelStateContractError, match="trail_distance"):
        strategy.restore_model_state(snapshot)
    assert [s.action for s in strategy.run([bar(5, 8)])] == [SignalAction.CLOSE]
