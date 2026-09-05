"""
Layer 2: Per-Strategy Meta-Labeling

This module computes the per-strategy meta-labeled score by:
1. Computing age decay: w_age = max(0, 1 - age/H)
2. Using a bounded observed-feature meta value: tilt = 2*p - 1
3. Combining: score_local = S_raw * tilt * w_age

Reference: vynmatrix Scoring Engine Architecture v1.0, Section 4.2 & 8.2
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class MarketRegime(Enum):
    """Market regime classification for context-aware scoring."""

    BULL_QUIET = "bull_quiet"  # Low vol uptrend
    BULL_VOLATILE = "bull_volatile"  # High vol uptrend
    BEAR_QUIET = "bear_quiet"  # Low vol downtrend
    BEAR_VOLATILE = "bear_volatile"  # High vol downtrend
    SIDEWAYS = "sideways"  # Range-bound
    CRISIS = "crisis"  # Extreme volatility / tail event


@dataclass
class MarketContext:
    """
    Market context features for meta-labeling decisions.

    These observed features let the meta scorer determine if market conditions
    are favorable for a given strategy's signal.

    Attributes:
        vix_level: Current VIX value (e.g., 15.5)
        vix_percentile: VIX percentile rank over lookback (0-100)
        realized_vol_20d: 20-day realized volatility (annualized, e.g., 0.15 = 15%)
        breadth_score: Market breadth indicator (-1 to +1)
        regime: Current market regime classification
        news_sentiment: Aggregate news sentiment (-1 to +1)
        event_flags: Binary flags for known events (FOMC, earnings, etc.)
        iv_rank: Implied volatility rank (0-100)
        liquidity_score: Market liquidity indicator (0-1)
        correlation_regime: Cross-asset correlation level (0-1)

    All fields must be observed or derived from observed data. Optional fields
    remain ``None`` when that feature was not observed; callers must never fill
    them with neutral-looking constants.

    Example:
        context = MarketContext(
            regime=MarketRegime.BULL_QUIET,
            as_of=datetime.now(tz=UTC),
            vix_level=18.5,
            vix_percentile=45.0,
            realized_vol_20d=0.12,
            breadth_score=0.3,
            news_sentiment=0.1,
        )
    """

    # Required provenance for every observed/derived context.
    regime: MarketRegime
    as_of: datetime

    # Volatility context
    vix_level: float | None = None
    vix_percentile: float | None = None
    realized_vol_20d: float | None = None

    # Market breadth
    breadth_score: float | None = None  # -1 to +1

    # Sentiment & events
    news_sentiment: float | None = None  # -1 to +1, N_t in formulas
    event_flags: dict[str, bool] = field(default_factory=dict)

    # Options context
    iv_rank: float | None = None  # 0-100

    # Microstructure
    liquidity_score: float | None = None  # 0-1
    correlation_regime: float | None = None  # 0-1

    def __post_init__(self) -> None:
        """Reject malformed observations before they reach decisioning."""
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            msg = "MarketContext.as_of must be timezone-aware"
            raise ValueError(msg)

        ranges = {
            "vix_percentile": (self.vix_percentile, 0.0, 100.0),
            "breadth_score": (self.breadth_score, -1.0, 1.0),
            "news_sentiment": (self.news_sentiment, -1.0, 1.0),
            "iv_rank": (self.iv_rank, 0.0, 100.0),
            "liquidity_score": (self.liquidity_score, 0.0, 1.0),
            "correlation_regime": (self.correlation_regime, 0.0, 1.0),
        }
        for name, (value, minimum, maximum) in ranges.items():
            if value is not None and (
                not math.isfinite(value) or value < minimum or value > maximum
            ):
                msg = f"MarketContext.{name} must be within [{minimum}, {maximum}]"
                raise ValueError(msg)
        for name, value in (
            ("vix_level", self.vix_level),
            ("realized_vol_20d", self.realized_vol_20d),
        ):
            if value is not None and (not math.isfinite(value) or value < 0):
                msg = f"MarketContext.{name} must be finite and non-negative"
                raise ValueError(msg)

    def to_feature_vector(self) -> dict[str, float]:
        """Convert observed context to a meta-scorer feature dictionary."""
        bull_regimes = (MarketRegime.BULL_QUIET, MarketRegime.BULL_VOLATILE)
        volatile_regimes = (
            MarketRegime.BULL_VOLATILE,
            MarketRegime.BEAR_VOLATILE,
            MarketRegime.CRISIS,
        )
        features = {
            "regime_bull": 1.0 if self.regime in bull_regimes else 0.0,
            "regime_volatile": 1.0 if self.regime in volatile_regimes else 0.0,
        }
        optional = {
            "vix_level": self.vix_level,
            "vix_percentile": self.vix_percentile,
            "realized_vol_20d": self.realized_vol_20d,
            "breadth_score": self.breadth_score,
            "news_sentiment": self.news_sentiment,
            "iv_rank": self.iv_rank,
            "liquidity_score": self.liquidity_score,
            "correlation_regime": self.correlation_regime,
        }
        features.update({name: value for name, value in optional.items() if value is not None})
        return features

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "vix_level": self.vix_level,
            "vix_percentile": self.vix_percentile,
            "realized_vol_20d": self.realized_vol_20d,
            "breadth_score": self.breadth_score,
            "regime": self.regime.value,
            "news_sentiment": self.news_sentiment,
            "event_flags": self.event_flags,
            "iv_rank": self.iv_rank,
            "liquidity_score": self.liquidity_score,
            "correlation_regime": self.correlation_regime,
            "as_of": self.as_of.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MarketContext:
        """Create from a complete observed-context dictionary."""
        if "regime" not in data or "as_of" not in data:
            msg = "Market context requires regime and as_of provenance"
            raise ValueError(msg)
        regime = data["regime"]
        if isinstance(regime, str):
            regime = MarketRegime(regime)

        as_of = data["as_of"]
        if isinstance(as_of, str):
            as_of = datetime.fromisoformat(as_of)
        if not isinstance(as_of, datetime):
            msg = "Market context as_of must be an ISO timestamp or datetime"
            raise TypeError(msg)

        return cls(
            regime=regime,
            as_of=as_of,
            vix_level=data.get("vix_level"),
            vix_percentile=data.get("vix_percentile"),
            realized_vol_20d=data.get("realized_vol_20d"),
            breadth_score=data.get("breadth_score"),
            news_sentiment=data.get("news_sentiment"),
            event_flags=data.get("event_flags", {}),
            iv_rank=data.get("iv_rank"),
            liquidity_score=data.get("liquidity_score"),
            correlation_regime=data.get("correlation_regime"),
        )


@dataclass
class MetaLabelOutput:
    """
    Output from Layer 2 meta-labeling for a single strategy signal.

    Computes the local score using:
    - Age decay: w_age = max(0, 1 - age/H)
    - Bounded meta-value tilt: tilt = 2*p - 1
    - Local score: score_local = S_raw x tilt x w_age

    Attributes:
        signal_id: Reference to the original Signal
        strategy_id: Strategy identifier
        asset: Trading symbol
        s_raw: Sharpe-normalized raw signal strength (d x mu/sigma)
        meta_probability: Bounded meta value in [0, 1]; a probability only
            when produced by an explicitly calibrated scorer
        tilt: Transformed meta value (2*p - 1) ∈ [-1, 1]
        age_days: Signal age in days since emission
        horizon_days: Signal's prediction horizon (H)
        w_age: Age decay weight max(0, 1 - age/H)
        score_local: Final meta-labeled score (S_raw x tilt x w_age)
        market_context: Market context used for meta-labeling
        meta_features: Observed features used by the meta scorer
        computed_at: Timestamp of computation

    Mathematical:
        tilt = 2 x p_i - 1
        w_age = max(0, 1 - age_days / horizon_days)
        score_local = s_raw x tilt x w_age

    Example:
        # Given: direction=1, mu=0.008, sigma=0.02, p=0.7, age=1 day, H=5 days
        # s_raw = 1 x (0.008 / 0.02) = 0.4
        # tilt = 2 x 0.7 - 1 = 0.4
        # w_age = max(0, 1 - 1/5) = 0.8
        # score_local = 0.4 x 0.4 x 0.8 = 0.128
    """

    # Signal reference
    signal_id: str
    strategy_id: str
    asset: str

    # Raw signal strength
    s_raw: float  # d x (mu/sigma)

    # Meta-classifier output
    # Historical field name retained in the persisted contract. The value is a
    # probability only when its scorer is explicitly calibrated.
    meta_probability: float

    # Computed components
    tilt: float = field(init=False)  # 2*p - 1
    w_age: float = field(init=False)  # max(0, 1 - age/H)
    score_local: float = field(init=False)  # s_raw x tilt x w_age

    # Time components
    age_days: float
    horizon_days: float

    # Context (optional)
    market_context: MarketContext | None = None
    meta_features: dict[str, float] = field(default_factory=dict)

    # Timestamp
    computed_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def __post_init__(self) -> None:
        """Compute derived fields after initialization."""
        # Validate meta_probability
        if not 0.0 <= self.meta_probability <= 1.0:
            msg = f"meta_probability must be in [0, 1], got {self.meta_probability}"
            raise ValueError(msg)

        if self.horizon_days <= 0:
            msg = f"horizon_days must be positive, got {self.horizon_days}"
            raise ValueError(msg)

        # Compute tilt: transform p from [0,1] to [-1,1]
        self.tilt = 2.0 * self.meta_probability - 1.0

        # Compute age decay
        self.w_age = max(0.0, 1.0 - self.age_days / self.horizon_days)

        # Compute local score
        self.score_local = self.s_raw * self.tilt * self.w_age

    @property
    def is_expired(self) -> bool:
        """Check if signal has expired (age >= horizon)."""
        return self.age_days >= self.horizon_days

    @property
    def confidence_adjusted_score(self) -> float:
        """Score weighted by bounded-meta confidence (distance from 0.5)."""
        confidence = abs(self.meta_probability - 0.5) * 2  # 0 at p=0.5, 1 at p=0 or p=1
        return self.score_local * confidence

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "signal_id": self.signal_id,
            "strategy_id": self.strategy_id,
            "asset": self.asset,
            "s_raw": self.s_raw,
            "meta_probability": self.meta_probability,
            "tilt": self.tilt,
            "age_days": self.age_days,
            "horizon_days": self.horizon_days,
            "w_age": self.w_age,
            "score_local": self.score_local,
            "is_expired": self.is_expired,
            "market_context": self.market_context.to_dict() if self.market_context else None,
            "meta_features": self.meta_features,
            "computed_at": self.computed_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MetaLabelOutput:
        """Create from dictionary."""
        context_data = data.get("market_context")
        market_context = MarketContext.from_dict(context_data) if context_data else None

        computed_at = data.get("computed_at")
        if isinstance(computed_at, str):
            computed_at = datetime.fromisoformat(computed_at)
        elif computed_at is None:
            computed_at = datetime.now(tz=UTC)

        return cls(
            signal_id=data["signal_id"],
            strategy_id=data["strategy_id"],
            asset=data["asset"],
            s_raw=data["s_raw"],
            meta_probability=data["meta_probability"],
            age_days=data["age_days"],
            horizon_days=data["horizon_days"],
            market_context=market_context,
            meta_features=data.get("meta_features", {}),
            computed_at=computed_at,
        )

    @classmethod
    def compute(
        cls,
        signal_id: str,
        strategy_id: str,
        asset: str,
        direction: int,
        expected_return: float,
        predicted_risk: float,
        horizon_days: float,
        signal_timestamp: datetime,
        meta_probability: float,
        market_context: MarketContext | None = None,
        current_time: datetime | None = None,
    ) -> MetaLabelOutput:
        """
        Factory method to compute MetaLabelOutput from signal components.

        Args:
            signal_id: Unique signal identifier
            strategy_id: Strategy identifier
            asset: Trading symbol
            direction: Signal direction (+1, -1, 0)
            expected_return: mu - expected return
            predicted_risk: sigma - predicted volatility
            horizon_days: H - prediction horizon
            signal_timestamp: When signal was generated
            meta_probability: Bounded scorer output; probability only for a
                calibrated scorer
            market_context: Optional market context
            current_time: Current time for age calculation (default: now)

        Returns:
            MetaLabelOutput with computed score_local
        """
        if current_time is None:
            current_time = datetime.now(tz=UTC)

        # Compute raw Sharpe-like strength
        s_raw = direction * (expected_return / predicted_risk) if predicted_risk > 0 else 0.0

        # Compute age in days
        age_delta = current_time - signal_timestamp
        age_days = age_delta.total_seconds() / 86400.0  # seconds to days

        return cls(
            signal_id=signal_id,
            strategy_id=strategy_id,
            asset=asset,
            s_raw=s_raw,
            meta_probability=meta_probability,
            age_days=age_days,
            horizon_days=horizon_days,
            market_context=market_context,
            meta_features=market_context.to_feature_vector() if market_context else {},
            computed_at=current_time,
        )


def logit(p: float, eps: float = 1e-7) -> float:
    """
    Compute logit (log-odds) of probability.

    logit(p) = log(p / (1-p))

    Args:
        p: Probability in [0, 1]
        eps: Small value to avoid log(0)

    Returns:
        Log-odds value in (-∞, +∞)
    """
    p_clipped = max(eps, min(1.0 - eps, p))
    return math.log(p_clipped / (1.0 - p_clipped))


def sigmoid(z: float) -> float:
    """
    Compute sigmoid (inverse logit).

    sigmoid(z) = 1 / (1 + e^(-z))

    Args:
        z: Log-odds value

    Returns:
        Probability in [0, 1]
    """
    # Numerically stable sigmoid
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    exp_z = math.exp(z)
    return exp_z / (1.0 + exp_z)
