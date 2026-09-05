"""Warm-start determinism for Layer-3 rolling standardization.

Guards the G4 fix: a restarted scoring process must resume the same rolling
mean/std it had before (reproducible standardized asset score) instead of
resetting to the cold-start default that ~10x-amplifies the first signals.
"""

from __future__ import annotations

from scoring_engine.services.ensemble_service import EnsembleService

_HISTORY = {"BTCUSD": [0.10, 0.20, 0.15, 0.18, 0.12, 0.21, 0.17, 0.14]}


def test_warm_is_reproducible_across_instances() -> None:
    a = EnsembleService()
    b = EnsembleService()
    applied_a = a.warm(_HISTORY)
    applied_b = b.warm(_HISTORY)

    assert applied_a == applied_b == len(_HISTORY["BTCUSD"])

    stats_a = a._get_rolling_stats("BTCUSD")
    stats_b = b._get_rolling_stats("BTCUSD")
    # Two independently-warmed processes derive identical standardization params.
    assert stats_a.mean == stats_b.mean
    assert stats_a.std == stats_b.std


def test_warm_differs_from_cold_start() -> None:
    warm = EnsembleService()
    warm.warm(_HISTORY)
    cold = EnsembleService()

    warm_stats = warm._get_rolling_stats("BTCUSD")
    cold_stats = cold._get_rolling_stats("BTCUSD")

    # Cold start uses the default std (0.1) which amplifies early scores; the
    # warmed window reflects the real dispersion of the history.
    assert cold_stats.std == 0.1
    assert warm_stats.std != cold_stats.std


def test_warm_empty_history_is_noop() -> None:
    svc = EnsembleService()
    assert svc.warm({}) == 0
    assert svc.warm({"ETHUSD": []}) == 0
