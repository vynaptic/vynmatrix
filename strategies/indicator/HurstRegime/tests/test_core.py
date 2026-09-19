from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lib_strategy.signals.loading import load_pure_strategy_core
from lib_strategy.signals.pure_strategy import MarketState


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


def test_source_indicator_chain_declares_full_macd_cross_warmup():
    strategy = core()
    strategy.initialize()
    assert strategy.warmup_bars_needed == 35


def test_flat_hurst_does_not_invent_a_trend_or_mean_reversion_signal():
    assert core().run([bar(i, 100) for i in range(100)]) == []


def test_regime_thresholds_and_estimator_sample_count_are_validated():
    with pytest.raises(ValueError, match="threshold"):
        core(hurst_reversion_threshold="0.7", hurst_trend_threshold="0.5").initialize()
    with pytest.raises(ValueError, match="hurst_period"):
        core(hurst_period="5").initialize()
