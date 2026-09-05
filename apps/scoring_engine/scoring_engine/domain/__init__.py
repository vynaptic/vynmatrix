"""
Domain models for the scoring architecture.

Layer 1: canonical Signal (from lib_strategy.signals.signal)
Layer 2: MetaLabelOutput - Per-strategy meta-labeled score
Layer 3: GlobalScore - Cross-strategy ensemble score

Reference: vynmatrix Scoring Engine Architecture v1.0
"""

from .layer2_meta_label import (
    MarketContext,
    MarketRegime,
    MetaLabelOutput,
    logit,
    sigmoid,
)
from .layer3_global_score import (
    EnsembleConfig,
    GlobalScore,
    ScoreInterpretation,
    compute_global_score,
)

__all__ = [
    "EnsembleConfig",
    "GlobalScore",
    "MarketContext",
    "MarketRegime",
    "MetaLabelOutput",
    "ScoreInterpretation",
    "compute_global_score",
    "logit",
    "sigmoid",
]
