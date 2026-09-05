"""Multi-factor composite-alpha blending for the scoring ensemble (Q3).

Default-OFF: a ``FactorBlender`` is only constructed when
``SCORING_MULTI_FACTOR_ENABLED`` is set. When enabled it augments the signal
alpha with orthogonal per-asset factors (price momentum, a low-volatility tilt,
…) computed from the ``prices`` history — each standardized to a z-score over its
OWN rolling window (time-series), winsorized, and combined by configured weights
(equal-weight default). The blended contribution is added to ``alpha_raw`` before
the existing standardization, so when the blender is absent ``alpha_raw`` is
byte-identical to the single-alpha path.

Cross-sectional ranking is intentionally omitted: the live crypto universe (a few
Coinbase symbols) is far too small for a meaningful cross-section, so a
time-series composite is the correct construction here. The factor set is a
registry, so new factors (IC-weighted, feedback-derived) extend it without
touching the ensemble.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from lib_common.logging import get_logger
from lib_strategy.cross_sectional import winsorize

from .services.ensemble_service import RollingStats

logger = get_logger(__name__)

# A factor needs at least this many closes to be computable.
_MIN_CLOSES_FOR_RETURN = 2

# A factor is only standardized once its rolling window has at least this many
# samples (matches RollingStats' recompute threshold). Before that the window is at
# the cold-start default (std=0.1) which would amplify the z-score ~10x, so the
# factor is skipped from the blend until it has warmed — consistent with the
# missing-factor skip.
_MIN_FACTOR_SAMPLES = 2


def momentum(closes: Sequence[float], lookback: int) -> float | None:
    """Trailing return over ``lookback`` bars (oldest→newest closes). Positive = uptrend."""
    if len(closes) < lookback + 1:
        return None
    base = closes[-lookback - 1]
    if base == 0:
        return None
    return closes[-1] / base - 1.0


def low_volatility(closes: Sequence[float], lookback: int) -> float | None:
    """Negative realized volatility of log returns (a low-vol premium: calmer = higher)."""
    window = list(closes[-(lookback + 1) :])
    if len(window) < _MIN_CLOSES_FOR_RETURN:
        return None
    returns: list[float] = []
    for prev, cur in pairwise(window):
        if prev <= 0 or cur <= 0:
            continue
        returns.append(math.log(cur / prev))
    if len(returns) < _MIN_CLOSES_FOR_RETURN:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    return -float(variance**0.5)


#: The default factor registry: name -> (closes -> raw value). Each is standardized
#: to a z-score per asset before blending. Weights (and thus sign/emphasis) are
#: operator-configurable via SCORING_FACTOR_WEIGHTS; the default is equal-weight.
DEFAULT_FACTORS: dict[str, Callable[[Sequence[float], int], float | None]] = {
    "momentum": momentum,
    "low_volatility": low_volatility,
}


@dataclass
class FactorBlend:
    """Result of blending an asset's factors into a composite alpha contribution."""

    alpha: float
    breakdown: dict[str, float] = field(default_factory=dict)


class FactorBlender:
    """Compute + blend per-asset factors into a composite alpha contribution.

    Stateful only in the per-factor rolling windows (one ``RollingStats`` per
    ``factor:<name>:<asset>``), mirroring the ensemble's standardization. Pure of
    DB/clock otherwise — prices are supplied by an injected ``closes_provider``.
    """

    def __init__(
        self,
        *,
        closes_provider: Callable[[str, int], Sequence[float]],
        weights: Mapping[str, float] | None = None,
        lookback: int = 20,
        winsorize_z: float = 3.0,
        factors: Mapping[str, Callable[[Sequence[float], int], float | None]] | None = None,
    ) -> None:
        self._closes_provider = closes_provider
        self._weights = dict(weights or {})
        self._lookback = max(1, lookback)
        self._winsorize_z = winsorize_z
        self._factors = dict(factors or DEFAULT_FACTORS)
        self._stats: dict[str, RollingStats] = {}

    def _stats_for(self, name: str, asset: str) -> RollingStats:
        key = f"factor:{name}:{asset}"
        stats = self._stats.get(key)
        if stats is None:
            stats = RollingStats()
            self._stats[key] = stats
        return stats

    def blend(self, asset: str) -> FactorBlend:
        """Return the composite factor alpha + per-factor z-score breakdown for ``asset``.

        Each factor's raw value is standardized over its own rolling window,
        winsorized, then weighted (equal-weight default). A factor that can't be
        computed (insufficient history) is skipped, not zero-filled, so it never
        drags the blend toward 0. Returns a zero blend when no factor is available.
        """
        # The closes_provider is contracted to RETURN a sequence (the production
        # loader catches DB errors internally and yields []); factor computation on
        # the returned data is the failure mode we guard. Any arithmetic/type/value
        # error from a malformed price series degrades to no factor contribution —
        # an optional, opt-in feature must never fail a scoring request.
        try:
            closes = list(self._closes_provider(asset, self._lookback + 1))
            breakdown: dict[str, float] = {}
            weighted_sum = 0.0
            total_weight = 0.0
            for name, compute in self._factors.items():
                raw = compute(closes, self._lookback)
                if raw is None:
                    continue
                stats = self._stats_for(name, asset)
                stats.update(raw)
                if len(stats.values) < _MIN_FACTOR_SAMPLES:
                    # Cold window — not yet standardizable; skip until it warms.
                    continue
                z = winsorize((raw - stats.mean) / stats.std, self._winsorize_z)
                weight = self._weights.get(name, 1.0)
                breakdown[name] = z
                weighted_sum += weight * z
                total_weight += abs(weight)
        except (ArithmeticError, TypeError, ValueError):
            logger.exception("Factor blend failed for %s; using no factor contribution", asset)
            return FactorBlend(alpha=0.0)

        alpha = weighted_sum / total_weight if total_weight > 0 else 0.0
        return FactorBlend(alpha=alpha, breakdown=breakdown)
