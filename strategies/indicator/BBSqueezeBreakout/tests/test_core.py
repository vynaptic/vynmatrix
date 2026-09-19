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


def test_previous_squeeze_arms_current_breakout_and_midband_exit():
    strategy = core(
        bb_period="3", bb_devfactor="1", squeeze_lookback="3", use_volume_confirm="false"
    )
    bars = [bar(i, price) for i, price in enumerate([10, 10, 10, 10, 10, 13, 10])]
    signals = strategy.run(bars)
    assert [(s.action, s.timestamp) for s in signals] == [
        (SignalAction.LONG, bars[5].timestamp),
        (SignalAction.CLOSE, bars[6].timestamp),
    ]
    assert signals[0].stop_loss is None


def test_tenth_percentile_does_not_treat_every_rank_as_a_squeeze():
    strategy = core(
        bb_period="3",
        bb_devfactor="1",
        squeeze_lookback="3",
        squeeze_threshold_pct="10",
        use_volume_confirm="false",
    )
    assert strategy.run([bar(i, price) for i, price in enumerate([10, 10, 11, 13, 16, 20])]) == []


def test_volume_confirmation_requires_explicit_traded_volume_semantics():
    with pytest.raises(ValueError, match="volume_semantics"):
        core(use_volume_confirm="true").initialize()
