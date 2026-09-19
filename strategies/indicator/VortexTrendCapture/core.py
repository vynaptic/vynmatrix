"""Vortex crossover with SMA alignment and an ATR percentage ceiling."""

from typing import Any

from lib_indicators import AverageTrueRange, SimpleMovingAverage, Vortex
from lib_indicators.streaming_statistics import CrossOver
from lib_strategy.signals.bar_strategy import BarSignalStrategy
from lib_strategy.signals.pure_strategy import MarketState


class VortexTrendCaptureCore(BarSignalStrategy):
    DEFAULT_STRATEGY_ID = "vortex_trend_capture_v1"

    def configure(self) -> int:
        self.vortex_period = self.integer("vortex_period", 30)
        self.long_term_ma_period = self.integer("long_term_ma_period", 30)
        self.atr_period = self.integer("atr_period", 7)
        self.atr_threshold = self.number("atr_threshold", 0.05)
        self.atr_stop_multiplier = self.number("atr_stop_multiplier", 3)
        return max(self.vortex_period + 2, self.long_term_ma_period, self.atr_period + 1)

    def create_indicators(self) -> dict[str, Any]:
        return {
            "vortex": Vortex(self.vortex_period),
            "sma": SimpleMovingAverage(self.long_term_ma_period),
            "atr": AverageTrueRange(self.atr_period),
            "cross": CrossOver(),
        }

    def on_bar(self, bar: MarketState, indicators: dict[str, Any]) -> None:
        vortex = indicators["vortex"]
        vortex.update(bar.high, bar.low, bar.close)
        sma = indicators["sma"].update(bar.close)
        atr = indicators["atr"].update(bar.high, bar.low, bar.close)
        cross = 0
        if vortex.vi_plus is not None and vortex.vi_minus is not None:
            cross = indicators["cross"].update(vortex.vi_plus - vortex.vi_minus)
        if not self.can_decide(bar.symbol) or atr is None or sma is None:
            return
        if self.state_for(bar.symbol).position:
            self.trail(bar, distance=atr * self.atr_stop_multiplier, use_extreme=True, strict=True)
            return
        if atr / bar.close >= self.atr_threshold:
            return
        if (cross > 0 and bar.close > sma) or (cross < 0 and bar.close < sma):
            self.enter(
                bar,
                cross,
                stop_loss=bar.close - cross * atr * self.atr_stop_multiplier,
                custom={"extreme": bar.high if cross > 0 else bar.low},
            )
