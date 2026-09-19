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


def test_range_extreme_entry_and_mean_exit():
    strategy = core(z_period="3", adx_period="2", adx_filter_level="100", trail_percent="0.5")
    signals = strategy.run([bar(i, price) for i, price in enumerate([10, 12, 10, 12, 9, 11])])
    assert [(s.action, s.timestamp) for s in signals] == [
        (SignalAction.LONG, bar(4, 9).timestamp),
        (SignalAction.CLOSE, bar(5, 11).timestamp),
    ]
    assert signals[-1].metadata["exit_reason"] == "mean_reversion"


def test_flat_series_produces_no_division_error_or_trade():
    assert core(z_period="3", adx_period="2").run([bar(i, 10) for i in range(20)]) == []


def test_adx_gate_blocks_the_same_price_extreme():
    strategy = core(z_period="3", adx_period="2", adx_filter_level="0.1", trail_percent="0.5")
    assert strategy.run([bar(i, price) for i, price in enumerate([10, 12, 10, 12, 9, 11])]) == []
