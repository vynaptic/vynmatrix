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


def test_williams_recovery_requires_trend_and_histogram_confirmation():
    strategy = core(
        trend_period="3",
        wr_period="3",
        macd_fast="2",
        macd_slow="3",
        macd_signal="2",
        atr_period="2",
        atr_stop_multiplier="1",
    )
    bars = [bar(i, price) for i, price in enumerate([10, 11, 12, 11, 10, 12, 7])]
    signals = strategy.run(bars)
    assert [(s.action, s.timestamp) for s in signals] == [
        (SignalAction.LONG, bars[5].timestamp),
        (SignalAction.CLOSE, bars[6].timestamp),
    ]


def test_flat_price_range_is_unavailable_without_division_error():
    strategy = core(
        trend_period="3",
        wr_period="3",
        macd_fast="2",
        macd_slow="3",
        macd_signal="2",
        atr_period="2",
    )
    assert strategy.run([bar(i, 10, high=10, low=10) for i in range(10)]) == []
