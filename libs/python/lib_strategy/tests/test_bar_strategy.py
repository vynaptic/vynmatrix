"""Behavioral contracts shared by the native bar strategy ports."""

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

from lib_strategy.signals.bar_strategy import BarSignalStrategy
from lib_strategy.signals.pure_strategy import MarketState, ModelStateContractError
from lib_strategy.signals.signal import SignalAction


class ThresholdCore(BarSignalStrategy):
    DEFAULT_STRATEGY_ID = "threshold_test_v1"
    STREAM_FIELDS: ClassVar[dict[str, str]] = {"seen": "integer"}

    def configure(self):
        return self.integer("period", 2)

    def create_indicators(self):
        return {"seen": 0}

    def on_bar(self, bar, indicators):
        indicators["seen"] += 1
        if not self.can_decide(bar.symbol):
            return
        if self.state_for(bar.symbol).position:
            self.trail(bar, distance=1, intrabar=True)
        elif bar.close > 10:
            self.enter(bar, 1, stop_loss=bar.close - 2)
        elif bar.close < 9:
            self.enter(bar, -1, stop_loss=bar.close + 2)


def bar(index, close, symbol="BTC-USD", **overrides):
    values = {
        "symbol": symbol,
        "timestamp": datetime(2026, 6, 10, tzinfo=UTC) + timedelta(minutes=index),
        "open": close,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": 10,
        "metadata": {"price_source": "recorded", "price_timeframe": "1m"},
    }
    values.update(overrides)
    return MarketState(**values)


def test_duplicate_and_invalid_bars_cannot_advance_warmup_or_emit():
    core = ThresholdCore()
    signals = core.run([bar(0, 10), bar(0, 11), bar(1, float("nan")), bar(2, 11)])
    assert [s.action for s in signals] == [SignalAction.LONG]
    assert signals[0].timestamp == bar(2, 11).timestamp
    assert signals[0].metadata["price_source"] == "recorded"
    assert core._indicators["BTC-USD"]["seen"] == 2


def test_invalid_ohlc_and_negative_volume_do_not_change_model_state():
    core = ThresholdCore()
    signals = core.run([bar(0, 10), bar(1, 11, high=9), bar(2, 11, volume=-1)])
    assert signals == []
    assert not core.warmup_complete("BTC-USD")


def test_intrabar_stop_uses_preexisting_level_before_close_ratchet():
    core = ThresholdCore()
    signals = core.run([bar(0, 10), bar(1, 11), bar(2, 13, low=10), bar(3, 12, low=11.5)])
    assert [s.action for s in signals] == [SignalAction.LONG, SignalAction.CLOSE]
    assert signals[-1].timestamp == bar(3, 12).timestamp
    assert core.state_for("BTC-USD").position == 0
    assert core.state_for("BTC-USD").custom == {}


def test_restart_and_redelivery_preserve_decisions_and_signal_ids():
    prefix = [bar(0, 10), bar(1, 11), bar(2, 12)]
    original = ThresholdCore(config={"strategy_version": "1.0.0"})
    original.run(prefix)
    snapshot = original.serialize_model_state()
    restarted = ThresholdCore(config={"strategy_version": "1.0.0"})
    restarted.bootstrap_history(prefix)
    assert restarted.state_for("BTC-USD").position == 0
    restarted.restore_model_state(snapshot)
    continuation = [prefix[-1], bar(3, 10), bar(4, 8)]
    left, right = original.run(continuation), restarted.run(continuation)
    assert [(s.action, s.timestamp, s.external_signal_id) for s in left] == [
        (s.action, s.timestamp, s.external_signal_id) for s in right
    ]
    assert [s.action for s in right] == [SignalAction.CLOSE, SignalAction.SHORT]
    assert original.serialize_model_state() == restarted.serialize_model_state()


def test_interleaved_symbols_have_independent_warmup_and_positions():
    core = ThresholdCore()
    signals = core.run([bar(0, 10), bar(0, 10, "ETH-USD"), bar(1, 11), bar(1, 8, "ETH-USD")])
    assert [(s.symbol, s.action) for s in signals] == [
        ("BTC-USD", SignalAction.LONG),
        ("ETH-USD", SignalAction.SHORT),
    ]


def test_long_only_mode_suppresses_short_without_advancing_position():
    core = ThresholdCore(config={"trade_direction_mode": "long_only"})
    assert core.run([bar(0, 10), bar(1, 8)]) == []
    assert core.state_for("BTC-USD").position == 0


def test_restore_rejects_missing_protection_and_forbidden_direction():
    core = ThresholdCore(config={"strategy_version": "1.0.0"})
    core.run([bar(0, 10), bar(1, 11)])
    snapshot = core.serialize_model_state()
    snapshot["symbol_states"]["BTC-USD"]["custom"] = {}
    with pytest.raises(ModelStateContractError, match="stop"):
        core.restore_model_state(snapshot)


def test_evaluation_boundary_flattens_model_but_keeps_warmed_indicators():
    core = ThresholdCore()
    core.run([bar(0, 10), bar(1, 11)])
    core.apply_flat_model_evaluation_boundary(("BTC-USD",))
    assert core.state_for("BTC-USD").position == 0
    assert core.warmup_complete("BTC-USD")
    assert [s.action for s in core.run([bar(2, 12)])] == [SignalAction.LONG]


@pytest.mark.parametrize(
    "config",
    [{"period": "2.5"}, {"period": True}, {"period": "0"}, {"trade_direction_mode": "typo"}],
)
def test_configuration_is_validated_before_processing(config):
    with pytest.raises((TypeError, ValueError), match=r"period|trade_direction_mode"):
        ThresholdCore(config=config).initialize()


@pytest.mark.parametrize(
    ("configured", "expected_label", "expected_days"),
    [(None, "1d", 1.0), ("5d", "5d", 5.0), ("4h", "4h", 1 / 6)],
)
def test_entries_and_exits_preserve_configured_feedback_horizon(
    configured, expected_label, expected_days
):
    from lib_strategy.signals.adapters.scoring import build_scoring_view
    from lib_strategy.signals.emitter import HttpSignalEmitter

    config = {} if configured is None else {"evaluation_horizon": configured}
    signals = ThresholdCore(config=config).run([bar(0, 10), bar(1, 11), bar(2, 8), bar(3, 8)])
    assert [signal.action for signal in signals] == [
        SignalAction.LONG,
        SignalAction.CLOSE,
        SignalAction.SHORT,
    ]
    for signal in signals:
        assert signal.horizon == expected_label
        assert signal.horizon_days == pytest.approx(expected_days)
        assert build_scoring_view(signal).horizon_days == pytest.approx(expected_days)
        wire = HttpSignalEmitter(base_url="http://scoring.test")._build_payload(signal)
        assert wire["insight"]["horizon"] == expected_label
        assert wire["context"]["horizon_days"] == pytest.approx(expected_days)


@pytest.mark.parametrize("horizon", ["", "0d", "-1d", "nand", "infd", "unknown", " 5d", True, None])
def test_invalid_feedback_horizon_is_rejected_before_processing(horizon):
    with pytest.raises((ValueError, TypeError), match="evaluation_horizon"):
        ThresholdCore(config={"evaluation_horizon": horizon}).initialize()
