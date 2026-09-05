"""Shared binding evaluator for threshold checking.

This module provides the CANONICAL threshold evaluation logic for user bindings.
All code that evaluates whether a score passes a user's threshold MUST use this.

Threshold Semantics:
- Use MAGNITUDE (abs(score)) for pass/fail determination
- Direction is derived from SIGN (>= 0 → long, < 0 → short)
- This provides symmetric treatment of long/short signals

Usage:
    >>> from lib_strategy.scoring.binding_evaluator import evaluate_threshold
    >>> result = evaluate_threshold(
    ...     score_value=Decimal("-0.75"),
    ...     threshold=Decimal("0.6"),
    ... )
    >>> result.passes  # True (abs(-0.75) = 0.75 >= 0.6)
    >>> result.direction  # "short" (score < 0)
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal


@dataclass(frozen=True)
class ThresholdResult:
    """Result of evaluating a score against a threshold.

    Attributes:
        passes: Whether the score passes the threshold (magnitude-based)
        score_value: The original score value
        magnitude: The absolute value used for comparison
        threshold: The threshold that was checked
        direction: Derived direction ("long" if score >= 0, "short" if score < 0)
    """

    passes: bool
    score_value: Decimal
    magnitude: Decimal
    threshold: Decimal
    direction: Literal["long", "short", "neutral"]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging/serialization."""
        return {
            "passes": self.passes,
            "score_value": float(self.score_value),
            "magnitude": float(self.magnitude),
            "threshold": float(self.threshold),
            "direction": self.direction,
        }


@dataclass(frozen=True)
class BindingEvaluationResult:
    """Complete result of evaluating a user binding against 3-tier scores.

    Attributes:
        passes_all: Whether all configured thresholds pass (AND logic)
        asset_result: Asset score evaluation result
        sector_result: Sector score evaluation result (if configured)
        market_result: Market score evaluation result (if configured)
        derived_direction: Overall direction derived from asset score
    """

    passes_all: bool
    asset_result: ThresholdResult
    sector_result: ThresholdResult | None
    market_result: ThresholdResult | None
    derived_direction: Literal["long", "short", "neutral"]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging/serialization."""
        result: dict[str, Any] = {
            "passes_all": self.passes_all,
            "derived_direction": self.derived_direction,
            "asset": self.asset_result.to_dict(),
        }
        if self.sector_result:
            result["sector"] = self.sector_result.to_dict()
        if self.market_result:
            result["market"] = self.market_result.to_dict()
        return result


def evaluate_threshold(
    score_value: Decimal,
    threshold: Decimal,
) -> ThresholdResult:
    """Evaluate a single score against a threshold using magnitude.

    This is the CANONICAL threshold evaluation function.

    Threshold passes if: abs(score_value) >= threshold

    Direction is derived from sign:
    - score_value >= 0 → "long"
    - score_value < 0 → "short"
    - score_value == 0 → "neutral"

    Args:
        score_value: The score value (can be positive or negative)
        threshold: The threshold to compare against (should be positive)

    Returns:
        ThresholdResult with pass/fail, magnitude, and direction

    Examples:
        >>> evaluate_threshold(Decimal("0.75"), Decimal("0.6"))
        ThresholdResult(passes=True, ..., direction="long")

        >>> evaluate_threshold(Decimal("-0.75"), Decimal("0.6"))
        ThresholdResult(passes=True, ..., direction="short")

        >>> evaluate_threshold(Decimal("0.5"), Decimal("0.6"))
        ThresholdResult(passes=False, ..., direction="long")
    """
    magnitude = abs(score_value)
    passes = magnitude >= threshold

    # Derive direction from sign
    if score_value > 0:
        direction: Literal["long", "short", "neutral"] = "long"
    elif score_value < 0:
        direction = "short"
    else:
        direction = "neutral"

    return ThresholdResult(
        passes=passes,
        score_value=score_value,
        magnitude=magnitude,
        threshold=threshold,
        direction=direction,
    )


def evaluate_binding_thresholds(
    asset_score: Decimal,
    asset_threshold: Decimal,
    sector_score: Decimal | None = None,
    sector_threshold: Decimal | None = None,
    market_score: Decimal | None = None,
    market_threshold: Decimal | None = None,
) -> BindingEvaluationResult:
    """Evaluate 3-tier binding thresholds using magnitude semantics.

    This is the CANONICAL binding evaluation function for the entire pipeline.

    3-Tier Logic (AND):
    1. Asset threshold ALWAYS checked (required)
    2. Sector threshold checked IF sector_threshold is set AND sector_score provided
    3. Market threshold checked IF market_threshold is set AND market_score provided

    All configured tiers must pass for passes_all to be True.

    Args:
        asset_score: Asset-level score value (required)
        asset_threshold: Asset-level threshold (required)
        sector_score: Sector-level score value (optional)
        sector_threshold: Sector-level threshold (optional)
        market_score: Market-level score value (optional)
        market_threshold: Market-level threshold (optional)

    Returns:
        BindingEvaluationResult with all tier results and overall pass/fail

    Examples:
        >>> # Basic asset-only evaluation
        >>> result = evaluate_binding_thresholds(
        ...     asset_score=Decimal("0.75"),
        ...     asset_threshold=Decimal("0.6"),
        ... )
        >>> result.passes_all  # True
        >>> result.derived_direction  # "long"

        >>> # Full 3-tier evaluation
        >>> result = evaluate_binding_thresholds(
        ...     asset_score=Decimal("-0.8"),
        ...     asset_threshold=Decimal("0.6"),
        ...     sector_score=Decimal("-0.7"),
        ...     sector_threshold=Decimal("0.5"),
        ...     market_score=Decimal("-0.6"),
        ...     market_threshold=Decimal("0.5"),
        ... )
        >>> result.passes_all  # True (all magnitudes pass)
        >>> result.derived_direction  # "short"
    """
    # Always evaluate asset score
    asset_result = evaluate_threshold(asset_score, asset_threshold)

    # Evaluate sector if both threshold and score provided
    sector_result = None
    sector_passes = True  # Default to pass if not configured

    if sector_threshold is not None:
        if sector_score is not None:
            sector_result = evaluate_threshold(sector_score, sector_threshold)
            sector_passes = sector_result.passes
        else:
            # Threshold configured but no score provided - fail
            sector_result = ThresholdResult(
                passes=False,
                score_value=Decimal("0"),
                magnitude=Decimal("0"),
                threshold=sector_threshold,
                direction="neutral",
            )
            sector_passes = False

    # Evaluate market if both threshold and score provided
    market_result = None
    market_passes = True  # Default to pass if not configured

    if market_threshold is not None:
        if market_score is not None:
            market_result = evaluate_threshold(market_score, market_threshold)
            market_passes = market_result.passes
        else:
            # Threshold configured but no score provided - fail
            market_result = ThresholdResult(
                passes=False,
                score_value=Decimal("0"),
                magnitude=Decimal("0"),
                threshold=market_threshold,
                direction="neutral",
            )
            market_passes = False

    # All configured tiers must pass
    passes_all = asset_result.passes and sector_passes and market_passes

    return BindingEvaluationResult(
        passes_all=passes_all,
        asset_result=asset_result,
        sector_result=sector_result,
        market_result=market_result,
        derived_direction=asset_result.direction,
    )


# Explicit public API for the scoring domain package.
__all__ = [
    "BindingEvaluationResult",
    "ThresholdResult",
    "evaluate_binding_thresholds",
    "evaluate_threshold",
]
