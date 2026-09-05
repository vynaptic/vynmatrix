"""Adjustment-builder tests (design doc §7.2): CRSP conventions + scale invariance."""

from __future__ import annotations

import itertools
import math
import random
from datetime import date, timedelta
from decimal import Decimal

import pytest

from lib_data.adjustments import (
    AdjustmentError,
    AdjustmentEvent,
    adjustment_factors,
    apply_adjustments,
    cumulative_factors_asof,
)

D1 = date(2020, 8, 27)
D2 = date(2020, 8, 28)
EX = date(2020, 8, 31)
D4 = date(2020, 9, 1)

_REL_TOL = 1e-12


def _bar(day: date, price: float, volume: float | None = 1000.0):
    return (day, price, price * 1.01, price * 0.99, price, volume)


def test_split_scales_prior_prices_down_and_volume_up() -> None:
    bars = [_bar(D1, 400.0), _bar(D2, 500.0), _bar(EX, 125.0), _bar(D4, 130.0)]
    split = AdjustmentEvent(ex_date=EX, action_type="split", ratio=Decimal(4))

    adjusted = apply_adjustments(bars, [split])

    assert adjusted[0][4] == pytest.approx(100.0)  # 400 / 4
    assert adjusted[1][4] == pytest.approx(125.0)  # 500 / 4
    assert adjusted[0][5] == pytest.approx(4000.0)  # volume x4
    assert adjusted[2] == bars[2]  # ex-date bar untouched
    assert adjusted[3] == bars[3]


def test_dividend_factor_matches_reinvest_at_ex_reference_price() -> None:
    # Prior close 100, dividend 1.00 -> factor 0.99; the adjusted return across
    # the ex-date must equal close_ex / (p_prev - d) (standard adjusted-close
    # reinvestment convention).
    bars = [_bar(D2, 100.0), _bar(EX, 99.5)]
    dividend = AdjustmentEvent(ex_date=EX, action_type="dividend", amount=Decimal("1.00"))

    factors = adjustment_factors([dividend], closes_by_date={D2: 100.0, EX: 99.5})
    assert factors[EX] == pytest.approx(0.99)

    adjusted = apply_adjustments(bars, [dividend])
    adjusted_return = adjusted[1][4] / adjusted[0][4]
    assert adjusted_return == pytest.approx(99.5 / (100.0 - 1.0), rel=_REL_TOL)


def test_events_compound_multiplicatively_in_date_order() -> None:
    ex2 = date(2021, 3, 1)
    ex3 = date(2021, 9, 1)
    bars = [
        _bar(D1, 400.0),
        _bar(EX, 100.0),
        _bar(ex2, 99.0),
        _bar(ex3, 50.0),
        _bar(date(2021, 9, 2), 51.0),
    ]
    events = [
        AdjustmentEvent(ex_date=ex3, action_type="split", ratio=Decimal(2)),
        AdjustmentEvent(ex_date=EX, action_type="split", ratio=Decimal(4)),
        AdjustmentEvent(ex_date=ex2, action_type="dividend", amount=Decimal(1)),
    ]

    factors = cumulative_factors_asof(events, {b[0]: b[4] for b in bars}, bar_date=D1)
    # Before everything: (1/4) * (1 - 1/100) * (1/2)
    assert factors.price == pytest.approx(0.25 * 0.99 * 0.5, rel=_REL_TOL)
    assert factors.volume == pytest.approx(8.0)

    mid = cumulative_factors_asof(events, {b[0]: b[4] for b in bars}, bar_date=ex2)
    assert mid.price == pytest.approx(0.5, rel=_REL_TOL)  # only the later split remains


def test_scale_invariance_property() -> None:
    """Scaling all prices AND dividend amounts by k leaves adjusted log-returns identical."""
    rng = random.Random(42)
    start = date(2020, 1, 2)
    days = [start + timedelta(days=offset) for offset in range(0, 400, 1)]
    days = [d for d in days if d.weekday() < 5][:250]

    price = 100.0
    bars: list = []
    for day in days:
        price *= math.exp(rng.gauss(0.0003, 0.02))
        bars.append(_bar(day, price))
    events = [
        AdjustmentEvent(ex_date=days[60], action_type="dividend", amount=Decimal("0.55")),
        AdjustmentEvent(ex_date=days[120], action_type="split", ratio=Decimal(5)),
        AdjustmentEvent(ex_date=days[200], action_type="dividend", amount=Decimal("0.61")),
    ]

    def log_returns(raw_bars: list, raw_events: list) -> list[float]:
        closes = [bar[4] for bar in apply_adjustments(raw_bars, raw_events)]
        return [math.log(b / a) for a, b in itertools.pairwise(closes)]

    k = Decimal("3.7")
    scaled_bars = [
        (d, o * float(k), h * float(k), lo * float(k), c * float(k), v)
        for (d, o, h, lo, c, v) in bars
    ]
    scaled_events = [
        AdjustmentEvent(
            ex_date=e.ex_date,
            action_type=e.action_type,
            ratio=e.ratio,
            amount=e.amount * k if e.amount is not None else None,
        )
        for e in events
    ]

    base = log_returns(bars, events)
    scaled = log_returns(scaled_bars, scaled_events)
    assert base == pytest.approx(scaled, rel=_REL_TOL, abs=_REL_TOL)


def test_dividend_without_prior_close_fails_closed() -> None:
    bars = [_bar(EX, 99.0), _bar(D4, 100.0)]
    dividend = AdjustmentEvent(ex_date=EX, action_type="dividend", amount=Decimal(1))
    with pytest.raises(AdjustmentError, match="no prior close"):
        apply_adjustments(bars, [dividend])


def test_dividend_gte_prior_close_fails_closed() -> None:
    dividend = AdjustmentEvent(ex_date=EX, action_type="dividend", amount=Decimal(100))
    with pytest.raises(AdjustmentError, match="refusing"):
        adjustment_factors([dividend], closes_by_date={D2: 100.0})


@pytest.mark.parametrize(
    ("action_type", "kwargs"),
    [
        ("split", {"ratio": Decimal(0)}),
        ("split", {"ratio": None}),
        ("dividend", {"amount": Decimal(0)}),
        ("dividend", {"amount": None}),
        ("buyback", {"ratio": Decimal(1)}),
    ],
)
def test_malformed_events_rejected(action_type: str, kwargs: dict) -> None:
    with pytest.raises(AdjustmentError):
        AdjustmentEvent(ex_date=EX, action_type=action_type, **kwargs)
