"""Scoring module for signal aggregation and score calculation.

This module provides:
- Live scoring domain types
- Shared binding evaluator for threshold checking
"""

from lib_strategy.scoring.binding_evaluator import (
    BindingEvaluationResult,
    ThresholdResult,
    evaluate_binding_thresholds,
    evaluate_threshold,
)
from lib_strategy.scoring.types import ExecutionDecision

__all__ = [
    "BindingEvaluationResult",
    "ExecutionDecision",
    "ThresholdResult",
    "evaluate_binding_thresholds",
    "evaluate_threshold",
]
