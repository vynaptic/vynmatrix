"""Pins for the single-source strategy runtime parameter resolution."""

from __future__ import annotations

import pytest

from indicator_runner.runtime_params import (
    DEFAULT_MAX_PANEL_AGE_DAYS,
    StrategyRuntimeParams,
)


def test_from_config_normalizes_array_universe() -> None:
    """The schema allows a JSON-array universe (equity basket); it must not be
    str()-coerced into a python-repr string (the P1e regression that broke
    equity-basket bootstrap)."""
    params = StrategyRuntimeParams.from_config(
        {"universe": ["NVDA", "TSLA", " msft "], "asset_class": "equity"},
        {"source": "eodhd", "timeframe": "1d", "consolidation_minutes": 0},
    )
    assert params.universe == "NVDA,TSLA,msft"
    assert params.source == "eodhd"


def test_panel_freshness_uses_fail_closed_default_or_exact_config() -> None:
    default = StrategyRuntimeParams.from_config({}, {})
    configured = StrategyRuntimeParams.from_config(
        {"max_panel_age_days": 100},
        {},
    )

    assert default.max_panel_age_days == DEFAULT_MAX_PANEL_AGE_DAYS == 40
    assert default.max_panel_age_seconds == 40 * 24 * 60 * 60
    assert configured.max_panel_age_days == 100
    assert configured.max_panel_age_seconds == 100 * 24 * 60 * 60


@pytest.mark.parametrize("invalid", [True, 0, -1, 1.5, "100", None])
def test_panel_freshness_rejects_invalid_configuration(invalid: object) -> None:
    with pytest.raises(ValueError, match="max_panel_age_days must be a positive integer"):
        StrategyRuntimeParams.from_config(
            {"max_panel_age_days": invalid},
            {},
        )
