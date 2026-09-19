"""Volatility-adaptive exponential price filter, evaluated on every accepted bar."""

import math
from typing import Any, ClassVar

from lib_indicators import AverageTrueRange
from lib_indicators.streaming_statistics import RollingStatistics
from lib_strategy.signals.bar_strategy import BarSignalStrategy
from lib_strategy.signals.pure_strategy import MarketState

_MIN_VOLATILITY = 0.001
_MAX_VOLATILITY = 0.1


class TimeDecayAdaptiveEMACore(BarSignalStrategy):
    DEFAULT_STRATEGY_ID = "time_decay_adaptive_ema_v1"
    REQUIRED_POSITIVE_MODEL_FIELDS = ("trail_distance",)

    STREAM_FIELDS: ClassVar[dict[str, str]] = {
        "previous": "number?",
        "ewma_vol": "number?",
        "aema": "number?",
    }

    def configure(self) -> int:
        self.vol_calc_window = self.integer("vol_calc_window", 20, minimum=2)
        self.vol_ema_period = self.integer("vol_ema_period", 10)
        self.alpha_base_period = self.integer("alpha_base_period", 10)
        self.max_period_cap = self.integer("max_period_cap", 150)
        self.lambda_decay = self.number("lambda_decay", 50, inclusive=True)
        self.atr_period = self.integer("atr_period", 14)
        self.atr_multiplier = self.number("atr_multiplier", 2)
        if self.max_period_cap < self.alpha_base_period:
            msg = "max_period_cap must be at least alpha_base_period"
            raise ValueError(msg)
        return max(self.vol_calc_window + 2, self.atr_period + 1)

    def create_indicators(self) -> dict[str, Any]:
        return {
            "returns": RollingStatistics(self.vol_calc_window),
            "atr": AverageTrueRange(self.atr_period),
            "previous": None,
            "ewma_vol": None,
            "aema": None,
        }

    def on_bar(self, bar: MarketState, indicators: dict[str, Any]) -> None:
        previous = indicators["previous"]
        indicators["previous"] = bar.close
        atr = indicators["atr"].update(bar.high, bar.low, bar.close)
        stats = (
            indicators["returns"].update(bar.close / previous - 1) if previous is not None else None
        )
        if stats is None:
            return
        volatility = max(_MIN_VOLATILITY, stats[1])
        prior_vol = indicators["ewma_vol"]
        weight = 2 / (self.vol_ema_period + 1)
        ewma_vol = (
            volatility if prior_vol is None else weight * volatility + (1 - weight) * prior_vol
        )
        indicators["ewma_vol"] = ewma_vol
        alpha0 = 2 / (self.alpha_base_period + 1)
        alpha = max(
            2 / (self.max_period_cap + 1),
            min(alpha0, alpha0 * math.exp(-self.lambda_decay * min(_MAX_VOLATILITY, ewma_vol))),
        )
        prior_aema = indicators["aema"]
        aema = bar.close if prior_aema is None else alpha * bar.close + (1 - alpha) * prior_aema
        indicators["aema"] = aema
        if not self.can_decide(bar.symbol) or prior_aema is None or atr is None:
            return
        model = self.state_for(bar.symbol)
        if model.position:
            self.trail(bar, distance=float(model.custom["trail_distance"]), intrabar=True)
            return
        direction = 0
        if bar.close > aema and previous <= prior_aema:
            direction = 1
        elif bar.close < aema and previous >= prior_aema:
            direction = -1
        if direction:
            distance = atr * self.atr_multiplier
            self.enter(
                bar,
                direction,
                stop_loss=bar.close - direction * distance,
                custom={"trail_distance": distance},
            )
