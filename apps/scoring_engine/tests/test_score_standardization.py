"""SC-3: hierarchy scores (sector/industry/index/market) are standardized to a
z-score so they share the asset gate's scale — the 3-tier binding thresholds
then mean the same thing on every tier."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from scoring_engine._score_calculator import ScoreCalculator

TS = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)


def _calc() -> ScoreCalculator:
    return ScoreCalculator(store=None, weight_for=lambda _sid: 1.0, half_life_bars=10)


def _signals(score_value: float) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(strategy_id="s1", score_value=score_value, confidence=0.8, timestamp=TS)
    ]


def test_aggregate_returns_zscore_and_keeps_raw_in_metadata() -> None:
    calc = _calc()
    record = calc._aggregate("crypto", "market", _signals(0.1))
    # First value: cold-start std=0.1 -> standardized = raw/0.1 = 1.0; raw kept.
    assert record.metadata["raw_score"] == pytest.approx(0.1)
    assert record.score == pytest.approx(1.0)


def test_constant_series_standardizes_toward_zero() -> None:
    calc = _calc()
    last = None
    for _ in range(6):
        last = calc._aggregate("crypto", "market", _signals(0.1))
    # A constant raw series has zero variance -> z-score collapses to ~0.
    assert last is not None
    assert abs(last.score) < 0.05
    assert last.metadata["raw_score"] == pytest.approx(0.1)


def test_each_scope_target_has_independent_stats() -> None:
    calc = _calc()
    # Drive 'market' to a settled mean at raw=0.1 ...
    for _ in range(5):
        calc._aggregate("crypto", "market", _signals(0.1))
    # ... a fresh sector target is still cold-start (amplified), not affected.
    sector = calc._aggregate("crypto", "sector", _signals(0.1))
    assert sector.score == pytest.approx(1.0)
