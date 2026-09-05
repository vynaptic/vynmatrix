"""Layer adapter that derives scoring inputs from canonical signals."""

from lib_strategy.signals.adapters.scoring import ScoringSignalView, build_scoring_view

__all__ = [
    "ScoringSignalView",
    "build_scoring_view",
]
