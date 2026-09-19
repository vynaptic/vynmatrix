from datetime import UTC, datetime, timedelta
from pathlib import Path


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


def test_strict_bullish_body_engulfing_at_low_rsi_then_trail_exit():
    bars = [
        bar(0, 20),
        bar(1, 10),
        bar(2, 9, open=9.5, high=10),
        bar(3, 9.6, open=8.9, low=8.5),
        bar(4, 9),
    ]
    signals = core(rsi_period="2").run(bars)
    assert [(s.action, s.timestamp) for s in signals] == [
        (SignalAction.LONG, bars[3].timestamp),
        (SignalAction.CLOSE, bars[4].timestamp),
    ]


def test_strict_bearish_body_engulfing_at_high_rsi():
    bars = [
        bar(0, 20),
        bar(1, 30),
        bar(2, 31, open=30.5, low=30),
        bar(3, 30.4, open=31.1, high=31.5),
    ]
    assert [s.action for s in core(rsi_period="2").run(bars)] == [SignalAction.SHORT]


def test_body_touch_without_engulfing_is_not_a_signal():
    bars = [bar(0, 20), bar(1, 10), bar(2, 9, open=9.5, high=10), bar(3, 9.5, open=8.9, low=8.5)]
    assert core(rsi_period="2").run(bars) == []
