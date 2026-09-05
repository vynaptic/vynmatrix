"""Score-calculation helpers extracted from :class:`ScoreEngine`.

Separates the read-only score-computation pipeline from
``ScoreEngine``'s orchestration responsibilities (signal ingestion,
canonical-write mirror, binding evaluation). The calculator only needs
read access to the store and the weight table; it never mutates state.

Two responsibilities:

* :func:`apply_decay` — exponential time decay so older signals
  contribute less to the aggregated score.
* :class:`ScoreCalculator` — computes asset / sector / industry / index
  / market scores by pulling signals from the store, applying decay,
  and aggregating with strategy weights.

The store interface is intentionally pluggable — in-memory test doubles may not
expose the per-axis ``list_signals_by_*`` helpers, so each classified axis can
fall back to a full scan via ``list_all_signals``. Market aggregation is always
explicitly partitioned by asset class; a storage failure propagates instead of
fabricating a zero crypto context.
"""

from __future__ import annotations

from collections.abc import Callable

from lib_common.asset_classes import normalize_asset_class
from lib_common.logging import get_logger

from .models import ScoreComponent, ScoreRecord, SignalRecord
from .services.ensemble_service import RollingStats
from .storage_base import ScoreStore

logger = get_logger(__name__)


def apply_decay(
    signals: list[SignalRecord],
    *,
    half_life_bars: int,
) -> list[SignalRecord]:
    """Apply exponential decay based on recency.

    Signals are assumed to be ordered newest-first (matching the store
    contract). Each signal's ``score_value`` is multiplied by
    ``0.5 ** (age / half_life_bars)`` where ``age`` is its index in the
    list. ``half_life_bars`` is clamped to at least 1 by callers.
    """
    if not signals:
        return signals

    decayed: list[SignalRecord] = []
    for idx, sig in enumerate(signals):
        decay = 0.5 ** (idx / half_life_bars)
        decayed.append(
            SignalRecord(
                signal_id=sig.signal_id,
                strategy_id=sig.strategy_id,
                strategy_type=sig.strategy_type,
                symbol=sig.symbol,
                action=sig.action,
                confidence=sig.confidence,
                timestamp=sig.timestamp,
                metadata=sig.metadata,
                sector=sig.sector,
                industry=sig.industry,
                index=sig.index,
                entry_price=sig.entry_price,
                stop_loss=sig.stop_loss,
                take_profit=sig.take_profit,
                size_hint=sig.size_hint,
                asset_class=sig.asset_class,
                instrument_id=sig.instrument_id,
                strategy_version=sig.strategy_version,
                run_id=sig.run_id,
                source=sig.source,
                score_value=sig.score_value * decay,
            )
        )
    return decayed


class ScoreCalculator:
    """Compute asset / sector / industry / index / market scores."""

    def __init__(
        self,
        store: ScoreStore,
        *,
        weight_for: Callable[[str], float],
        half_life_bars: int,
    ) -> None:
        self._store = store
        self._weight_for = weight_for
        self._half_life_bars = max(1, half_life_bars)
        # Per-(scope, target) rolling stats so hierarchy scores (sector/industry/
        # index/market) are standardized to a z-score like the asset path. The
        # 3-tier binding thresholds then compare every tier on one scale (SC-3) —
        # previously sector/market were raw weighted means (~0.1) while the asset
        # gate used a z-score (~[-3,3]), so a single threshold meant different
        # things per tier.
        self._rolling: dict[str, RollingStats] = {}
        self._rolling_window = 100

    # ------------------------------------------------------------------
    # Public per-axis compute methods
    # ------------------------------------------------------------------

    def asset(self, symbol: str) -> ScoreRecord:
        signals = self._decay(self._store.list_signals(symbol, limit=200))
        if not signals:
            return ScoreRecord(target=symbol, scope="asset", score=0.0, components=[])
        return self._aggregate(symbol, "asset", signals)

    def sector(self, sector: str) -> ScoreRecord:
        signals = self._fetch_or_scan(
            sector,
            scope="sector",
            store_method="list_signals_by_sector",
            attribute="sector",
        )
        if not signals:
            return ScoreRecord(target=sector, scope="sector", score=0.0, components=[])
        return self._aggregate(sector, "sector", signals)

    def industry(self, industry: str) -> ScoreRecord:
        signals = self._fetch_or_scan(
            industry,
            scope="industry",
            store_method="list_signals_by_industry",
            attribute="industry",
        )
        if not signals:
            return ScoreRecord(target=industry, scope="industry", score=0.0, components=[])
        return self._aggregate(industry, "industry", signals)

    def index(self, index_name: str) -> ScoreRecord:
        signals = self._fetch_or_scan(
            index_name,
            scope="index",
            store_method="list_signals_by_index",
            attribute="index",
        )
        if not signals:
            return ScoreRecord(target=index_name, scope="index", score=0.0, components=[])
        return self._aggregate(index_name, "index", signals)

    def market(self, asset_class: str) -> ScoreRecord | None:
        if not str(asset_class or "").strip():
            return None
        normalized_asset_class = normalize_asset_class(
            asset_class,
            field_name="market-score asset class",
        )
        signals = self._decay(self._store.list_all_signals(limit=500))
        signals = [
            signal
            for signal in signals
            if signal.asset_class is not None
            and normalize_asset_class(
                signal.asset_class,
                field_name="persisted signal asset class",
            )
            == normalized_asset_class
        ]
        if not signals:
            return None
        return self._aggregate(normalized_asset_class, "market", signals)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _decay(self, signals: list[SignalRecord]) -> list[SignalRecord]:
        return apply_decay(signals, half_life_bars=self._half_life_bars)

    def _fetch_or_scan(
        self,
        target: str,
        *,
        scope: str,
        store_method: str,
        attribute: str,
    ) -> list[SignalRecord]:
        """Prefer the store's per-axis lookup; fall back to a full scan."""
        del scope  # Kept as an explicit keyword for call-site readability.
        filtered: list[SignalRecord] = []
        store_lookup = getattr(self._store, store_method, None)
        if callable(store_lookup):
            try:
                filtered = self._decay(store_lookup(target))
            except Exception:
                logger.exception("%s failed, falling back to full scan", store_method)
        if not filtered:
            all_signals = self._store.list_all_signals(limit=500)
            filtered = self._decay(
                [sig for sig in all_signals if getattr(sig, attribute) == target]
            )
        return filtered

    def _aggregate(
        self,
        target: str,
        scope: str,
        signals: list[SignalRecord],
    ) -> ScoreRecord:
        components: list[ScoreComponent] = []
        weighted_sum = 0.0
        weight_total = 0.0

        for sig in signals:
            weight = self._weight_for(sig.strategy_id)
            weighted_sum += sig.score_value
            weight_total += weight
            raw_value = sig.score_value / weight if weight else sig.score_value
            components.append(
                ScoreComponent(
                    strategy_id=sig.strategy_id,
                    weight=weight,
                    raw_value=raw_value,
                    weighted_value=sig.score_value,
                    confidence=sig.confidence,
                    timestamp=sig.timestamp,
                )
            )

        raw_score = weighted_sum / weight_total if weight_total else 0.0
        # Standardize to a z-score against this (scope, target)'s recent history
        # so the score is on the same scale the asset gate uses (SC-3). Standardize
        # with the prior stats, then fold this raw value in.
        stats = self._rolling_stats_for(scope, target)
        standardized = (raw_score - stats.mean) / stats.std
        stats.update(raw_score)
        return ScoreRecord(
            target=target,
            scope=scope,
            score=standardized,
            components=components,
            metadata={
                "raw_score": raw_score,
                "alpha_mean": stats.mean,
                "alpha_std": stats.std,
            },
        )

    def _rolling_stats_for(self, scope: str, target: str) -> RollingStats:
        key = f"{scope}:{target}"
        if key not in self._rolling:
            self._rolling[key] = RollingStats(window_size=self._rolling_window)
        return self._rolling[key]


__all__ = ["ScoreCalculator", "apply_decay"]
