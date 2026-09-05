"""The mode-performance horizon bucket mapping (shared read+write contract)."""

from __future__ import annotations

import pytest

from lib_strategy.scoring.mode_horizon import normalize_mode_horizon


@pytest.mark.parametrize(
    ("horizon", "bucket"),
    [
        ("intraday", "intraday"),
        ("swing", "swing"),
        ("long_term", "long_term"),
        ("15min", "intraday"),
        ("1h", "intraday"),
        ("4h", "intraday"),
        ("1d", "intraday"),  # <= 1 day
        ("3d", "swing"),
        ("7d", "swing"),  # boundary: still swing
        ("1w", "swing"),  # 7 days
        ("2w", "long_term"),  # 14 days > 7
        ("14d", "long_term"),
        ("1m", "long_term"),  # months
        (None, "swing"),  # default
        ("", "swing"),
        ("garbage", "swing"),
        ("5x", "swing"),  # unknown suffix
    ],
)
def test_normalize_mode_horizon(horizon: str | None, bucket: str) -> None:
    assert normalize_mode_horizon(horizon) == bucket
