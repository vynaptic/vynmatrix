"""Production timeframe and shared OHLCV invariant contracts."""

from __future__ import annotations

from datetime import timedelta

import pytest

from lib_data.bars import ohlcv_invariant_error
from lib_data.dataset import (
    UnsupportedSessionTimingError,
    fixed_duration_interval,
    timeframe_interval,
)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"open_price": 0.0}, "contains a non-positive price"),
        ({"high": 99.0}, "violates OHLC bounds"),
        ({"volume": -1.0}, "contains negative volume"),
        ({"close": float("nan")}, "contains non-finite OHLCV"),
    ],
)
def test_shared_ohlcv_invariant_reports_failures(
    overrides: dict[str, float], expected: str
) -> None:
    values = {
        "open_price": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.0,
        "volume": 10.0,
    }
    values.update(overrides)

    assert ohlcv_invariant_error(**values) == expected


@pytest.mark.parametrize("timeframe", ["0m", "-1m", "0h", "-2d"])
def test_timeframe_interval_rejects_non_positive_durations(timeframe: str) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        timeframe_interval(timeframe)


def test_fixed_duration_interval_rejects_session_daily_without_authoritative_close() -> None:
    with pytest.raises(UnsupportedSessionTimingError, match="authoritative bar open/close"):
        fixed_duration_interval("1d", asset_class="equity")
    with pytest.raises(UnsupportedSessionTimingError, match="authoritative bar open/close"):
        fixed_duration_interval("1d", asset_class="index")
    with pytest.raises(UnsupportedSessionTimingError, match="authoritative bar open/close"):
        fixed_duration_interval("1d", asset_class="commodity")

    assert fixed_duration_interval("1d", asset_class="crypto") == timedelta(days=1)
    assert fixed_duration_interval("1h", asset_class="equity") == timedelta(hours=1)


def test_fixed_duration_interval_rejects_unknown_asset_class() -> None:
    with pytest.raises(ValueError, match="Unsupported asset_class"):
        fixed_duration_interval("1h", asset_class="bonds")
