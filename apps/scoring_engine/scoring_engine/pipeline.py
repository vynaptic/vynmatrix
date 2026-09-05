"""
Scoring Pipeline: Integrated Orchestrator

This module provides the main entry point for processing signals through
the scoring architecture:

Layer 1: Signal (input)
    ↓
Layer 2: MetaLabelService → MetaLabelOutput
    ↓
Layer 3: EnsembleService → GlobalScore (output)

Reference: vynmatrix Scoring Engine Architecture v1.0
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from lib_common.logging import get_logger
from lib_strategy.signals.signal import Signal, SignalAction

from .domain import (
    EnsembleConfig,
    GlobalScore,
    MarketContext,
    MetaLabelOutput,
)
from .services import (
    EnsembleService,
    MetaLabelService,
)
from .services.ensemble_service import (
    StrategyWeightManager,
)
from .services.meta_label_service import (
    MetaLabelConfig,
    ObservedMarketContextProvider,
)

logger = get_logger(__name__)


@dataclass
class PipelineConfig:
    """
    Unified configuration for the scoring pipeline.

    Combines configuration for all layers into a single object.
    """

    # Layer 2: Meta-labeling config
    meta_regime_adjustment: float = 0.1

    # Layer 3: Ensemble config
    beta_news: float = 0.1  # News coefficient for alpha
    gamma_news: float = 0.3  # News coefficient for logit stacking
    tau_min: float = 0.3  # Minimum |Score| to pass gate
    p_min: float = 0.55  # Minimum P_agg to pass gate
    w_max: float = 0.10  # Maximum position weight
    s_max: float = 2.0  # Score at which position reaches w_max

    # Rolling stats window
    rolling_window: int = 100

    def to_meta_config(self) -> MetaLabelConfig:
        """Create MetaLabelConfig from pipeline config."""
        return MetaLabelConfig(
            regime_adjustment=self.meta_regime_adjustment,
        )

    def to_ensemble_config(self) -> EnsembleConfig:
        """Create EnsembleConfig from pipeline config."""
        return EnsembleConfig(
            beta_news=self.beta_news,
            gamma_news=self.gamma_news,
            tau_min=self.tau_min,
            p_min=self.p_min,
            w_max=self.w_max,
            s_max=self.s_max,
        )


@dataclass
class PipelineResult:
    """
    Result from processing signals through the pipeline.

    Contains outputs from all layers for traceability.
    """

    asset: str
    signals: list[Signal]
    meta_outputs: list[MetaLabelOutput]
    global_score: GlobalScore
    processed_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "asset": self.asset,
            "signal_count": len(self.signals),
            "meta_output_count": len(self.meta_outputs),
            "global_score": self.global_score.to_dict(),
            "processed_at": self.processed_at.isoformat(),
            "metadata": self.metadata,
        }


class ScoringPipeline:
    """
    Integrated three-stage scoring pipeline.

    Orchestrates the complete signal-to-score flow:
    1. Receive canonical Signals and derive ScoringSignalView inputs
    2. Meta-label with observed market context
    3. Aggregate into GlobalScore

    Binding evaluation, execution-mode selection, and position sizing are
    downstream responsibilities and are not part of this pipeline.

    Usage:
        pipeline = ScoringPipeline(config=PipelineConfig())

        # Process signals for an asset
        result = pipeline.process_signals(signals)
        score = result.global_score

    """

    def __init__(
        self,
        config: PipelineConfig | None = None,
        meta_service: MetaLabelService | None = None,
        ensemble_service: EnsembleService | None = None,
        factor_blender: Any | None = None,
    ) -> None:
        """
        Initialize scoring pipeline.

        Args:
            config: Pipeline configuration (uses defaults if not provided)
            meta_service: Override Layer 2 service
            ensemble_service: Override Layer 3 service
            factor_blender: Optional multi-factor blender (Q3); passed to the
                default ensemble. None (default) = single-alpha path.
        """
        self.config = config or PipelineConfig()

        # Initialize services with config
        if meta_service is not None:
            self.meta_service = meta_service
        else:
            self.meta_service = MetaLabelService(
                config=self.config.to_meta_config(),
            )

        if ensemble_service is not None:
            self.ensemble_service = ensemble_service
        else:
            self.ensemble_service = EnsembleService(
                config=self.config.to_ensemble_config(),
                weight_manager=StrategyWeightManager(),
                rolling_window=self.config.rolling_window,
                factor_blender=factor_blender,
            )

        # Shared providers (can be updated)
        self._context_provider = self.meta_service.context_provider
        self._news_provider = self.ensemble_service.news_provider

    # -------------------------------------------------------------------------
    # Provider setters for updating market data
    # -------------------------------------------------------------------------

    def set_market_context(self, asset: str, context: MarketContext) -> None:
        """Set market context for an asset (Layer 2)."""
        if isinstance(self._context_provider, ObservedMarketContextProvider):
            self._context_provider.set_context(asset, context)

    def set_news_sentiment(self, asset: str, sentiment: float) -> None:
        """Set news sentiment for an asset (Layer 3)."""
        self._news_provider.set_sentiment(asset, sentiment)

    # -------------------------------------------------------------------------
    # Main processing methods
    # -------------------------------------------------------------------------

    def process_signals(
        self,
        signals: list[Signal],
        current_time: datetime | None = None,
        stats_key: str | None = None,
        *,
        scoring_action_overrides: Mapping[str, SignalAction] | None = None,
    ) -> PipelineResult:
        """
        Process signals for a single asset through all scoring stages.

        Args:
            signals: List of canonical Signal objects from various strategies
            current_time: Override current time (for backtesting)
            stats_key: Optional override for the rolling-stats namespace used to
                standardize the global score. Defaults to the asset symbol; a
                caller passes a distinct key (e.g. a cross-strategy namespace) to
                keep separate score distributions from sharing one window.

        Returns:
            PipelineResult with outputs from all scoring stages
        """
        if not signals:
            asset = "UNKNOWN"
            return PipelineResult(
                asset=asset,
                signals=[],
                meta_outputs=[],
                global_score=GlobalScore(
                    asset=asset,
                    alpha_core=0.0,
                    alpha_raw=0.0,
                    score=0.0,
                    p_agg=0.5,
                ),
            )

        if current_time is None:
            current_time = datetime.now(tz=UTC)

        # Validate all signals are for the same asset
        symbols = {s.symbol for s in signals}
        if len(symbols) > 1:
            logger.warning(
                "process_signals received mixed symbols %s; using first signal's symbol %s",
                symbols,
                signals[0].symbol,
            )

        asset = signals[0].symbol

        # Layer 2: Meta-labeling
        if scoring_action_overrides:
            meta_outputs = self.meta_service.process_signals(
                signals=signals,
                current_time=current_time,
                filter_expired=True,
                scoring_action_overrides=scoring_action_overrides,
            )
        else:
            meta_outputs = self.meta_service.process_signals(
                signals=signals,
                current_time=current_time,
                filter_expired=True,
            )

        # Layer 3: Global ensemble
        global_score = self.ensemble_service.compute_score(
            meta_outputs=meta_outputs,
            asset=asset,
            stats_key=stats_key,
        )
        abstained = not meta_outputs
        abstain_reason = "meta_inputs_unavailable" if abstained else None

        return PipelineResult(
            asset=asset,
            signals=signals,
            meta_outputs=meta_outputs,
            global_score=global_score,
            processed_at=current_time,
            metadata={
                "config": {
                    "tau_min": self.config.tau_min,
                    "p_min": self.config.p_min,
                    "w_max": self.config.w_max,
                },
                "abstained": abstained,
                "abstain_reason": abstain_reason,
            },
        )

    def get_status(self) -> dict[str, Any]:
        """Get current pipeline status and configuration."""
        return {
            "config": {
                "tau_min": self.config.tau_min,
                "p_min": self.config.p_min,
                "w_max": self.config.w_max,
                "s_max": self.config.s_max,
                "beta_news": self.config.beta_news,
                "gamma_news": self.config.gamma_news,
            },
            "layer2": {
                "scorer": type(self.meta_service.scorer).__name__,
                "context_provider": type(self.meta_service.context_provider).__name__,
            },
            "layer3": self.ensemble_service.get_config_summary(),
        }
