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


def test_prior_channel_breakout_and_bar_count_exit():
    strategy = core(
        atr_period="2", lookback="3", atr_thresh="0.8", trail_atr_mult="3", max_hold_days="2"
    )
    signals = strategy.run([bar(i, 100 + i) for i in range(8)])
    assert [(s.action, s.timestamp) for s in signals] == [
        (SignalAction.LONG, bar(5, 105).timestamp),
        (SignalAction.CLOSE, bar(7, 107).timestamp),
    ]
    assert signals[-1].metadata["exit_reason"] == "maximum_holding_bars"
    assert signals[0].stop_loss < signals[0].entry_price


def test_short_breakout_is_symmetric_and_magnitude_gate_allows_it():
    strategy = core(
        atr_period="2", lookback="3", atr_thresh="0.8", trail_atr_mult="3", max_hold_days="2"
    )
    signals = strategy.run([bar(i, 100 - i) for i in range(8)])
    assert [s.action for s in signals] == [SignalAction.SHORT, SignalAction.CLOSE]
    assert signals[0].stop_loss > signals[0].entry_price


def test_disabled_time_exit_holds_until_protection_is_breached():
    strategy = core(
        atr_period="2",
        lookback="3",
        atr_thresh="0.8",
        trail_atr_mult="3",
        max_hold_days="1",
        use_time_exit="false",
    )
    assert [s.action for s in strategy.run([bar(i, 100 + i) for i in range(9)])] == [
        SignalAction.LONG
    ]
