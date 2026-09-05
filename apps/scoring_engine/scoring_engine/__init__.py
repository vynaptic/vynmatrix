"""Scoring Engine package.

Aggregates canonical Signals into asset/sector scores and evaluates user bindings.

Scoring Architecture:
- Layer 1: Signal (Alpha Generators)
- Layer 2: MetaLabelOutput (Per-Strategy Meta-Labeling)
- Layer 3: GlobalScore (Ensemble Scoring)

Reference: vynmatrix Scoring Engine Architecture v1.0
"""

# Domain Models
from .domain import (
    EnsembleConfig,
    GlobalScore,
    MarketContext,
    MarketRegime,
    MetaLabelOutput,
    ScoreInterpretation,
)
from .engine import ScoreEngine

# Integrated Pipeline
from .pipeline import (
    PipelineConfig,
    PipelineResult,
    ScoringPipeline,
)
from .providers_db import DBProfileProvider, DBStrategyConfigProvider

# Services
from .services import (
    EnsembleService,
    MetaLabelService,
)

__all__ = [
    "DBProfileProvider",
    "DBStrategyConfigProvider",
    "EnsembleConfig",
    "EnsembleService",
    "GlobalScore",
    "MarketContext",
    "MarketRegime",
    "MetaLabelOutput",
    "MetaLabelService",
    "PipelineConfig",
    "PipelineResult",
    "ScoreEngine",
    "ScoreInterpretation",
    "ScoringPipeline",
]
