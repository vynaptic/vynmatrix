"""Checkpoint contracts reject corruption before altering live calculations."""

import copy

import pytest

from lib_indicators import AverageTrueRange, ExponentialMovingAverage, SimpleMovingAverage, Vortex
from lib_indicators.streaming_statistics import RollingStatistics, WilderAverage


@pytest.mark.parametrize(
    "factory",
    [
        SimpleMovingAverage,
        ExponentialMovingAverage,
        AverageTrueRange,
        RollingStatistics,
        WilderAverage,
    ],
)
def test_ready_indicator_cannot_restore_a_missing_value(factory):
    indicator = factory(3)
    for price in (10, 11, 9, 12, 11):
        if isinstance(indicator, AverageTrueRange):
            indicator.update(price + 1, price - 1, price)
        else:
            indicator.update(price)
    snapshot = indicator.snapshot_state()
    corrupt = copy.deepcopy(snapshot)
    key = (
        "mean"
        if isinstance(indicator, RollingStatistics)
        else "value"
        if isinstance(indicator, WilderAverage)
        else "_value"
    )
    corrupt["state"][key] = None
    with pytest.raises(ValueError, match="checkpoint"):
        indicator.restore_state(corrupt)
    assert indicator.snapshot_state() == snapshot


def test_ema_checkpoint_rejects_sample_count_that_disagrees_with_seed():
    ema = ExponentialMovingAverage(3)
    ema.update(10)
    snapshot = ema.snapshot_state()
    snapshot["state"]["_samples"] = 12
    with pytest.raises(ValueError, match="checkpoint"):
        ema.restore_state(snapshot)
    assert ema.update(12) is None
    assert ema.update(14) == 12


def test_vortex_becomes_unavailable_when_the_entire_window_has_zero_range():
    vortex = Vortex(2)
    for high, low, close in [(11, 9, 10), (12, 10, 11), (13, 11, 12)]:
        vortex.update(high, low, close)
    assert vortex.is_ready
    for _ in range(3):
        vortex.update(12, 12, 12)
    assert vortex.vi_plus is None
    assert vortex.vi_minus is None
    assert not vortex.is_ready


@pytest.mark.parametrize("corruption", ["previous", "negative_loss", "sample_count"])
def test_rsi_checkpoint_rejects_inconsistent_recursive_state(corruption):
    from lib_indicators.oscillators import RelativeStrengthIndex

    rsi = RelativeStrengthIndex(2)
    for close in [10, 12, 10]:
        rsi.update(close)
    before = rsi.snapshot_state()
    corrupt = copy.deepcopy(before)
    if corruption == "previous":
        corrupt["state"]["_previous"] = None
    elif corruption == "negative_loss":
        corrupt["state"]["_gains"]["state"]["value"] = 1
        corrupt["state"]["_losses"]["state"]["value"] = -1
    else:
        corrupt["state"]["_gains"]["state"]["_samples"] += 1
    with pytest.raises(ValueError, match="checkpoint"):
        rsi.restore_state(corrupt)
    assert rsi.snapshot_state() == before


@pytest.mark.parametrize("kind", ["adx", "macd", "stochastic"])
def test_composite_checkpoint_rejects_missing_ready_output(kind):
    from lib_indicators.oscillators import MACD, DirectionalMovement
    from lib_indicators.regime import Stochastic

    indicator = {
        "adx": DirectionalMovement(2),
        "macd": MACD(2, 3, 2),
        "stochastic": Stochastic(2, 2, 2),
    }[kind]
    for close in [10, 12, 10, 14, 15, 12, 14]:
        if kind == "macd":
            indicator.update(close)
        else:
            indicator.update(close + 1, close - 1, close)
    before = indicator.snapshot_state()
    corrupt = copy.deepcopy(before)
    corrupt["state"]["value"] = None
    with pytest.raises(ValueError, match="checkpoint"):
        indicator.restore_state(corrupt)
    assert indicator.snapshot_state() == before
