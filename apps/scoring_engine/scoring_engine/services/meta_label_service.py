"""
Layer 2: Meta-Labeling Service

Computes per-strategy meta-labeled scores by:
1. Fetching/computing market context
2. Running the configured observed-feature scorer to get a bounded meta value
3. Computing age decay, tilt, and local score

Reference: vynmatrix Scoring Engine Architecture v1.0, Section 4.2
"""

from __future__ import annotations

import math
import statistics
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Protocol

from lib_common.logging import get_logger
from lib_strategy.signals.adapters.scoring import ScoringSignalView, build_scoring_view
from lib_strategy.signals.signal import Signal, SignalAction
from scoring_engine.domain import (
    MarketContext,
    MarketRegime,
    MetaLabelOutput,
)

logger = get_logger(__name__)

_HIGH_VIX_PERCENTILE = 70.0
_LOW_VIX_PERCENTILE = 30.0
_LOW_LIQUIDITY_SCORE = 0.5
_CONTEXT_ADJUSTMENT = 0.05


class MetaScorer(Protocol):
    """Protocol for observed-feature meta scorers."""

    def score_signal(
        self,
        signal: ScoringSignalView,
        context: MarketContext,
    ) -> float:
        """
        Compute a bounded meta score for a signal.

        Args:
            signal: The strategy signal to evaluate
            context: Current market context

        Returns:
            Bounded value in [0, 1]. It is a probability only when the
            implementation is explicitly calibrated from observed outcomes.
        """
        ...


class DeterministicMetaScorer:
    """
    Bounded deterministic meta-scorer for paper validation.

    This is not a trained or calibrated probability model. It maps an explicitly
    supplied strategy confidence plus observed market-context fields into a
    bounded value consumed by the existing meta-label formula. Live promotion
    requires replacing it with a calibrated scorer backed by observed outcomes
    via ``register_scorer``.
    """

    def __init__(
        self,
        regime_adjustment: float = 0.1,
    ) -> None:
        """Initialize the deterministic observed-feature adjustment."""
        self.regime_adjustment = regime_adjustment

    def score_signal(
        self,
        signal: ScoringSignalView,
        context: MarketContext,
    ) -> float:
        """
        Compute an uncalibrated bounded score using deterministic rules.

        Factors considered:
        - Signal's Sharpe-like strength (higher = more confident)
        - Market regime alignment with signal direction
        - VIX percentile (extreme values reduce confidence)
        - Liquidity conditions
        """
        # Start from the strategy's explicitly emitted confidence. There is no
        # neutral-looking fallback constant.
        p = signal.confidence

        # Adjust for Sharpe-like strength
        sharpe_magnitude = min(abs(signal.sharpe_raw), 2.0)  # Cap at 2
        sharpe_boost = sharpe_magnitude * 0.1  # Up to +0.2 for strong signals
        p += sharpe_boost

        # Adjust for market regime alignment
        is_bullish_signal = signal.direction > 0
        bull_regimes = (MarketRegime.BULL_QUIET, MarketRegime.BULL_VOLATILE)
        bear_regimes = (MarketRegime.BEAR_QUIET, MarketRegime.BEAR_VOLATILE)
        is_bullish_regime = context.regime in bull_regimes
        is_bearish_regime = context.regime in bear_regimes

        # Signal aligns with regime → boost the bounded meta value
        signal_aligns = (is_bullish_signal and is_bullish_regime) or (
            not is_bullish_signal and is_bearish_regime
        )
        signal_conflicts = (is_bullish_signal and is_bearish_regime) or (
            not is_bullish_signal and is_bullish_regime
        )

        if signal_aligns:
            p += self.regime_adjustment
        elif signal_conflicts:
            p -= self.regime_adjustment

        # Adjust for volatility regime
        # High VIX (>70 percentile) reduces confidence for momentum strategies
        if signal.strategy_type == "indicator" and context.vix_percentile is not None:
            if context.vix_percentile > _HIGH_VIX_PERCENTILE:
                p -= _CONTEXT_ADJUSTMENT
            elif context.vix_percentile < _LOW_VIX_PERCENTILE:
                p += _CONTEXT_ADJUSTMENT

        # Adjust only when liquidity was observed.
        if context.liquidity_score is not None and context.liquidity_score < _LOW_LIQUIDITY_SCORE:
            p -= _CONTEXT_ADJUSTMENT

        # Clamp to the bounded meta-value range
        return float(max(0.05, min(0.95, p)))


@dataclass
class MetaLabelConfig:
    """Configuration for meta-labeling service."""

    regime_adjustment: float = 0.1

    # Age decay parameters
    max_age_multiplier: float = 2.0  # Signals older than 2xhorizon are expired


class MarketContextProvider(ABC):
    """Abstract provider for market context data."""

    @abstractmethod
    def get_context(self, asset: str, as_of: datetime | None = None) -> MarketContext:
        """
        Get market context for an asset at a given time.

        Args:
            asset: Trading symbol
            as_of: Point in time (default: now)

        Returns:
            MarketContext with all available features
        """
        ...


class MarketContextUnavailableError(RuntimeError):
    """Raised when observed market context cannot be established safely."""


class ObservedMarketContextProvider(MarketContextProvider):
    """
    Explicit observed-context provider for unit tests and historical backtests.

    There are no defaults. A caller must inject a validated ``MarketContext`` for
    each asset before scoring; a missing asset fails closed.
    """

    def __init__(self) -> None:
        self._cache: dict[str, MarketContext] = {}

    def set_context(self, asset: str, context: MarketContext) -> None:
        """Cache context for an asset."""
        self._cache[asset] = context

    def get_context(self, asset: str, as_of: datetime | None = None) -> MarketContext:
        """Get explicitly injected context or fail closed."""
        try:
            context = self._cache[asset]
        except KeyError as exc:
            msg = f"No observed market context is available for {asset}"
            raise MarketContextUnavailableError(msg) from exc
        if as_of is not None and context.as_of > as_of:
            msg = f"Observed market context for {asset} is newer than the scoring time"
            raise MarketContextUnavailableError(msg)
        return context


class AssetClassRoutingMarketContextProvider(MarketContextProvider):
    """Route context lookups to a per-asset-class provider.

    One scoring deployment can serve feeds with different sources and bar
    cadences (crypto ``coinbase_live``/1m next to equity ``eodhd``/1d). The
    router resolves the instrument's persisted asset class and delegates to
    that class's provider; classes without a dedicated provider use the
    default. An unresolvable instrument or resolver failure fails closed so
    the meta layer abstains rather than scoring against the wrong feed.
    """

    def __init__(
        self,
        *,
        default: MarketContextProvider,
        by_asset_class: Mapping[str, MarketContextProvider],
        asset_class_resolver: Callable[[str], str | None],
    ) -> None:
        self._default = default
        self._by_asset_class = dict(by_asset_class)
        self._resolve_asset_class = asset_class_resolver

    def get_context(self, asset: str, as_of: datetime | None = None) -> MarketContext:
        """Delegate to the provider configured for the asset's class."""
        if not self._by_asset_class:
            return self._default.get_context(asset, as_of)
        try:
            asset_class = self._resolve_asset_class(asset)
        except (OSError, TypeError, ValueError) as exc:
            msg = f"Failed to resolve the asset class for {asset}"
            raise MarketContextUnavailableError(msg) from exc
        if asset_class is None:
            msg = f"Unknown instrument {asset}: cannot select a market-context feed"
            raise MarketContextUnavailableError(msg)
        provider = self._by_asset_class.get(asset_class, self._default)
        return provider.get_context(asset, as_of)


# Regime-classification thresholds: per-bar log-return stdev + window trend.
# Conservative so a quiet range stays SIDEWAYS rather than over-classifying.
_DEFAULT_VOL_HIGH = 0.03
_DEFAULT_VOL_CRISIS = 0.06
_DEFAULT_TREND = 0.02
_MIN_RETURNS_FOR_VOL = 2


@dataclass(frozen=True)
class PriceObservation:
    """Point-in-time close used to derive market context."""

    timestamp: datetime
    close: float


class PriceBasedMarketContextProvider(MarketContextProvider):
    """Derive market regime + realized volatility from recent closes.

    The production provider is decoupled from the database via an injected
    ``history_loader(asset, limit, as_of)``. The loader returns point-in-time
    observations oldest→newest and must exclude data newer than ``as_of``.
    Insufficient, invalid, future, or stale history fails closed.
    """

    def __init__(
        self,
        history_loader: Callable[[str, int, datetime], Sequence[PriceObservation]],
        *,
        window: int = 20,
        max_age: timedelta = timedelta(hours=1),
        vol_high: float = _DEFAULT_VOL_HIGH,
        vol_crisis: float = _DEFAULT_VOL_CRISIS,
        trend_threshold: float = _DEFAULT_TREND,
    ) -> None:
        self._loader = history_loader
        self._window = max(_MIN_RETURNS_FOR_VOL, window)
        if max_age <= timedelta(0):
            msg = "max_age must be positive"
            raise ValueError(msg)
        self._max_age = max_age
        self._vol_high = vol_high
        self._vol_crisis = vol_crisis
        self._trend = trend_threshold

    def get_context(self, asset: str, as_of: datetime | None = None) -> MarketContext:
        stamp = as_of or datetime.now(tz=UTC)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            msg = "Market context as_of must be timezone-aware"
            raise MarketContextUnavailableError(msg)
        try:
            observations = list(self._loader(asset, self._window + 1, stamp) or [])
        except (OSError, TypeError, ValueError) as exc:
            msg = f"Failed to load observed price history for {asset}"
            raise MarketContextUnavailableError(msg) from exc
        if len(observations) < self._window + 1:
            msg = (
                f"Insufficient price history for {asset}: "
                f"{len(observations)}/{self._window + 1} observations"
            )
            raise MarketContextUnavailableError(msg)

        normalized: list[PriceObservation] = []
        for observation in observations:
            timestamp = observation.timestamp
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            close = float(observation.close)
            if not math.isfinite(close) or close <= 0:
                msg = f"Invalid observed close for {asset}"
                raise MarketContextUnavailableError(msg)
            normalized.append(PriceObservation(timestamp=timestamp.astimezone(UTC), close=close))
        normalized.sort(key=lambda item: item.timestamp)
        if any(left.timestamp >= right.timestamp for left, right in pairwise(normalized)):
            msg = f"Price history for {asset} is not strictly increasing"
            raise MarketContextUnavailableError(msg)

        latest = normalized[-1]
        stamp = stamp.astimezone(UTC)
        if latest.timestamp > stamp:
            msg = f"Price history for {asset} contains a future observation"
            raise MarketContextUnavailableError(msg)
        if stamp - latest.timestamp > self._max_age:
            msg = f"Latest price observation for {asset} is stale"
            raise MarketContextUnavailableError(msg)

        closes = [observation.close for observation in normalized]
        returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        if len(returns) < _MIN_RETURNS_FOR_VOL:
            msg = f"Insufficient valid returns for {asset}"
            raise MarketContextUnavailableError(msg)
        realized_vol = statistics.pstdev(returns)
        trend = (closes[-1] - closes[0]) / closes[0]
        return MarketContext(
            realized_vol_20d=realized_vol,
            regime=self._classify(trend, realized_vol),
            as_of=latest.timestamp,
        )

    def _classify(self, trend: float, realized_vol: float) -> MarketRegime:
        vol_high = realized_vol >= self._vol_high
        if realized_vol >= self._vol_crisis and trend <= -self._trend:
            return MarketRegime.CRISIS
        if trend >= self._trend:
            return MarketRegime.BULL_VOLATILE if vol_high else MarketRegime.BULL_QUIET
        if trend <= -self._trend:
            return MarketRegime.BEAR_VOLATILE if vol_high else MarketRegime.BEAR_QUIET
        return MarketRegime.SIDEWAYS


class MetaLabelService:
    """
    Layer 2: Meta-Labeling Service.

    Transforms raw strategy signals into meta-labeled outputs with:
    - Market context features
    - Bounded observed-feature meta score
    - Age decay weighting
    - Local score computation

    Usage:
        service = MetaLabelService(
            context_provider=ObservedMarketContextProvider(),
            scorer=DeterministicMetaScorer(),
        )
        meta_outputs = service.process_signals(signals)
    """

    def __init__(
        self,
        context_provider: MarketContextProvider | None = None,
        scorer: MetaScorer | None = None,
        config: MetaLabelConfig | None = None,
    ) -> None:
        """
        Initialize meta-labeling service.

        Args:
            context_provider: Provider for market context data
            scorer: Observed-feature scorer for the bounded meta value
            config: Service configuration
        """
        self.context_provider = context_provider or ObservedMarketContextProvider()
        self.config = config or MetaLabelConfig()
        self.scorer = scorer or DeterministicMetaScorer(
            regime_adjustment=self.config.regime_adjustment,
        )

        # Strategy-specific scorers (for example a calibrated production model).
        self._strategy_scorers: dict[str, MetaScorer] = {}

    def register_scorer(self, strategy_id: str, scorer: MetaScorer) -> None:
        """Register a custom observed-feature scorer for a specific strategy."""
        self._strategy_scorers[strategy_id] = scorer

    def get_scorer(self, strategy_id: str) -> MetaScorer:
        """Get a strategy scorer, falling back to the configured bounded scorer."""
        return self._strategy_scorers.get(strategy_id, self.scorer)

    def process_signal(
        self,
        signal: Signal,
        current_time: datetime | None = None,
        *,
        scoring_action_override: SignalAction | None = None,
    ) -> MetaLabelOutput:
        """
        Process a single signal through meta-labeling.

        Args:
            signal: Strategy signal to process
            current_time: Current time for age calculation (default: now)

        Returns:
            MetaLabelOutput with computed local score
        """
        if current_time is None:
            current_time = datetime.now(tz=UTC)

        scoring_view = build_scoring_view(
            signal,
            scoring_action_override=scoring_action_override,
        )

        # Get market context
        context = self.context_provider.get_context(
            asset=scoring_view.asset,
            as_of=current_time,
        )

        # Compute the bounded meta value. The default deterministic scorer is
        # explicitly uncalibrated; calibrated strategy scorers use the same seam.
        scorer = self.get_scorer(scoring_view.strategy_id)
        meta_probability = scorer.score_signal(scoring_view, context)

        # Compute meta-labeled output
        return MetaLabelOutput.compute(
            signal_id=scoring_view.signal_id,
            strategy_id=scoring_view.strategy_id,
            asset=scoring_view.asset,
            direction=scoring_view.direction,
            expected_return=scoring_view.expected_return,
            predicted_risk=scoring_view.predicted_risk,
            horizon_days=scoring_view.horizon_days,
            signal_timestamp=scoring_view.signal_timestamp,
            meta_probability=meta_probability,
            market_context=context,
            current_time=current_time,
        )

    def process_signals(
        self,
        signals: list[Signal],
        current_time: datetime | None = None,
        filter_expired: bool = True,
        *,
        scoring_action_overrides: Mapping[str, SignalAction] | None = None,
    ) -> list[MetaLabelOutput]:
        """
        Process multiple signals through meta-labeling.

        Args:
            signals: List of strategy signals
            current_time: Current time for age calculation
            filter_expired: Whether to filter out expired signals

        Returns:
            List of MetaLabelOutput objects
        """
        if current_time is None:
            current_time = datetime.now(tz=UTC)

        overrides = dict(scoring_action_overrides or {})
        unknown_ids = set(overrides) - {signal.signal_id for signal in signals}
        if unknown_ids:
            message = f"Scoring overrides reference unknown signals: {sorted(unknown_ids)!r}"
            raise ValueError(message)
        outputs = []
        for signal in signals:
            try:
                output = self.process_signal(
                    signal,
                    current_time,
                    scoring_action_override=overrides.get(signal.signal_id),
                )

                # Check expiration
                if filter_expired and output.is_expired:
                    logger.debug(
                        "Skipping expired signal %s (age=%.1f, horizon=%.1f)",
                        signal.signal_id,
                        output.age_days,
                        output.horizon_days,
                    )
                    continue

                outputs.append(output)
            except MarketContextUnavailableError as exc:
                logger.warning(
                    "Meta scoring abstained for signal %s: %s",
                    signal.signal_id,
                    exc,
                )
                continue
            except Exception:
                logger.exception("Failed to process signal %s", signal.signal_id)
                continue

        return outputs

    def process_signals_by_asset(
        self,
        signals: list[Signal],
        current_time: datetime | None = None,
    ) -> dict[str, list[MetaLabelOutput]]:
        """
        Process signals and group by asset.

        Args:
            signals: List of strategy signals
            current_time: Current time for age calculation

        Returns:
            Dictionary mapping asset to list of MetaLabelOutput
        """
        outputs = self.process_signals(signals, current_time)

        by_asset: dict[str, list[MetaLabelOutput]] = {}
        for output in outputs:
            if output.asset not in by_asset:
                by_asset[output.asset] = []
            by_asset[output.asset].append(output)

        return by_asset
