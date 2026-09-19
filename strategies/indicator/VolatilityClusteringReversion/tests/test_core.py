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


def test_exact_cluster_length_fades_decline_when_long_term_trend_is_positive():
    strategy = core(
        vol_window="2",
        vol_stats_window="3",
        vol_cluster_threshold_factor="0.1",
        vol_cluster_days_trigger="2",
        trend_filter_sma_window="30",
        atr_window_sl="2",
        atr_multiplier_sl="1",
    )
    prices = [90] * 20 + [91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 100, 101, 100, 105, 99]
    bars = [bar(i, price) for i, price in enumerate(prices)]
    signals = strategy.run(bars)
    assert [(s.action, s.timestamp) for s in signals] == [(SignalAction.LONG, bars[-1].timestamp)]


def test_flat_returns_do_not_create_volatility_clusters():
    assert core().run([bar(i, 100) for i in range(100)]) == []
