"""
Layer 3: Global Ensemble Scoring Service

Aggregates per-strategy meta-labeled scores into global consensus:
1. Weighted alpha aggregation: alpha_core = sum(w_i * score_local_i)
2. News augmentation: alpha_raw = alpha_core + beta_news * N_t
3. Standardization: Score = (alpha_raw - m_alpha) / s_alpha
4. Logit stacking: P_agg = sigmoid(sum(w_i * logit(p_i)) + gamma_news * N_t)

Reference: vynmatrix Scoring Engine Architecture v1.0, Section 4.3
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from lib_common.logging import get_logger
from scoring_engine.domain import EnsembleConfig, GlobalScore, MetaLabelOutput, compute_global_score

logger = get_logger(__name__)

_MIN_ROLLING_VALUES = 2
_MIN_ROLLING_STD = 0.01


@dataclass
class RollingStats:
    """Rolling statistics for alpha standardization."""

    window_size: int = 100
    values: deque[float] = field(default_factory=lambda: deque(maxlen=100))
    _mean: float = 0.0
    _std: float = 0.1  # Default std to avoid division by zero

    def __post_init__(self) -> None:
        self.values = deque(maxlen=self.window_size)

    def update(self, value: float) -> None:
        """Add a new value and recompute statistics."""
        self.values.append(value)
        if len(self.values) >= _MIN_ROLLING_VALUES:
            self._mean = sum(self.values) / len(self.values)
            variance = sum((v - self._mean) ** 2 for v in self.values) / len(self.values)
            self._std = max(variance**0.5, _MIN_ROLLING_STD)

    @property
    def mean(self) -> float:
        return self._mean

    @property
    def std(self) -> float:
        return self._std


@dataclass
class StrategyWeight:
    """Weight configuration for a strategy."""

    strategy_id: str
    base_weight: float = 1.0
    performance_multiplier: float = 1.0  # Updated based on feedback
    enabled: bool = True

    @property
    def effective_weight(self) -> float:
        """Compute effective weight including performance adjustment."""
        if not self.enabled:
            return 0.0
        return self.base_weight * self.performance_multiplier


class StrategyWeightManager:
    """
    Manages strategy weights with dynamic adjustment.

    Supports:
    - Per-strategy weight overrides
    - Performance-based weight adjustment (from feedback loop)
    """

    def __init__(
        self,
        strategy_weights: dict[str, StrategyWeight] | None = None,
    ) -> None:
        """
        Initialize weight manager.

        Args:
            strategy_weights: Per-strategy weight configurations
        """
        self._strategy_weights = strategy_weights or {}

    def get_weight(self, strategy_id: str) -> float:
        """Get weight for a strategy."""
        if strategy_id in self._strategy_weights:
            return self._strategy_weights[strategy_id].effective_weight

        return 1.0

    def set_weight(self, strategy_id: str, weight: StrategyWeight) -> None:
        """Set weight for a specific strategy."""
        self._strategy_weights[strategy_id] = weight

    def update_performance_multiplier(self, strategy_id: str, multiplier: float) -> None:
        """Update performance multiplier from feedback loop."""
        if strategy_id in self._strategy_weights:
            self._strategy_weights[strategy_id].performance_multiplier = multiplier
        else:
            self._strategy_weights[strategy_id] = StrategyWeight(
                strategy_id=strategy_id,
                performance_multiplier=multiplier,
            )

    def disable_strategy(self, strategy_id: str) -> None:
        """Disable a strategy (weight = 0)."""
        if strategy_id in self._strategy_weights:
            self._strategy_weights[strategy_id].enabled = False
        else:
            self._strategy_weights[strategy_id] = StrategyWeight(
                strategy_id=strategy_id,
                enabled=False,
            )

    def get_weights_dict(self, strategy_ids: list[str]) -> dict[str, float]:
        """Get weights dictionary for a list of strategies."""
        return {sid: self.get_weight(sid) for sid in strategy_ids}


class NewsSentimentProvider:
    """Provider for news sentiment data."""

    def __init__(self, default_sentiment: float = 0.0) -> None:
        self.default_sentiment = default_sentiment
        self._cache: dict[str, float] = {}

    def set_sentiment(self, asset: str, sentiment: float) -> None:
        """Set sentiment for an asset."""
        self._cache[asset] = max(-1.0, min(1.0, sentiment))

    def get_sentiment(self, asset: str) -> float:
        """Get sentiment for an asset (default: 0)."""
        return self._cache.get(asset, self.default_sentiment)


class EnsembleService:
    """
    Layer 3: Global Ensemble Scoring Service.

    Aggregates meta-labeled outputs into a single global score with:
    - Weighted alpha aggregation
    - Rolling standardization
    - Logit stacking for probability
    - Gating rules

    Usage:
        service = EnsembleService(
            config=EnsembleConfig(tau_min=0.3, p_min=0.55),
            weight_manager=StrategyWeightManager(),
        )
        global_score = service.compute_score(meta_outputs, asset="BTCUSD")
    """

    def __init__(
        self,
        config: EnsembleConfig | None = None,
        weight_manager: StrategyWeightManager | None = None,
        news_provider: NewsSentimentProvider | None = None,
        rolling_window: int = 100,
        factor_blender: Any | None = None,
    ) -> None:
        """
        Initialize ensemble service.

        Args:
            config: Ensemble configuration
            weight_manager: Strategy weight manager
            news_provider: News sentiment provider
            rolling_window: Window size for rolling statistics
            factor_blender: Optional multi-factor blender (Q3). When None (the
                default) the score is the single-alpha path, byte-identical to
                before. When supplied, its blended factor alpha augments
                ``alpha_raw`` and the composite is standardized in a SEPARATE
                ``mf:`` rolling-stats namespace so the single-alpha window is
                never mutated.
        """
        self.config = config or EnsembleConfig()
        self.weight_manager = weight_manager or StrategyWeightManager()
        self.news_provider = news_provider or NewsSentimentProvider()
        self._factor_blender = factor_blender

        # Rolling stats per asset for standardization
        self._rolling_stats: dict[str, RollingStats] = {}
        self._rolling_window = rolling_window

    def _get_rolling_stats(self, asset: str) -> RollingStats:
        """Get or create rolling stats for an asset."""
        if asset not in self._rolling_stats:
            self._rolling_stats[asset] = RollingStats(window_size=self._rolling_window)
        return self._rolling_stats[asset]

    def compute_score(
        self,
        meta_outputs: list[MetaLabelOutput],
        asset: str | None = None,
        news_sentiment: float | None = None,
        stats_key: str | None = None,
    ) -> GlobalScore:
        """
        Compute global ensemble score from meta-labeled outputs.

        Args:
            meta_outputs: List of MetaLabelOutput from Layer 2
            asset: Asset symbol (inferred from outputs if not provided)
            news_sentiment: Override news sentiment (fetched from provider if not set)
            stats_key: Optional rolling-stats namespace override. Defaults to
                ``asset``; pass a distinct key to standardize/update a separate
                score distribution (e.g. cross-strategy blends vs lone signals)
                without the two windows contaminating each other.

        Returns:
            GlobalScore with aggregated score and position sizing
        """
        if not meta_outputs:
            return GlobalScore(
                asset=asset or "UNKNOWN",
                alpha_core=0.0,
                alpha_raw=0.0,
                score=0.0,
                p_agg=0.5,
            )

        # Infer asset from outputs
        if asset is None:
            asset = meta_outputs[0].asset

        # Get news sentiment
        if news_sentiment is None:
            news_sentiment = self.news_provider.get_sentiment(asset)

        # Get strategy weights
        strategy_ids = [m.strategy_id for m in meta_outputs]
        weights = self.weight_manager.get_weights_dict(strategy_ids)

        # Multi-factor composite (Q3): when a blender is wired, add its blended
        # factor alpha to the signal alpha and standardize the COMPOSITE in a
        # separate ``mf:`` namespace so the single-alpha window is never mutated.
        # When off (default), factor_alpha=0.0 and the namespace/arithmetic are
        # identical to the single-alpha path → byte-identical for the soak.
        factor_alpha = 0.0
        factor_breakdown: dict[str, float] = {}
        effective_stats_key = stats_key or asset
        if self._factor_blender is not None:
            blend = self._factor_blender.blend(asset)
            factor_alpha = blend.alpha
            factor_breakdown = blend.breakdown
            effective_stats_key = f"mf:{effective_stats_key}"

        # Get rolling stats for standardization (a distinct stats_key keeps the
        # cross-strategy blend distribution separate from the lone-signal one).
        rolling = self._get_rolling_stats(effective_stats_key)

        # Create dynamic config with rolling stats
        dynamic_config = EnsembleConfig(
            alpha_mean=rolling.mean,
            alpha_std=rolling.std,
            beta_news=self.config.beta_news,
            gamma_news=self.config.gamma_news,
            tau_min=self.config.tau_min,
            p_min=self.config.p_min,
            w_max=self.config.w_max,
            s_max=self.config.s_max,
        )

        # Compute global score
        global_score = compute_global_score(
            meta_outputs=meta_outputs,
            strategy_weights=weights,
            config=dynamic_config,
            news_sentiment=news_sentiment,
            factor_alpha=factor_alpha,
        )

        # Update rolling stats with this alpha_raw
        rolling.update(global_score.alpha_raw)

        if self._factor_blender is not None:
            global_score.metadata["factors"] = {"alpha": factor_alpha, **factor_breakdown}

        return global_score

    def compute_scores_batch(
        self,
        outputs_by_asset: dict[str, list[MetaLabelOutput]],
    ) -> dict[str, GlobalScore]:
        """
        Compute global scores for multiple assets.

        Args:
            outputs_by_asset: Dictionary mapping asset to meta-labeled outputs

        Returns:
            Dictionary mapping asset to GlobalScore
        """
        scores = {}
        for asset, outputs in outputs_by_asset.items():
            try:
                scores[asset] = self.compute_score(outputs, asset=asset)
            except Exception:
                logger.exception("Failed to compute score for %s", asset)
                continue
        return scores

    def update_rolling_stats(self, asset: str, alpha_raw: float) -> None:
        """Manually update rolling stats (for historical backfill)."""
        rolling = self._get_rolling_stats(asset)
        rolling.update(alpha_raw)

    def warm(self, history_by_asset: Mapping[str, Sequence[float]]) -> int:
        """Pre-fill rolling standardization windows from historical ``alpha_raw``.

        Values are applied oldest→newest per asset so a restarted scoring
        process resumes with the same rolling mean/std it had before, making the
        standardized asset score reproducible across restarts instead of
        resetting to the cold-start default (std=0.1, which amplifies the first
        signals ~10x). Returns the number of values applied.
        """
        applied = 0
        for asset, values in history_by_asset.items():
            rolling = self._get_rolling_stats(asset)
            for value in values:
                rolling.update(float(value))
                applied += 1
        return applied

    def get_config_summary(self) -> dict[str, Any]:
        """Get summary of current configuration."""
        return {
            "tau_min": self.config.tau_min,
            "p_min": self.config.p_min,
            "w_max": self.config.w_max,
            "s_max": self.config.s_max,
            "beta_news": self.config.beta_news,
            "gamma_news": self.config.gamma_news,
        }
