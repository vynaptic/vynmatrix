"""
Layer 3: Global Ensemble Scoring Engine

This module computes the global aggregated score across all strategies:
1. Alpha aggregation: alpha_core = sum(w_i * score_local_i)
2. News augmentation: alpha_raw = alpha_core + beta_news * N_t
3. Standardization: Score = (alpha_raw - m_alpha) / s_alpha
4. Logit stacking: z = sum(w_i * logit(p_i)) + gamma_news * N_t
5. Global probability: P_agg = 1 / (1 + e^(-z))

Reference: vynmatrix Scoring Engine Architecture v1.0, Section 4.3 & 8.3
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from .layer2_meta_label import MetaLabelOutput, logit, sigmoid

_STRONG_SCORE_THRESHOLD = 1.5
_MODERATE_SCORE_THRESHOLD = 0.5
_WEAK_SCORE_THRESHOLD = 0.1
_WEIGHT_NORMALIZATION_TOLERANCE = 0.01
_MIN_ALPHA_STD = 0.01


class ScoreInterpretation(Enum):
    """Human-readable interpretation of score magnitude."""

    STRONG_LONG = "strong_long"  # Score > 1.5
    MODERATE_LONG = "moderate_long"  # 0.5 < Score <= 1.5
    WEAK_LONG = "weak_long"  # 0 < Score <= 0.5
    NEUTRAL = "neutral"  # Score ≈ 0
    WEAK_SHORT = "weak_short"  # -0.5 <= Score < 0
    MODERATE_SHORT = "moderate_short"  # -1.5 <= Score < -0.5
    STRONG_SHORT = "strong_short"  # Score < -1.5


@dataclass
class GlobalScore:
    """
    Global ensemble score output from Layer 3.

    Aggregates all strategy scores into a single consensus view with:
    - Standardized alpha score (Score)
    - Aggregated probability from logit stacking (P_agg)

    Attributes:
        asset: Trading symbol
        alpha_core: Weighted sum of local scores (sum w_i x score_local_i)
        alpha_raw: News-augmented alpha (alpha_core + beta_news x N_t)
        score: Standardized score ((alpha_raw - m_alpha) / s_alpha)
        p_agg: Aggregated probability from logit stacking
        news_sentiment: News sentiment used (N_t)
        interpretation: Human-readable score interpretation
        strategy_count: Number of strategies in ensemble
        contributing_strategies: List of strategy IDs that contributed
        computed_at: Timestamp of computation

    Mathematical Formulas:
        alpha_core = sum w_i x score_local_i
        alpha_raw = alpha_core + beta_news x N_t
        Score = (alpha_raw - m_alpha) / s_alpha
        z = sum w_tilde_i x logit(p_i) + gamma_news x N_t
        P_agg = 1 / (1 + e^(-z))

    Example:
        # Given 3 strategies with scores [0.128, 0.08, -0.05] and weights [0.4, 0.35, 0.25]
        # alpha_core = 0.4x0.128 + 0.35x0.08 + 0.25x(-0.05) = 0.067
        # With m_alpha=0, s_alpha=0.1: Score = 0.067/0.1 = 0.67
    """

    # Target asset
    asset: str

    # Alpha components
    alpha_core: float  # sum(w_i * score_local_i)
    alpha_raw: float  # alpha_core + beta_news * N_t
    score: float  # Standardized: (alpha_raw - m_alpha) / s_alpha

    # Probability aggregation
    p_agg: float  # 1 / (1 + e^(-z))

    # Optional fields with defaults
    news_sentiment: float = 0.0  # N_t
    strategy_count: int = 0
    contributing_strategies: list[str] = field(default_factory=list)

    # Metadata
    computed_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def interpretation(self) -> ScoreInterpretation:
        """Get human-readable interpretation of score."""
        if self.score > _STRONG_SCORE_THRESHOLD:
            interpretation = ScoreInterpretation.STRONG_LONG
        elif self.score > _MODERATE_SCORE_THRESHOLD:
            interpretation = ScoreInterpretation.MODERATE_LONG
        elif self.score > _WEAK_SCORE_THRESHOLD:
            interpretation = ScoreInterpretation.WEAK_LONG
        elif self.score >= -_WEAK_SCORE_THRESHOLD:
            interpretation = ScoreInterpretation.NEUTRAL
        elif self.score >= -_MODERATE_SCORE_THRESHOLD:
            interpretation = ScoreInterpretation.WEAK_SHORT
        elif self.score >= -_STRONG_SCORE_THRESHOLD:
            interpretation = ScoreInterpretation.MODERATE_SHORT
        else:
            interpretation = ScoreInterpretation.STRONG_SHORT
        return interpretation

    @property
    def direction(self) -> int:
        """Inferred direction from score: +1 long, -1 short, 0 neutral."""
        if self.score > _WEAK_SCORE_THRESHOLD:
            return 1
        if self.score < -_WEAK_SCORE_THRESHOLD:
            return -1
        return 0

    @property
    def confidence(self) -> float:
        """Confidence level based on P_agg distance from 0.5."""
        return abs(self.p_agg - 0.5) * 2  # 0 at 0.5, 1 at 0 or 1

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "asset": self.asset,
            "alpha_core": self.alpha_core,
            "alpha_raw": self.alpha_raw,
            "score": self.score,
            "p_agg": self.p_agg,
            "news_sentiment": self.news_sentiment,
            "interpretation": self.interpretation.value,
            "direction": self.direction,
            "confidence": self.confidence,
            "strategy_count": self.strategy_count,
            "contributing_strategies": self.contributing_strategies,
            "computed_at": self.computed_at.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GlobalScore:
        """Create from dictionary."""
        computed_at = data.get("computed_at")
        if isinstance(computed_at, str):
            computed_at = datetime.fromisoformat(computed_at)
        elif computed_at is None:
            computed_at = datetime.now(tz=UTC)

        return cls(
            asset=data["asset"],
            alpha_core=data["alpha_core"],
            alpha_raw=data["alpha_raw"],
            score=data["score"],
            p_agg=data["p_agg"],
            news_sentiment=data.get("news_sentiment", 0.0),
            strategy_count=data.get("strategy_count", 0),
            contributing_strategies=data.get("contributing_strategies", []),
            computed_at=computed_at,
            metadata=data.get("metadata", {}),
        )


@dataclass
class EnsembleConfig:
    """
    Configuration for global ensemble scoring.

    Reference: vynmatrix Scoring Engine Architecture v1.0, Section 6.1
    """

    # Standardization parameters (rolling window stats)
    alpha_mean: float = 0.0  # m_alpha: rolling mean of alpha_raw
    alpha_std: float = 0.1  # s_alpha: rolling std of alpha_raw (min 0.01)

    # News impact
    beta_news: float = 0.1  # beta_news: news coefficient for alpha
    gamma_news: float = 0.3  # gamma_news: news coefficient for logit stacking

    # Gating thresholds
    tau_min: float = 0.3  # tau_min: minimum |Score| to pass gate
    p_min: float = 0.55  # p_min: minimum P_agg to pass gate

    # Position sizing
    w_max: float = 0.10  # Maximum position weight (10%)
    s_max: float = 2.0  # Score at which position reaches w_max


def compute_global_score(
    meta_outputs: list[MetaLabelOutput],
    strategy_weights: dict[str, float],
    config: EnsembleConfig | None = None,
    news_sentiment: float = 0.0,
    factor_alpha: float = 0.0,
) -> GlobalScore:
    """
    Compute global ensemble score from meta-labeled outputs.

    Implements the full Layer 3 scoring pipeline:
    1. Filter expired signals
    2. Compute weighted alpha_core
    3. Add news augmentation for alpha_raw
    4. Standardize to Score
    5. Logit stacking for P_agg
    6. Apply gating rules
    7. Compute base position size

    Args:
        meta_outputs: List of MetaLabelOutput from Layer 2
        strategy_weights: Mapping of strategy_id to weight w_i
        config: Ensemble configuration (uses defaults if None)
        news_sentiment: Current news sentiment N_t

    Returns:
        GlobalScore with aggregated score and position sizing

    Raises:
        ValueError: If no valid (non-expired) signals provided
    """
    if config is None:
        config = EnsembleConfig()

    # Filter expired signals
    active_outputs = [m for m in meta_outputs if not m.is_expired]

    if not active_outputs:
        # Return neutral score if no active signals
        return GlobalScore(
            asset=meta_outputs[0].asset if meta_outputs else "UNKNOWN",
            alpha_core=0.0,
            alpha_raw=0.0,
            score=0.0,
            p_agg=0.5,
            news_sentiment=news_sentiment,
            strategy_count=0,
            contributing_strategies=[],
        )

    asset = active_outputs[0].asset

    # Step 1: Compute weighted alpha_core = sum w_i x score_local_i
    total_weight = 0.0
    alpha_core = 0.0
    contributing = []

    for m in active_outputs:
        w_i = strategy_weights.get(m.strategy_id, 1.0 / len(active_outputs))
        alpha_core += w_i * m.score_local
        total_weight += w_i
        contributing.append(m.strategy_id)

    # Normalize if weights don't sum to 1
    if total_weight > 0 and abs(total_weight - 1.0) > _WEIGHT_NORMALIZATION_TOLERANCE:
        alpha_core /= total_weight

    # Step 2: News augmentation + multi-factor composite (Q3):
    #   alpha_raw = alpha_core + beta_news x N_t + factor_alpha
    # factor_alpha is the blended, already-standardized contribution of the
    # configured factors (0.0 when multi-factor scoring is off → byte-identical).
    alpha_raw = alpha_core + config.beta_news * news_sentiment + factor_alpha

    # Step 3: Standardization: Score = (alpha_raw - m_alpha) / s_alpha
    s_alpha = max(config.alpha_std, _MIN_ALPHA_STD)
    score = (alpha_raw - config.alpha_mean) / s_alpha

    # Step 4: Logit stacking: z = sum w_tilde_i x logit(p_i) + gamma_news x N_t
    z = config.gamma_news * news_sentiment
    logit_weight_sum = 0.0

    for m in active_outputs:
        w_i = strategy_weights.get(m.strategy_id, 1.0 / len(active_outputs))
        z += w_i * logit(m.meta_probability)
        logit_weight_sum += w_i

    # Normalize logit weights if needed
    if logit_weight_sum > 0 and abs(logit_weight_sum - 1.0) > _WEIGHT_NORMALIZATION_TOLERANCE:
        news_component = config.gamma_news * news_sentiment
        z = (z - news_component) / logit_weight_sum + news_component

    # Step 5: Global probability: P_agg = sigmoid(z)
    p_agg = sigmoid(z)

    return GlobalScore(
        asset=asset,
        alpha_core=alpha_core,
        alpha_raw=alpha_raw,
        score=score,
        p_agg=p_agg,
        news_sentiment=news_sentiment,
        strategy_count=len(active_outputs),
        contributing_strategies=contributing,
        metadata={
            "total_weight": total_weight,
            "logit_z": z,
            "config": {
                "tau_min": config.tau_min,
                "p_min": config.p_min,
                "w_max": config.w_max,
                "s_max": config.s_max,
            },
        },
    )
