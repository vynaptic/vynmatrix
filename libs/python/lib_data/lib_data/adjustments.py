"""Corporate-action price adjustment.

Reference: strategies/indicator/USQualityCompounder/README.md, "Data and Failure Rules".

CRSP-style proportional back-adjustment computed in-house from
``corporate_actions`` rows: a split with ratio ``r`` multiplies all prices
strictly before the ex-date by ``1/r`` (volumes by ``r``); a cash dividend
``d`` multiplies prices strictly before the ex-date by ``1 - d/p_prev`` where
``p_prev`` is the last raw close before the ex-date. The dividend convention
is the standard adjusted-close one: it is equivalent to reinvesting the
dividend at the ex-date reference price ``p_prev - d``, so the adjusted
return across an ex-date is ``close_ex / (p_prev - d)``.

Every factor is a pure ratio of prices, so scaling all raw prices and
dividend amounts by a constant leaves adjusted returns bit-identical — the
scale-invariance property the signal stack relies on (enforced by a property
test in ``tests/test_adjustments.py``).
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

__all__ = [
    "AdjustmentError",
    "AdjustmentEvent",
    "AdjustmentFactors",
    "RawBar",
    "adjustment_factors",
    "apply_adjustments",
    "cumulative_factors_asof",
]

_SPLIT = "split"
_DIVIDEND = "dividend"

#: (session_date, open, high, low, close, volume-or-None) — raw, as traded.
RawBar = tuple[date, float, float, float, float, float | None]


class AdjustmentError(ValueError):
    """Raised on malformed corporate-action inputs (fail closed, never guess)."""


@dataclass(frozen=True)
class AdjustmentEvent:
    """One corporate action, mirroring the ``corporate_actions`` columns."""

    ex_date: date
    action_type: str
    ratio: Decimal | None = None
    amount: Decimal | None = None

    def __post_init__(self) -> None:
        if self.action_type == _SPLIT:
            if self.ratio is None or self.ratio <= 0:
                msg = f"split on {self.ex_date} requires a positive ratio, got {self.ratio!r}"
                raise AdjustmentError(msg)
        elif self.action_type == _DIVIDEND:
            if self.amount is None or self.amount <= 0:
                msg = f"dividend on {self.ex_date} requires a positive amount, got {self.amount!r}"
                raise AdjustmentError(msg)
        else:
            msg = f"unknown corporate-action type: {self.action_type!r}"
            raise AdjustmentError(msg)


@dataclass(frozen=True)
class AdjustmentFactors:
    """Cumulative multipliers applicable to a bar (prices vs volume differ)."""

    price: float
    volume: float


def _event_factors(
    events: list[AdjustmentEvent], closes_by_date: dict[date, float]
) -> list[tuple[date, float, float]]:
    """Per-event (ex_date, price_factor, volume_factor), ascending by ex-date."""
    close_dates = sorted(closes_by_date)
    factors: list[tuple[date, float, float]] = []
    for event in sorted(events, key=lambda e: e.ex_date):
        if event.action_type == _SPLIT:
            ratio = float(event.ratio)  # type: ignore[arg-type]  # validated in __post_init__
            factors.append((event.ex_date, 1.0 / ratio, ratio))
            continue
        prior_index = bisect_left(close_dates, event.ex_date) - 1
        if prior_index < 0:
            msg = f"dividend on {event.ex_date} has no prior close to adjust against"
            raise AdjustmentError(msg)
        prior_close = closes_by_date[close_dates[prior_index]]
        amount = float(event.amount)  # type: ignore[arg-type]  # validated in __post_init__
        if amount >= prior_close:
            msg = (
                f"dividend {amount} on {event.ex_date} is >= the prior close "
                f"{prior_close} — refusing to produce a non-positive factor"
            )
            raise AdjustmentError(msg)
        factors.append((event.ex_date, 1.0 - amount / prior_close, 1.0))
    return factors


def adjustment_factors(
    events: list[AdjustmentEvent], *, closes_by_date: dict[date, float]
) -> dict[date, float]:
    """Per-event price factor keyed by ex-date (splits and dividends compound)."""
    result: dict[date, float] = {}
    for ex_date, price_factor, _volume_factor in _event_factors(events, closes_by_date):
        result[ex_date] = result.get(ex_date, 1.0) * price_factor
    return result


def cumulative_factors_asof(
    events: list[AdjustmentEvent],
    closes_by_date: dict[date, float],
    *,
    bar_date: date,
) -> AdjustmentFactors:
    """Cumulative factors for a bar: the product over events with ex_date > bar_date."""
    price = 1.0
    volume = 1.0
    for ex_date, price_factor, volume_factor in _event_factors(events, closes_by_date):
        if ex_date > bar_date:
            price *= price_factor
            volume *= volume_factor
    return AdjustmentFactors(price=price, volume=volume)


def apply_adjustments(bars: list[RawBar], events: list[AdjustmentEvent]) -> list[RawBar]:
    """Return bars with OHLC back-adjusted and volume split-adjusted.

    ``bars`` are raw as-traded rows; their own closes supply the dividend
    reference prices. Output preserves order and shape; ``None`` volume stays
    ``None``. Bars on/after the last ex-date are unchanged by construction.
    """
    if not bars:
        return []
    ordered = sorted(bars, key=lambda bar: bar[0])
    closes_by_date = {bar[0]: bar[4] for bar in ordered}
    factors = _event_factors(events, closes_by_date)
    adjusted: list[RawBar] = []
    # Walk newest -> oldest, compounding factors as ex-dates are crossed.
    price = 1.0
    volume = 1.0
    pending = list(factors)  # ascending; consume from the end
    for bar_date, open_, high, low, close, vol in reversed(ordered):
        while pending and pending[-1][0] > bar_date:
            _ex, price_factor, volume_factor = pending.pop()
            price *= price_factor
            volume *= volume_factor
        adjusted.append(
            (
                bar_date,
                open_ * price,
                high * price,
                low * price,
                close * price,
                vol * volume if vol is not None else None,
            )
        )
    adjusted.reverse()
    return adjusted
