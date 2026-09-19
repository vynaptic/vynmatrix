"""Current-window z-score reversion under an ADX range gate."""

from typing import Any

from lib_indicators.oscillators import DirectionalMovement
from lib_indicators.streaming_statistics import RollingStatistics
from lib_strategy.signals.bar_strategy import BarSignalStrategy
from lib_strategy.signals.pure_strategy import MarketState


class ZScoreMeanReversionCore(BarSignalStrategy):
    DEFAULT_STRATEGY_ID = "z_score_mean_reversion_v1"

    def configure(self) -> int:
        self.z_period = self.integer("z_period", 7, minimum=2)
        self.z_entry_threshold = self.number("z_entry_threshold", 1.0)
        self.adx_period = self.integer("adx_period", 14)
        self.adx_filter_level = self.number("adx_filter_level", 20, maximum=100)
        self.trail_percent = self.number("trail_percent", 0.02, maximum=1)
        if self.trail_percent >= 1:
            msg = "trail_percent must be less than one"
            raise ValueError(msg)
        return max(self.z_period, 2 * self.adx_period)

    def create_indicators(self) -> dict[str, Any]:
        return {
            "prices": RollingStatistics(self.z_period),
            "adx": DirectionalMovement(self.adx_period),
        }

    def on_bar(self, bar: MarketState, indicators: dict[str, Any]) -> None:
        stats = indicators["prices"].update(bar.close)
        adx = indicators["adx"].update(bar.high, bar.low, bar.close)
        if not self.can_decide(bar.symbol) or stats is None or adx is None:
            return
        mean, std = stats
        z_score = (bar.close - mean) / std if std > 0 else 0.0
        model = self.state_for(bar.symbol)
        if model.position:
            if self.trail(bar, distance=bar.close * self.trail_percent, intrabar=True):
                return
            if model.position * z_score >= 0:
                self.close(bar, "mean_reversion")
            return
        if std == 0 or adx >= self.adx_filter_level:
            return
        direction = (
            1
            if z_score < -self.z_entry_threshold
            else -1
            if z_score > self.z_entry_threshold
            else 0
        )
        if direction:
            self.enter(bar, direction, stop_loss=bar.close * (1 - direction * self.trail_percent))
