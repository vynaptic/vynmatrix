"""Bar data primitives for the data-feed layer.

Provides a lightweight OHLCV bar representation used by consolidators,
watermark tracking, and the signal worker feed loop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def ohlcv_invariant_error(
    *,
    open_price: object,
    high: object,
    low: object,
    close: object,
    volume: object,
    allow_missing_volume: bool = False,
) -> str | None:
    """Return the shared OHLCV invariant violation, or ``None`` when valid."""

    raw_prices = (open_price, high, low, close)
    prices = tuple(
        float(value)
        for value in raw_prices
        if not isinstance(value, bool) and isinstance(value, (int, float))
    )
    error: str | None
    if len(prices) != len(raw_prices) or not all(math.isfinite(value) for value in prices):
        error = "contains non-finite OHLCV"
    elif min(prices) <= 0.0:
        error = "contains a non-positive price"
    elif (volume is None and not allow_missing_volume) or (
        volume is not None
        and (
            isinstance(volume, bool)
            or not isinstance(volume, (int, float))
            or not math.isfinite(float(volume))
        )
    ):
        error = "contains non-finite OHLCV"
    elif volume is not None and float(volume) < 0.0:
        error = "contains negative volume"
    else:
        normalized_open, normalized_high, normalized_low, normalized_close = prices
        violates_bounds = (
            normalized_high < max(normalized_open, normalized_close)
            or normalized_low > min(normalized_open, normalized_close)
            or normalized_low > normalized_high
        )
        error = "violates OHLC bounds" if violates_bounds else None
    return error


@dataclass
class Bar:
    """A single OHLCV bar."""

    symbol: str
    timestamp: datetime  # Period boundary; each feed contract defines start versus close.
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str = "1m"
    source: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2.0
