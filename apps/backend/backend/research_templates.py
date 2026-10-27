"""Reviewed editor starting points; templates grant no execution authority."""

from __future__ import annotations

from typing import Any

_MOVING_AVERAGE = """from collections import deque
from lib_strategy.signals.pure_strategy import PureSignalStrategy

class UserStrategy(PureSignalStrategy):
    def initialize(self):
        self.closes = deque(maxlen=20)
        self._warmup_bars_needed = 20

    def on_data(self, state):
        self.closes.append(state.close)
        if len(self.closes) < 20:
            return
        prices = list(self.closes)
        fast = sum(prices[-5:]) / 5
        slow = sum(prices) / 20
        model_state = self.state_for(state.symbol)
        if fast > slow and model_state.position == 0:
            self.emit_long(state.symbol, state.close, confidence=0.5,
                           timestamp=state.timestamp)
            model_state.position = 1
        elif fast <= slow and model_state.position > 0:
            self.emit_close(state.symbol, state.close, timestamp=state.timestamp)
            model_state.position = 0
"""

_RETURN_MODEL = """from collections import deque
from datetime import datetime
from lib_strategy.signals.pure_strategy import PureSignalStrategy
from lib_strategy.research import predict_return_model

class UserStrategy(PureSignalStrategy):
    def initialize(self):
        self.bars = deque(maxlen=21)
        self._warmup_bars_needed = 20

    def on_data(self, state):
        self.bars.append({"ts": state.timestamp.isoformat(),
            "open": str(state.open), "high": str(state.high),
            "low": str(state.low), "close": str(state.close),
            "volume": str(state.volume)})
        model = self.config.get("research_model")
        if model is None:
            raise ValueError("Select a registered model for this strategy backtest")
        if len(self.bars) < 21:
            return
        cutoff = datetime.fromisoformat(model["training_cutoff"].replace("Z", "+00:00"))
        if state.timestamp < cutoff:
            return
        result = predict_return_model(model, list(self.bars),
            evaluation_start=state.timestamp.isoformat())
        if not result["predictions"]:
            return
        forecast = result["predictions"][-1]["predicted_log_return"]
        threshold = float(self.config.get("return_threshold", 0))
        model_state = self.state_for(state.symbol)
        if forecast > threshold and model_state.position == 0:
            self.emit_long(state.symbol, state.close, confidence=0.5,
                           timestamp=state.timestamp)
            model_state.position = 1
        elif forecast <= threshold and model_state.position > 0:
            self.emit_close(state.symbol, state.close, timestamp=state.timestamp)
            model_state.position = 0
"""


def strategy_templates() -> list[dict[str, Any]]:
    return [
        {
            "template_id": "moving_average",
            "name": "Moving-average research strategy",
            "source": _MOVING_AVERAGE,
            "requires_model": False,
            "config": {
                "class_name": "UserStrategy",
                "strategy_id": "research_ma_v1",
                "parameters": {},
            },
        },
        {
            "template_id": "return_model",
            "name": "Registered return-model strategy",
            "source": _RETURN_MODEL,
            "requires_model": True,
            "config": {
                "class_name": "UserStrategy",
                "strategy_id": "research_return_v1",
                "parameters": {"return_threshold": 0},
            },
        },
    ]
