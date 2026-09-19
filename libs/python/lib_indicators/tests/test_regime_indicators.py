import math

import pytest

from lib_indicators.regime import HurstExponent, Stochastic


def test_hurst_uses_backtrader_square_root_of_population_standard_deviation():
    hurst = HurstExponent(8)
    assert [hurst.update(value) for value in (1, 4, 9, 16, 25, 36, 49)] == [None] * 7
    expected = math.log(math.sqrt(72 / (140 / 3))) / math.log(3 / 2)
    assert hurst.update(64) == pytest.approx(expected)


def test_degenerate_hurst_is_unavailable_instead_of_fabricated_regime():
    hurst = HurstExponent(8)
    assert [hurst.update(10) for _ in range(12)] == [None] * 12


def test_stochastic_is_slow_k_then_slow_d_with_full_chain_warmup():
    stochastic = Stochastic(2, 2, 2)
    assert stochastic.update(11, 9, 10) is None
    assert stochastic.update(13, 11, 12) is None
    assert stochastic.update(15, 13, 14) is None
    assert stochastic.update(17, 15, 16) == (75, 75)
    assert stochastic.update(16, 14, 15) == pytest.approx((325 / 6, 775 / 12))


def test_flat_stochastic_cannot_reuse_a_stale_signal():
    stochastic = Stochastic(2, 2, 2)
    for index in range(4):
        stochastic.update(11 + index, 9 + index, 10 + index)
    assert stochastic.update(13, 13, 13) is not None
    assert stochastic.update(13, 13, 13) is None
