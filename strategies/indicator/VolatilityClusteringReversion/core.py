"""Fade an exact volatility-cluster length subject to price-trend alignment."""

import math
from collections import deque
from typing import Any, ClassVar

from lib_indicators import AverageTrueRange, SimpleMovingAverage
from lib_indicators.streaming_statistics import RollingStatistics
from lib_strategy.signals.bar_strategy import BarSignalStrategy
from lib_strategy.signals.pure_strategy import MarketState


class VolatilityClusteringReversionCore(BarSignalStrategy):
    DEFAULT_STRATEGY_ID = "volatility_clustering_reversion_v1"

    STREAM_FIELDS: ClassVar[dict[str, str]] = {
        "previous": "number?",
        "closes": "numbers",
        "high_vol_streak": "integer",
    }

    def configure(self) -> int:
        self.vol_window = self.integer("vol_window", 7, minimum=2)
        self.vol_stats_window = self.integer("vol_stats_window", 30, minimum=2)
        self.vol_cluster_threshold_factor = self.number("vol_cluster_threshold_factor", 1)
        self.vol_cluster_days_trigger = self.integer("vol_cluster_days_trigger", 3)
        self.trend_filter_sma_window = self.integer("trend_filter_sma_window", 30)
        self.atr_window_sl = self.integer("atr_window_sl", 14)
        self.atr_multiplier_sl = self.number("atr_multiplier_sl", 2)
        self.trading_days_per_year = self.integer("trading_days_per_year", 252)
        return max(
            self.vol_window + self.vol_stats_window,
            self.trend_filter_sma_window,
            self.atr_window_sl + 1,
            self.vol_cluster_days_trigger + 1,
        )

    def create_indicators(self) -> dict[str, Any]:
        return {
            "returns": RollingStatistics(self.vol_window),
            "volatility": RollingStatistics(self.vol_stats_window),
            "sma": SimpleMovingAverage(self.trend_filter_sma_window),
            "atr": AverageTrueRange(self.atr_window_sl),
            "previous": None,
            "closes": deque(maxlen=self.vol_cluster_days_trigger + 1),
            "high_vol_streak": 0,
        }

    def on_bar(self, bar: MarketState, indicators: dict[str, Any]) -> None:
        previous = indicators["previous"]
        indicators["previous"] = bar.close
        indicators["closes"].append(bar.close)
        sma = indicators["sma"].update(bar.close)
        atr = indicators["atr"].update(bar.high, bar.low, bar.close)
        returns = (
            indicators["returns"].update(bar.close / previous - 1) if previous is not None else None
        )
        volatility = (
            returns[1] * math.sqrt(self.trading_days_per_year) if returns is not None else None
        )
        stats = indicators["volatility"].update(volatility) if volatility is not None else None
        # The cluster length is indicator state, not a decision: it has to rebuild
        # from replayed bars exactly as the rolling statistics above do. Advancing
        # it below the ``can_decide`` gate would reset it to zero on every cold
        # start, so a worker restarted inside a volatility cluster would count a
        # fresh one and enter where a continuously running worker never would.
        if stats is not None and volatility is not None:
            high_vol = volatility > stats[0] + self.vol_cluster_threshold_factor * stats[1]
            indicators["high_vol_streak"] = indicators["high_vol_streak"] + 1 if high_vol else 0
        if not self.can_decide(bar.symbol) or stats is None or sma is None or atr is None:
            return
        if self.state_for(bar.symbol).position:
            self.trail(bar, distance=atr * self.atr_multiplier_sl, intrabar=True)
            return
        if indicators["high_vol_streak"] != self.vol_cluster_days_trigger:
            return
        closes = indicators["closes"]
        if len(closes) <= self.vol_cluster_days_trigger:
            return
        direction = 1 if bar.close < closes[0] else -1 if bar.close > closes[0] else 0
        if direction * (bar.close - sma) > 0:
            self.enter(
                bar, direction, stop_loss=bar.close - direction * atr * self.atr_multiplier_sl
            )
