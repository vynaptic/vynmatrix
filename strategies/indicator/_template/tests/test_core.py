"""Scaffold tests — every real strategy must keep (and extend) these pins.

They verify the two contracts the pipeline depends on for ANY core:
deterministic construction from config strings, and warmup declaration.
Rule-level signal tests (see SwingHighLowPMO for the pattern: feed
MarketState bars, assert emitted Signal actions) must be added for the
strategy's actual logic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib_strategy.signals.loading import load_pure_strategy_core

StrategyNameCore = load_pure_strategy_core(Path(__file__).resolve().parents[1])


def test_initialize_coerces_string_parameters() -> None:
    core = StrategyNameCore(config={"period": "21", "stop_loss_pct": "0.05"})
    core.initialize()
    assert core.period == 21
    assert core.stop_loss_pct == 0.05
    assert core.warmup_bars_needed == 21


def test_initialize_fails_closed_on_invalid_parameters() -> None:
    core = StrategyNameCore(config={"period": "0"})
    with pytest.raises(ValueError, match="period"):
        core.initialize()
    core = StrategyNameCore(config={"stop_loss_pct": "1.5"})
    with pytest.raises(ValueError, match="stop_loss_pct"):
        core.initialize()
