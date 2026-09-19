"""Williams %R recovery aligned with the EMA trend and MACD histogram."""

from collections import deque
from typing import Any, ClassVar

from lib_indicators import AverageTrueRange, ExponentialMovingAverage
from lib_indicators.oscillators import MACD
from lib_indicators.streaming_statistics import CrossOver
from lib_strategy.signals.bar_strategy import BarSignalStrategy
from lib_strategy.signals.pure_strategy import MarketState


class WilliamsPullbackCore(BarSignalStrategy):
    DEFAULT_STRATEGY_ID = "williams_pullback_v1"

    STREAM_FIELDS: ClassVar[dict[str, str]] = {"ranges": "pairs"}

    def configure(self) -> int:
        self.trend_period = self.integer("trend_period", 30)
        self.wr_period = self.integer("wr_period", 7)
        self.wr_oversold = self.number("wr_oversold", -80, minimum=-100, maximum=0)
        self.wr_overbought = self.number("wr_overbought", -20, minimum=-100, maximum=0)
        self.macd_fast = self.integer("macd_fast", 7)
        self.macd_slow = self.integer("macd_slow", 30)
        self.macd_signal = self.integer("macd_signal", 7)
        self.atr_period = self.integer("atr_period", 7)
        self.atr_stop_multiplier = self.number("atr_stop_multiplier", 3)
        if self.wr_oversold >= self.wr_overbought or self.macd_fast >= self.macd_slow:
            msg = "Williams thresholds and MACD periods must be ordered"
            raise ValueError(msg)
        return max(
            self.trend_period,
            self.wr_period + 1,
            self.macd_slow + self.macd_signal - 1,
            self.atr_period + 1,
        )

    def create_indicators(self) -> dict[str, Any]:
        return {
            "ema": ExponentialMovingAverage(self.trend_period),
            "macd": MACD(self.macd_fast, self.macd_slow, self.macd_signal),
            "atr": AverageTrueRange(self.atr_period),
            "ranges": deque(maxlen=self.wr_period),
            "oversold_cross": CrossOver(),
            "overbought_cross": CrossOver(),
        }

    def on_bar(self, bar: MarketState, indicators: dict[str, Any]) -> None:
        ema = indicators["ema"].update(bar.close)
        macd = indicators["macd"].update(bar.close)
        atr = indicators["atr"].update(bar.high, bar.low, bar.close)
        ranges = indicators["ranges"]
        ranges.append((bar.high, bar.low))
        buy_cross = sell_cross = 0
        if len(ranges) == self.wr_period:
            high, low = max(value[0] for value in ranges), min(value[1] for value in ranges)
            if high > low:
                wr = -100 * (high - bar.close) / (high - low)
                buy_cross = indicators["oversold_cross"].update(wr - self.wr_oversold)
                sell_cross = indicators["overbought_cross"].update(wr - self.wr_overbought)
        if not self.can_decide(bar.symbol) or ema is None or macd is None or atr is None:
            return
        if self.state_for(bar.symbol).position:
            self.trail(bar, distance=atr * self.atr_stop_multiplier, use_extreme=True, strict=True)
            return
        direction = 0
        if buy_cross > 0 and bar.close > ema and macd[2] > 0:
            direction = 1
        elif sell_cross < 0 and bar.close < ema and macd[2] < 0:
            direction = -1
        if direction:
            self.enter(
                bar,
                direction,
                stop_loss=bar.close - direction * atr * self.atr_stop_multiplier,
                custom={"extreme": bar.high if direction > 0 else bar.low},
            )
