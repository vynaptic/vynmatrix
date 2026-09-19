"""Rolling traded-volume VWAP reversion with nested deviation and adaptive protection."""

import math
from collections import deque
from typing import Any, ClassVar

from lib_indicators import AverageTrueRange, SimpleMovingAverage
from lib_indicators.oscillators import DirectionalMovement, RelativeStrengthIndex
from lib_strategy.signals.bar_strategy import BarSignalStrategy
from lib_strategy.signals.pure_strategy import MarketState


class AdaptiveVWAPMeanReversionCore(BarSignalStrategy):
    DEFAULT_STRATEGY_ID = "adaptive_vwap_mean_reversion_v1"
    REQUIRED_BOOLEAN_MODEL_FIELDS = ("tight_trail",)

    STREAM_FIELDS: ClassVar[dict[str, str]] = {"pv": "pairs"}

    def configure(self) -> int:
        if self.config.get("volume_semantics") != "traded":
            msg = "VWAP requires explicit volume_semantics=traded"
            raise ValueError(msg)
        self.vwap_period = self.integer("vwap_period", 20)
        self.std_dev_period = self.integer("std_dev_period", 20)
        self.entry_std_threshold = self.number("entry_std_threshold", 1)
        self.tight_trail_threshold = self.number("tight_trail_threshold", 0.5)
        self.adx_period = self.integer("adx_period", 14)
        self.adx_weak_threshold = self.number("adx_weak_threshold", 30, maximum=100)
        self.adx_strong_threshold = self.number("adx_strong_threshold", 35, maximum=100)
        self.rsi_period = self.integer("rsi_period", 14)
        self.rsi_oversold = self.number("rsi_oversold", 45, maximum=100)
        self.rsi_overbought = self.number("rsi_overbought", 55, maximum=100)
        self.atr_period = self.integer("atr_period", 14)
        self.atr_stop_mult = self.number("atr_stop_mult", 2.5)
        self.atr_trail_mult_wide = self.number("atr_trail_mult_wide", 1.5)
        self.atr_trail_mult_tight = self.number("atr_trail_mult_tight", 0.5)
        if (
            self.adx_weak_threshold >= self.adx_strong_threshold
            or self.rsi_oversold >= self.rsi_overbought
            or self.atr_trail_mult_tight > self.atr_trail_mult_wide
        ):
            msg = "VWAP thresholds and trailing distances must be ordered"
            raise ValueError(msg)
        return max(
            self.vwap_period + self.std_dev_period - 1,
            2 * self.adx_period,
            self.rsi_period + 1,
            self.atr_period + 1,
        )

    def create_indicators(self) -> dict[str, Any]:
        return {
            "pv": deque(maxlen=self.vwap_period),
            "dispersion": SimpleMovingAverage(self.std_dev_period),
            "adx": DirectionalMovement(self.adx_period),
            "rsi": RelativeStrengthIndex(self.rsi_period),
            "atr": AverageTrueRange(self.atr_period),
        }

    def on_bar(self, bar: MarketState, indicators: dict[str, Any]) -> None:
        adx = indicators["adx"].update(bar.high, bar.low, bar.close)
        rsi = indicators["rsi"].update(bar.close)
        atr = indicators["atr"].update(bar.high, bar.low, bar.close)
        pv = indicators["pv"]
        pv.append((bar.close * bar.volume, bar.volume))
        total_volume = math.fsum(row[1] for row in pv)
        stopped = (
            self.can_decide(bar.symbol)
            and bool(self.state_for(bar.symbol).position)
            and self.trail(bar, distance=0, intrabar=True)
        )
        if len(pv) < self.vwap_period or total_volume <= 0:
            indicators["dispersion"].reset()
            return
        vwap = math.fsum(row[0] for row in pv) / total_volume
        variance = indicators["dispersion"].update((bar.close - vwap) ** 2)
        if (
            not self.can_decide(bar.symbol)
            or stopped
            or variance is None
            or atr is None
            or adx is None
            or rsi is None
        ):
            return
        std = math.sqrt(variance)
        model = self.state_for(bar.symbol)
        if model.position:
            if adx > self.adx_strong_threshold:
                self.close(bar, "strong_trend")
            elif model.position * (bar.close - vwap) >= 0:
                self.close(bar, "vwap_reversion")
            else:
                tight = model.position * (bar.close - vwap) > -std * self.tight_trail_threshold
                model.custom["tight_trail"] = bool(model.custom.get("tight_trail", False) or tight)
                multiplier = (
                    self.atr_trail_mult_tight
                    if model.custom["tight_trail"]
                    else self.atr_trail_mult_wide
                )
                self.trail(bar, distance=atr * multiplier, intrabar=True)
            return
        if std <= 0 or bar.volume <= 0 or adx >= self.adx_weak_threshold:
            return
        direction = 0
        if bar.close < vwap - std * self.entry_std_threshold and rsi < self.rsi_oversold:
            direction = 1
        elif bar.close > vwap + std * self.entry_std_threshold and rsi > self.rsi_overbought:
            direction = -1
        if direction:
            self.enter(
                bar,
                direction,
                stop_loss=bar.close - direction * atr * self.atr_stop_mult,
                custom={"tight_trail": False},
            )
