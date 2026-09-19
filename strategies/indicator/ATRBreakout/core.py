"""Prior-close breakout gated by ATR expansion, with a bar-count exit."""

from collections import deque
from typing import Any, ClassVar

from lib_indicators import AverageTrueRange, SimpleMovingAverage
from lib_strategy.signals.bar_strategy import BarSignalStrategy
from lib_strategy.signals.pure_strategy import MarketState


class ATRBreakoutCore(BarSignalStrategy):
    DEFAULT_STRATEGY_ID = "atr_breakout_v1"

    STREAM_FIELDS: ClassVar[dict[str, str]] = {"closes": "numbers"}

    def configure(self) -> int:
        self.atr_period = self.integer("atr_period", 14)
        self.lookback = self.integer("lookback", 30)
        self.atr_thresh = self.number("atr_thresh", 1.2)
        self.trail_atr_mult = self.number("trail_atr_mult", 5.0)
        self.use_time_exit = self.boolean("use_time_exit", True)
        self.max_hold_days = self.integer("max_hold_days", 7)
        return self.atr_period + self.lookback + 1

    def create_indicators(self) -> dict[str, Any]:
        return {
            "atr": AverageTrueRange(self.atr_period),
            "atr_mean": SimpleMovingAverage(self.lookback),
            "closes": deque(maxlen=self.lookback),
        }

    def on_bar(self, bar: MarketState, indicators: dict[str, Any]) -> None:
        closes = indicators["closes"]
        prior_high = max(closes) if len(closes) == self.lookback else None
        prior_low = min(closes) if len(closes) == self.lookback else None
        prior_atr_mean = indicators["atr_mean"].value
        closes.append(bar.close)
        atr = indicators["atr"].update(bar.high, bar.low, bar.close)
        if atr is not None:
            indicators["atr_mean"].update(atr)
        if not self.can_decide(bar.symbol) or atr is None:
            return
        model = self.state_for(bar.symbol)
        if model.position:
            if self.trail(bar, distance=atr * self.trail_atr_mult):
                return
            if self.use_time_exit and model.bars_in_trade >= self.max_hold_days:
                self.close(bar, "maximum_holding_bars")
            return
        if prior_atr_mean is None or prior_high is None or prior_low is None:
            return
        if atr <= prior_atr_mean * self.atr_thresh:
            return
        direction = 1 if bar.close > prior_high else -1 if bar.close < prior_low else 0
        if direction:
            self.enter(bar, direction, stop_loss=bar.close - direction * atr * self.trail_atr_mult)
