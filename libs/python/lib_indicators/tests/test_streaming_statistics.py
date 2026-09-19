"""Hand-calculated contracts for the shared migration indicators."""

import math

import pytest

from lib_indicators.oscillators import MACD, DirectionalMovement, RelativeStrengthIndex
from lib_indicators.streaming_statistics import CrossOver, RollingStatistics, WilderAverage


def test_rolling_population_statistics_include_current_sample():
    stats = RollingStatistics(3)
    assert stats.update(1) is None
    assert stats.update(2) is None
    assert stats.update(3) == pytest.approx((2, math.sqrt(2 / 3)))
    assert stats.update(5) == pytest.approx((10 / 3, math.sqrt(14 / 9)))
    assert stats.percent_rank == pytest.approx(2 / 3)
    stats.update(5)
    assert stats.percent_rank == pytest.approx(1 / 3)


def test_invalid_statistics_sample_does_not_poison_window():
    stats = RollingStatistics(2)
    stats.update(10)
    with pytest.raises(ValueError, match="finite"):
        stats.update(float("nan"))
    assert stats.update(12) == (11, 1)


def test_wilder_smoothing_seeds_with_mean_then_uses_one_over_period():
    average = WilderAverage(3)
    assert average.update(1) is None
    assert average.update(2) is None
    assert average.update(6) == 3
    assert average.update(9) == 5


def test_crossover_carries_last_nonzero_difference_across_equalities():
    cross = CrossOver()
    assert [cross.update(value) for value in (-2, 0, 0, 2, 0, -1, -2)] == [0, 0, 0, 1, 0, -1, 0]
    first = CrossOver()
    assert [first.update(value) for value in (0, 0, 2)] == [0, 0, 0]


def test_rsi_wilder_values_and_degenerate_series():
    rsi = RelativeStrengthIndex(3)
    assert [rsi.update(value) for value in (1, 2, 3)] == [None, None, None]
    assert rsi.update(2) == pytest.approx(200 / 3)
    assert rsi.update(4) == pytest.approx(250 / 3)
    for prices, expected in [([5] * 5, 50), ([1, 2, 3, 4, 5], 100), ([5, 4, 3, 2, 1], 0)]:
        rsi = RelativeStrengthIndex(3)
        result = [rsi.update(value) for value in prices][-1]
        assert result == expected


def test_directional_movement_warmup_and_zero_range():
    dmi = DirectionalMovement(2)
    assert dmi.update(10, 8, 9) is None
    assert dmi.update(11, 9, 10) is None
    assert dmi.update(12, 10, 11) is None
    assert dmi.plus_di == 50
    assert dmi.minus_di == 0
    assert dmi.update(13, 11, 12) == 100
    flat = DirectionalMovement(2)
    assert [flat.update(10, 10, 10) for _ in range(4)] == [None, None, None, 0]
    assert flat.plus_di == flat.minus_di == 0


def test_macd_seeds_both_price_emas_and_then_signal_ema():
    macd = MACD(2, 3, 2)
    assert [macd.update(price) for price in (1, 2, 3)] == [None, None, None]
    assert macd.update(4) == pytest.approx((0.5, 0.5, 0))
    assert macd.update(2) == pytest.approx((0, 1 / 6, -1 / 6))


@pytest.mark.parametrize("period", [0, -1, True, 2.5])
@pytest.mark.parametrize(
    "factory", [RollingStatistics, WilderAverage, RelativeStrengthIndex, DirectionalMovement]
)
def test_invalid_periods_fail_before_processing(factory, period):
    with pytest.raises(ValueError, match="period"):
        factory(period)
