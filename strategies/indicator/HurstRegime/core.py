"""Hurst-gated MACD trend or slow-stochastic reversion with ATR protection."""

from typing import Any

from lib_indicators import AverageTrueRange
from lib_indicators.oscillators import MACD
from lib_indicators.regime import HurstExponent, Stochastic
from lib_indicators.streaming_statistics import CrossOver
from lib_strategy.signals.bar_strategy import BarSignalStrategy
from lib_strategy.signals.pure_strategy import MarketState


class HurstRegimeCore(BarSignalStrategy):
    DEFAULT_STRATEGY_ID = "hurst_regime_v1"

    def configure(self) -> int:
        self.hurst_period = self.integer("hurst_period", 30, minimum=8)
        self.hurst_trend_threshold = self.number("hurst_trend_threshold", 0.55, maximum=1)
        self.hurst_reversion_threshold = self.number("hurst_reversion_threshold", 0.45, maximum=1)
        self.macd_fast = self.integer("macd_fast", 12)
        self.macd_slow = self.integer("macd_slow", 26)
        self.macd_signal = self.integer("macd_signal", 9)
        self.stoch_period = self.integer("stoch_period", 14)
        self.stoch_dfast = self.integer("stoch_dfast", 3)
        self.stoch_overbought = self.number("stoch_overbought", 80, maximum=100)
        self.stoch_oversold = self.number("stoch_oversold", 20, maximum=100)
        self.atr_period = self.integer("atr_period", 14)
        self.atr_stop_multiplier = self.number("atr_stop_multiplier", 3)
        if (
            self.hurst_reversion_threshold >= self.hurst_trend_threshold
            or self.stoch_oversold >= self.stoch_overbought
            or self.macd_fast >= self.macd_slow
        ):
            msg = "Regime thresholds, stochastic thresholds, and MACD periods must be ordered"
            raise ValueError(msg)
        return max(
            self.hurst_period,
            self.macd_slow + self.macd_signal,
            self.stoch_period + self.stoch_dfast + 2,
            self.atr_period + 1,
        )

    def create_indicators(self) -> dict[str, Any]:
        return {
            "hurst": HurstExponent(self.hurst_period),
            "macd": MACD(self.macd_fast, self.macd_slow, self.macd_signal),
            "stoch": Stochastic(self.stoch_period, self.stoch_dfast),
            "atr": AverageTrueRange(self.atr_period),
            "macd_cross": CrossOver(),
            "stoch_cross": CrossOver(),
        }

    def on_bar(self, bar: MarketState, indicators: dict[str, Any]) -> None:
        hurst = indicators["hurst"].update(bar.close)
        macd = indicators["macd"].update(bar.close)
        stoch = indicators["stoch"].update(bar.high, bar.low, bar.close)
        atr = indicators["atr"].update(bar.high, bar.low, bar.close)
        macd_cross = indicators["macd_cross"].update(macd[0] - macd[1]) if macd else 0
        stoch_cross = indicators["stoch_cross"].update(stoch[0] - stoch[1]) if stoch else 0
        if not self.can_decide(bar.symbol) or atr is None:
            return
        if self.state_for(bar.symbol).position:
            self.trail(bar, distance=atr * self.atr_stop_multiplier, use_extreme=True, strict=True)
            return
        direction = 0
        if hurst is not None and hurst > self.hurst_trend_threshold:
            direction = macd_cross
        elif hurst is not None and hurst < self.hurst_reversion_threshold and stoch is not None:
            if stoch[0] < self.stoch_oversold and stoch_cross > 0:
                direction = 1
            elif stoch[0] > self.stoch_overbought and stoch_cross < 0:
                direction = -1
        if direction:
            self.enter(
                bar,
                direction,
                stop_loss=bar.close - direction * atr * self.atr_stop_multiplier,
                custom={"extreme": bar.high if direction > 0 else bar.low},
            )
