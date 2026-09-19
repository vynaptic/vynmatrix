"""Previous-bar bandwidth squeeze followed by a current-band breakout."""

from typing import Any, ClassVar

from lib_indicators import SimpleMovingAverage
from lib_indicators.streaming_statistics import RollingStatistics
from lib_strategy.signals.bar_strategy import BarSignalStrategy
from lib_strategy.signals.pure_strategy import MarketState


class BBSqueezeBreakoutCore(BarSignalStrategy):
    DEFAULT_STRATEGY_ID = "bb_squeeze_breakout_v1"
    REQUIRES_PROTECTION = False

    STREAM_FIELDS: ClassVar[dict[str, str]] = {"obv": "number", "previous": "number?"}

    def configure(self) -> int:
        self.bb_period = self.integer("bb_period", 20, minimum=2)
        self.bb_devfactor = self.number("bb_devfactor", 2)
        self.squeeze_lookback = self.integer("squeeze_lookback", 40, minimum=2)
        self.squeeze_threshold_pct = self.number("squeeze_threshold_pct", 10, maximum=100)
        self.obv_lookback = self.integer("obv_lookback", 10)
        self.use_volume_confirm = self.boolean("use_volume_confirm", True)
        if self.use_volume_confirm and self.config.get("volume_semantics") != "traded":
            msg = "OBV confirmation requires explicit volume_semantics=traded"
            raise ValueError(msg)
        return max(
            self.bb_period + self.squeeze_lookback,
            self.obv_lookback if self.use_volume_confirm else 0,
        )

    def create_indicators(self) -> dict[str, Any]:
        return {
            "prices": RollingStatistics(self.bb_period),
            "bandwidth": RollingStatistics(self.squeeze_lookback),
            "obv_sma": SimpleMovingAverage(self.obv_lookback),
            "obv": 0.0,
            "previous": None,
        }

    def on_bar(self, bar: MarketState, indicators: dict[str, Any]) -> None:
        prior_rank = indicators["bandwidth"].percent_rank
        stats = indicators["prices"].update(bar.close)
        previous = indicators["previous"]
        if previous is None or bar.close > previous:
            indicators["obv"] += bar.volume
        elif bar.close < previous:
            indicators["obv"] -= bar.volume
        indicators["previous"] = bar.close
        obv_sma = indicators["obv_sma"].update(indicators["obv"])
        if stats is None:
            return
        mean, std = stats
        width = self.bb_devfactor * std
        indicators["bandwidth"].update(2 * width / mean)
        if not self.can_decide(bar.symbol):
            return
        model = self.state_for(bar.symbol)
        if model.position:
            if model.position * (bar.close - mean) <= 0:
                self.close(bar, "middle_band")
            return
        if prior_rank is None or prior_rank >= self.squeeze_threshold_pct / 100:
            return
        direction = 1 if bar.close > mean + width else -1 if bar.close < mean - width else 0
        if self.use_volume_confirm and (
            obv_sma is None or bar.volume <= 0 or direction * (indicators["obv"] - obv_sma) <= 0
        ):
            return
        if direction:
            self.enter(bar, direction)
