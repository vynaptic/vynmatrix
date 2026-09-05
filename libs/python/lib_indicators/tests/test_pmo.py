"""IND-PMO: the Price Momentum Oscillator uses DecisionPoint *custom* smoothing
(multiplier 2/period) for its two PMO-line EMAs, and a standard EMA (2/(period+1))
for the signal line — matching industry charts (StockCharts / the referenced
TradingView Pine). Previously all three stages used 2/(period+1), which shifted
the PMO/signal crossover — the sole entry trigger for the launch strategy.

No public library reproduces PMO exactly (talib has no PMO, pandas-ta uses a
slightly different signal convention), so these lock the definition via the exact
multipliers plus a frozen golden vector rather than a library oracle.
"""

from __future__ import annotations

import math

import pytest

from lib_indicators.pmo import PriceMomentumOscillator


def _series(n: int = 200) -> list[float]:
    # Deterministic sinusoid + slow drift — no randomness, so the golden vector
    # below is reproducible.
    prices = [100.0]
    for i in range(1, n):
        prices.append(round(100 + 10 * math.sin(i / 9.0) + i * 0.05, 4))
    return prices


def test_custom_smoothing_multiplier_is_two_over_period() -> None:
    # 2/period, NOT 2/(period+1): stepping from 0 toward 10 over period 20
    # advances by 10 * 2/20 = 1.0.
    assert PriceMomentumOscillator._custom_ema(0.0, 10.0, 20) == pytest.approx(1.0)
    assert PriceMomentumOscillator._custom_ema(0.0, 10.0, 4) == pytest.approx(5.0)


def test_signal_uses_standard_ema_multiplier() -> None:
    assert PriceMomentumOscillator._standard_ema(0.0, 10.0, 10) == pytest.approx(10 * 2 / 11)


def test_custom_and_standard_smoothing_differ() -> None:
    assert PriceMomentumOscillator._custom_ema(
        0.0, 10.0, 20
    ) != PriceMomentumOscillator._standard_ema(0.0, 10.0, 20)


def test_golden_vector_locks_the_definition() -> None:
    pmo = PriceMomentumOscillator(35, 20, 10)
    out = (None, None)
    for price in _series():
        out = pmo.update(price)
    assert pmo.is_ready
    # Frozen from the custom-smoothing implementation; a multiplier or seeding
    # regression breaks these.
    assert pmo.pmo == pytest.approx(1.026883, abs=1e-5)
    assert pmo.signal == pytest.approx(2.131625, abs=1e-5)
    assert out == (pmo.pmo, pmo.signal)


def test_deterministic_across_runs() -> None:
    series = _series()
    a = PriceMomentumOscillator(35, 20, 10)
    b = PriceMomentumOscillator(35, 20, 10)
    for price in series:
        a.update(price)
    for price in series:
        b.update(price)
    assert a.pmo == b.pmo
    assert a.signal == b.signal


def test_flat_series_converges_to_zero() -> None:
    # No div-by-zero on a flat series; ROC is 0 so PMO and signal decay to ~0.
    pmo = PriceMomentumOscillator(35, 20, 10)
    for _ in range(200):
        pmo.update(100.0)
    assert pmo.is_ready
    assert pmo.pmo == pytest.approx(0.0, abs=1e-9)
    assert pmo.signal == pytest.approx(0.0, abs=1e-9)


def test_histories_remain_bounded_during_long_running_stream() -> None:
    pmo = PriceMomentumOscillator(5, 4, 3)

    for index in range(10_000):
        pmo.update(100.0 + index * 0.01)

    assert len(pmo.prices) <= 2
    assert len(pmo.roc_values) <= 5
    assert len(pmo.first_ema_values) <= 4
    assert len(pmo.pmo_values) <= 3
    assert len(pmo.signal_values) <= 3


@pytest.mark.parametrize("lengths", [(0, 2, 2), (2, 0, 2), (2, 2, 0), (-1, 2, 2)])
def test_rejects_nonpositive_lengths(lengths: tuple[int, int, int]) -> None:
    with pytest.raises(ValueError, match="must all be positive"):
        PriceMomentumOscillator(*lengths)
