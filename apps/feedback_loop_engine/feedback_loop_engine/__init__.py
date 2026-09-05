"""Feedback Loop Engine for strategy parameter optimization.

This service monitors signal performance and generates parameter
optimization suggestions when strategies make consecutive wrong predictions.
"""

from .engine import FeedbackLoopEngine
from .evaluator import SignalEvaluator
from .optimizer import ParameterOptimizer, SuggestionGenerationError
from .suggestion_review import SuggestionReviewService

__all__ = [
    "FeedbackLoopEngine",
    "ParameterOptimizer",
    "SignalEvaluator",
    "SuggestionGenerationError",
    "SuggestionReviewService",
]
