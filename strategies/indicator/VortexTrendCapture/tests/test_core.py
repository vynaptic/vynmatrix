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


def test_vortex_cross_survives_equality_then_confirms_sma_trend():
    strategy = core(vortex_period="2", long_term_ma_period="2", atr_period="2", atr_threshold="1")
    bars = [bar(i, price) for i, price in enumerate([10, 9, 8, 9, 10, 5])]
    signals = strategy.run(bars)
    assert [(s.action, s.timestamp) for s in signals] == [
        (SignalAction.LONG, bars[4].timestamp),
        (SignalAction.CLOSE, bars[5].timestamp),
    ]


def test_vortex_mirror_cross_emits_short():
    strategy = core(vortex_period="2", long_term_ma_period="2", atr_period="2", atr_threshold="1")
    assert [
        s.action
        for s in strategy.run([bar(i, price) for i, price in enumerate([10, 11, 12, 11, 10])])
    ] == [SignalAction.SHORT]


def test_vortex_volatility_ceiling_blocks_entry():
    strategy = core(
        vortex_period="2", long_term_ma_period="2", atr_period="2", atr_threshold="0.001"
    )
    assert strategy.run([bar(i, price) for i, price in enumerate([10, 9, 8, 9, 10])]) == []
