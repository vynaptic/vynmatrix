"""Strict body engulfing confirmed by Wilder RSI extremes."""

from typing import Any, ClassVar

from lib_indicators.oscillators import RelativeStrengthIndex
from lib_strategy.signals.bar_strategy import BarSignalStrategy
from lib_strategy.signals.pure_strategy import MarketState


class EngulfingPatternCore(BarSignalStrategy):
    DEFAULT_STRATEGY_ID = "engulfing_pattern_v1"

    STREAM_FIELDS: ClassVar[dict[str, str]] = {"previous": "pair?"}

    def configure(self) -> int:
        self.rsi_period = self.integer("rsi_period", 14)
        self.rsi_oversold = self.number("rsi_oversold", 30, maximum=100)
        self.rsi_overbought = self.number("rsi_overbought", 70, maximum=100)
        self.trail_percent = self.number("trail_percent", 0.02, maximum=1)
        if self.rsi_oversold >= self.rsi_overbought or self.trail_percent >= 1:
            msg = "RSI thresholds must be ordered and trail_percent must be less than one"
            raise ValueError(msg)
        return self.rsi_period + 1

    def create_indicators(self) -> dict[str, Any]:
        return {"rsi": RelativeStrengthIndex(self.rsi_period), "previous": None}

    def on_bar(self, bar: MarketState, indicators: dict[str, Any]) -> None:
        previous = indicators["previous"]
        indicators["previous"] = (bar.open, bar.close)
        rsi = indicators["rsi"].update(bar.close)
        if not self.can_decide(bar.symbol) or previous is None or rsi is None:
            return
        if self.state_for(bar.symbol).position:
            self.trail(bar, distance=bar.close * self.trail_percent, intrabar=True)
            return
        prior_open, prior_close = previous
        bullish = (
            prior_close < prior_open
            and bar.close > bar.open
            and bar.open < prior_close
            and bar.close > prior_open
            and rsi < self.rsi_oversold
        )
        bearish = (
            prior_close > prior_open
            and bar.close < bar.open
            and bar.open > prior_close
            and bar.close < prior_open
            and rsi > self.rsi_overbought
        )
        direction = 1 if bullish else -1 if bearish else 0
        if direction:
            self.enter(bar, direction, stop_loss=bar.close * (1 - direction * self.trail_percent))
