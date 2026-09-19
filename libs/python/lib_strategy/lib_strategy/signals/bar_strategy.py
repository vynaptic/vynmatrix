"""Shared bar validation and virtual-position lifecycle for native signal cores.

Complete streaming indicators and model protection are checkpointed together
through PureSignalStrategy's existing JSON payload contract.
No broker position, order, sizing policy, or execution permission is inferred.
"""

from __future__ import annotations

import hashlib
import json
import math
from abc import abstractmethod
from collections.abc import Mapping
from datetime import datetime
from typing import Any, ClassVar, cast

from lib_common.logging import get_logger

from .pure_strategy import MarketState, ModelStateContractError, PureSignalStrategy, StrategyState
from .stream_checkpoint import capture_stream, restore_stream
from .utils import ensure_utc, extract_price_provenance, parse_signal_horizon_days

logger = get_logger(__name__)


class BarSignalStrategy(PureSignalStrategy):
    """A signal-only lifecycle with independent, validated streams per symbol."""

    DEFAULT_STRATEGY_ID: str
    REQUIRES_PROTECTION = True
    REQUIRED_POSITIVE_MODEL_FIELDS: ClassVar[tuple[str, ...]] = ()
    REQUIRED_BOOLEAN_MODEL_FIELDS: ClassVar[tuple[str, ...]] = ()
    STREAM_FIELDS: ClassVar[dict[str, str]] = {}

    def __init__(self, **kwargs: Any) -> None:
        strategy_type = kwargs.pop("strategy_type", "indicator")
        if strategy_type != "indicator":
            msg = "BarSignalStrategy requires strategy_type=indicator"
            raise ValueError(msg)
        super().__init__(
            strategy_id=kwargs.pop("strategy_id", self.DEFAULT_STRATEGY_ID),
            strategy_type="indicator",
            config=kwargs.pop("config", {}),
            emitter=kwargs.pop("emitter", None),
        )
        if kwargs:
            msg = f"Unknown strategy constructor arguments: {sorted(kwargs)}"
            raise TypeError(msg)
        self._indicators: dict[str, dict[str, Any]] = {}
        self._last_timestamp: dict[str, datetime] = {}
        self._accepted_bars: dict[str, int] = {}
        self._direction_mode = "long_short"

    def initialize(self) -> None:
        self._direction_mode = str(self.config.get("trade_direction_mode", "long_short"))
        if self._direction_mode not in {"long_only", "long_short"}:
            msg = "trade_direction_mode must be long_only or long_short"
            raise ValueError(msg)
        horizon = self.config.get("evaluation_horizon", "1d")
        if not isinstance(horizon, str) or not horizon or horizon != horizon.strip():
            msg = "evaluation_horizon must be a supported non-blank duration"
            raise ValueError(msg)
        days = parse_signal_horizon_days(horizon)
        if days is None:
            msg = "evaluation_horizon must be a finite positive supported duration"
            raise ValueError(msg)
        self._evaluation_horizon = horizon
        self._horizon_days = days
        self.warmup_bars_needed = self.configure()
        self._indicators.clear()
        self._last_timestamp.clear()
        self._accepted_bars.clear()
        self._symbol_states.clear()
        self._warmup_bars_received.clear()

    @abstractmethod
    def configure(self) -> int:
        """Validate rule parameters and return the complete indicator-chain warm-up."""

    @abstractmethod
    def create_indicators(self) -> dict[str, Any]:
        """Create one independent set of streaming calculations."""

    @abstractmethod
    def on_bar(self, bar: MarketState, indicators: dict[str, Any]) -> None:
        """Update indicators on every accepted bar, then evaluate the rule."""

    def integer(self, name: str, default: int, *, minimum: int = 1) -> int:
        raw = self.config.get(name, default)
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            msg = f"{name} must be an integer >= {minimum}"
            raise TypeError(msg)
        try:
            value = int(raw)
        except ValueError as exc:
            msg = f"{name} must be an integer >= {minimum}"
            raise ValueError(msg) from exc
        if value < minimum:
            msg = f"{name} must be >= {minimum}"
            raise ValueError(msg)
        return value

    def number(
        self,
        name: str,
        default: float,
        *,
        minimum: float = 0.0,
        maximum: float | None = None,
        inclusive: bool = False,
    ) -> float:
        raw = self.config.get(name, default)
        if isinstance(raw, bool):
            msg = f"{name} must be numeric, not boolean"
            raise TypeError(msg)
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            msg = f"{name} must be a finite number"
            raise ValueError(msg) from exc
        below = value < minimum if inclusive else value <= minimum
        if not math.isfinite(value) or below or (maximum is not None and value > maximum):
            msg = f"{name} is outside its finite allowed range"
            raise ValueError(msg)
        return value

    def boolean(self, name: str, default: bool) -> bool:
        value = self.config.get(name, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        msg = f"{name} must be true or false"
        raise ValueError(msg)

    def on_data(self, state: MarketState) -> None:
        prices = (state.open, state.high, state.low, state.close)
        if (
            not state.symbol
            or state.symbol.strip() != state.symbol
            or any(not math.isfinite(value) or value <= 0 for value in prices)
            or state.low > min(state.open, state.close)
            or state.high < max(state.open, state.close)
            or not math.isfinite(state.volume)
            or state.volume < 0
        ):
            logger.warning(
                "Ignoring invalid strategy bar", strategy_id=self.strategy_id, symbol=state.symbol
            )
            return
        timestamp = ensure_utc(state.timestamp)
        previous = self._last_timestamp.get(state.symbol)
        if previous is not None and timestamp <= previous:
            return
        self._last_timestamp[state.symbol] = timestamp
        self._accepted_bars[state.symbol] = self._accepted_bars.get(state.symbol, 0) + 1
        if state.symbol not in self._indicators:
            self._indicators[state.symbol] = self.create_indicators()
        model = self.state_for(state.symbol)
        if model.position and not self._bootstrapping:
            model.bars_in_trade += 1
        self.on_bar(state, self._indicators[state.symbol])

    def warmup_complete(self, symbol: str | None = None) -> bool:
        """Count accepted bars only; redelivery and rejected bars cannot warm a core."""
        if symbol is not None:
            return self._accepted_bars.get(symbol, 0) >= self.warmup_bars_needed
        return bool(self._accepted_bars) and all(
            count >= self.warmup_bars_needed for count in self._accepted_bars.values()
        )

    def can_decide(self, symbol: str) -> bool:
        return not self._bootstrapping and self.warmup_complete(symbol)

    def enter(
        self,
        bar: MarketState,
        direction: int,
        *,
        stop_loss: float | None = None,
        custom: dict[str, Any] | None = None,
    ) -> bool:
        """Record virtual intent only after a valid, eligible signal transition."""
        if not self.can_decide(bar.symbol) or self.state_for(bar.symbol).position:
            return False
        if direction not in {-1, 1}:
            msg = "Entry direction must be -1 or 1"
            raise ValueError(msg)
        if direction == -1 and self._direction_mode == "long_only":
            return False
        if self.REQUIRES_PROTECTION and stop_loss is None:
            return False
        if stop_loss is not None and (
            not math.isfinite(stop_loss)
            or stop_loss <= 0
            or direction * (bar.close - stop_loss) <= 0
        ):
            return False
        emit = self.emit_long if direction == 1 else self.emit_short
        emit(
            symbol=bar.symbol,
            entry_price=bar.close,
            stop_loss=stop_loss,
            timestamp=bar.timestamp,
            horizon=self._evaluation_horizon,
            horizon_days=self._horizon_days,
            metadata=extract_price_provenance(bar.metadata),
        )
        model = self.state_for(bar.symbol)
        model.position = direction
        model.entry_price = bar.close
        model.entry_time = ensure_utc(bar.timestamp)
        model.bars_in_trade = 0
        model.custom = dict(custom or {})
        if stop_loss is not None:
            model.custom["stop_loss"] = stop_loss
        return True

    def close(self, bar: MarketState, reason: str) -> None:
        if not self.can_decide(bar.symbol) or not self.state_for(bar.symbol).position:
            return
        self.emit_close(
            symbol=bar.symbol,
            exit_price=bar.close,
            reason=reason,
            timestamp=bar.timestamp,
            horizon=self._evaluation_horizon,
            horizon_days=self._horizon_days,
            metadata=extract_price_provenance(bar.metadata),
        )
        self._symbol_states[bar.symbol] = StrategyState()

    def trail(
        self,
        bar: MarketState,
        *,
        distance: float,
        intrabar: bool = False,
        use_extreme: bool = False,
        strict: bool = False,
    ) -> bool:
        """Ratchet virtual protection and emit a close when its rule is breached.

        Intrabar checks use the previously established stop before a new bar-close
        level exists. Close-only rules can use the current high/low and close.
        A CLOSE remains intent at bar close, never a fabricated stop-price fill.
        """
        model = self.state_for(bar.symbol)
        if not model.position:
            return False
        old_stop = float(model.custom["stop_loss"])
        if intrabar:
            touched = bar.low <= old_stop if model.position == 1 else bar.high >= old_stop
            if touched:
                self.close(bar, "trailing_stop")
                return True
        if not math.isfinite(distance) or distance <= 0:
            return False
        anchor = bar.close
        if use_extreme:
            observed = bar.high if model.position == 1 else bar.low
            if model.entry_price is None:
                msg = "Open model has no entry price for its trailing anchor"
                raise ModelStateContractError(msg)
            previous = float(model.custom.get("extreme", model.entry_price))
            anchor = max(previous, observed) if model.position == 1 else min(previous, observed)
            model.custom["extreme"] = anchor
        candidate = anchor - model.position * distance
        stop = max(old_stop, candidate) if model.position == 1 else min(old_stop, candidate)
        model.custom["stop_loss"] = stop
        if not intrabar:
            delta = model.position * (bar.close - stop)
            if delta < 0 or (not strict and delta == 0):
                self.close(bar, "trailing_stop")
                return True
        return False

    def _stream_config_digest(self) -> str:
        payload = json.dumps(self.config, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(payload.encode()).hexdigest()

    def serialize_model_state(self) -> dict[str, Any]:
        snapshot = super().serialize_model_state()
        snapshot["streams"] = {
            "version": 1,
            "config_sha256": self._stream_config_digest(),
            "symbols": {
                symbol: {
                    "accepted_bars": self._accepted_bars[symbol],
                    "last_timestamp": self._last_timestamp[symbol].isoformat(),
                    "indicators": capture_stream(stream, self.STREAM_FIELDS),
                }
                for symbol, stream in sorted(self._indicators.items())
            },
        }
        return cast(dict[str, Any], json.loads(json.dumps(snapshot, allow_nan=False)))

    def restore_model_state(self, snapshot: Mapping[str, Any]) -> None:
        """Validate fresh streams and model state before replacing live state."""
        try:
            indicators, accepted, timestamps = self._decode_stream_checkpoint(snapshot)
        except (KeyError, TypeError, ValueError) as exc:
            msg = f"Invalid complete stream checkpoint: {exc}"
            raise ModelStateContractError(msg) from exc
        super().restore_model_state(snapshot)
        self._indicators = indicators
        self._accepted_bars = accepted
        self._last_timestamp = timestamps
        self._warmup_bars_received = dict(accepted)

    def _decode_stream_checkpoint(
        self, snapshot: Mapping[str, Any]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, int], dict[str, datetime]]:
        streams = snapshot["streams"]
        if (
            not isinstance(streams, Mapping)
            or set(streams) != {"version", "config_sha256", "symbols"}
            or type(streams["version"]) is not int
            or streams["version"] != 1
            or streams["config_sha256"] != self._stream_config_digest()
            or not isinstance(streams["symbols"], Mapping)
        ):
            msg = "Incompatible stream checkpoint identity or configuration"
            raise ValueError(msg)
        indicators, accepted, timestamps = {}, {}, {}
        states = snapshot["symbol_states"]
        if not isinstance(states, Mapping):
            msg = "Model symbol states must be an object"
            raise TypeError(msg)
        for symbol, payload in streams["symbols"].items():
            if (
                not isinstance(symbol, str)
                or not symbol
                or symbol != symbol.strip().upper()
                or symbol not in states
                or not isinstance(payload, Mapping)
                or set(payload) != {"accepted_bars", "last_timestamp", "indicators"}
                or type(payload["accepted_bars"]) is not int
                or payload["accepted_bars"] < 1
            ):
                msg = "Malformed stream symbol checkpoint"
                raise ValueError(msg)
            timestamp = datetime.fromisoformat(payload["last_timestamp"])
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                msg = "Checkpoint timestamp must have an explicit timezone"
                raise ValueError(msg)
            indicators[symbol] = restore_stream(
                payload["indicators"], self.create_indicators(), self.STREAM_FIELDS
            )
            accepted[symbol] = payload["accepted_bars"]
            timestamps[symbol] = ensure_utc(timestamp)
        for symbol, model in states.items():
            if model["position"]:
                if symbol not in indicators:
                    msg = "Open model has no indicator checkpoint"
                    raise ValueError(msg)
                entry_time = datetime.fromisoformat(model["entry_time"])
                if (
                    entry_time.tzinfo is None
                    or ensure_utc(entry_time) > timestamps[symbol]
                    or model["bars_in_trade"] >= accepted[symbol]
                    or accepted[symbol] < self.warmup_bars_needed
                ):
                    msg = "Open model attribution disagrees with its stream checkpoint"
                    raise ValueError(msg)
        return indicators, accepted, timestamps

    @property
    def supports_flat_model_boundary(self) -> bool:
        return True

    def reset_model_to_flat_for_evaluation(self, symbols: tuple[str, ...]) -> None:
        for symbol in symbols:
            self._symbol_states[symbol] = StrategyState()

    def _validate_restored_model_state(self, symbol_states: Mapping[str, StrategyState]) -> None:
        for symbol, model in symbol_states.items():
            if model.position == -1 and self._direction_mode == "long_only":
                msg = f"Forbidden short model position for {symbol!r}"
                raise ModelStateContractError(msg)
            if model.position == 0:
                if model.entry_price is not None or model.entry_time is not None or model.custom:
                    msg = f"Flat model for {symbol!r} retains entry or protective state"
                    raise ModelStateContractError(msg)
                continue
            if model.entry_price is None or model.entry_time is None:
                msg = f"Open model for {symbol!r} lacks entry attribution"
                raise ModelStateContractError(msg)
            stop = model.custom.get("stop_loss")
            if (stop is None and self.REQUIRES_PROTECTION) or (
                stop is not None
                and (
                    isinstance(stop, bool)
                    or not isinstance(stop, (float, int))
                    or not math.isfinite(stop)
                    or stop <= 0
                )
            ):
                msg = f"Open model for {symbol!r} has no valid stop"
                raise ModelStateContractError(msg)
            for name in self.REQUIRED_BOOLEAN_MODEL_FIELDS:
                if not isinstance(model.custom.get(name), bool):
                    msg = f"Invalid or missing {name} in model for {symbol!r}"
                    raise ModelStateContractError(msg)
            for name in ("extreme", "trail_distance"):
                value = model.custom.get(name)
                if value is None and name in self.REQUIRED_POSITIVE_MODEL_FIELDS:
                    msg = f"Missing {name} in model for {symbol!r}"
                    raise ModelStateContractError(msg)
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, (float, int))
                    or not math.isfinite(value)
                    or value <= 0
                ):
                    msg = f"Invalid {name} in model for {symbol!r}"
                    raise ModelStateContractError(msg)
