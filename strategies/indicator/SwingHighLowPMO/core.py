"""SwingHighLowPMO pure core — framework-agnostic strategy logic.

Emits canonical Signal objects through a pluggable emitter with LONG and
CLOSE only (no raw "flat" strings). The DB-fed SignalWorker is its only
production runtime.

Indicators:
  - PMO (PriceMomentumOscillator) from lib_indicators
  - EMA (ExponentialMovingAverage) from lib_indicators — SMA-seeded like LEAN
  - SwingHighLow from lib_indicators
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Never
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lib_indicators import ExponentialMovingAverage, PriceMomentumOscillator, SwingHighLow
from lib_strategy.signals.pure_strategy import (
    MarketState,
    ModelStateContractError,
    PureSignalStrategy,
    StrategyState,
)
from lib_strategy.signals.signal import SignalAction
from lib_strategy.signals.utils import compute_external_signal_id, extract_price_provenance

_HOURS_PER_DAY = 24


class SwingHighLowPMOCore(PureSignalStrategy):
    """Pure-core SwingHighLowPMO strategy.

    Entry Logic:
      - LONG: PMO crosses above signal line, price > EMA, price > swing high

    Exit Logic:
      - Stop loss: swing low
      - Take profit: R:R ratio × risk distance
      - Outside trading hours
    """  # noqa: RUF002

    MODEL_STATE_SCHEMA_VERSION = 1

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            strategy_id=kwargs.pop("strategy_id", "swing_high_low_pmo_v1"),
            strategy_type="indicator",
            config=kwargs.pop("config", {}),
            emitter=kwargs.pop("emitter", None),
        )
        # Will be set up in initialize()
        self._indicators: dict[str, dict[str, Any]] = {}

    def initialize(self) -> None:
        cfg = self.config

        # Indicator parameters
        self._pmo_first = int(cfg.get("pmo_first_length", 35))
        self._pmo_second = int(cfg.get("pmo_second_length", 20))
        self._pmo_signal = int(cfg.get("pmo_signal_length", 10))
        self._ema_period = int(cfg.get("ema_period", 50))
        self._swing_length = int(cfg.get("swing_length", 5))

        # Risk management
        self._risk_reward_ratio = float(cfg.get("risk_reward_ratio", 1.5))
        self._max_swing_age_bars = int(cfg.get("max_swing_age_bars", self._swing_length * 4))
        self._max_stop_distance_pct = float(cfg.get("max_stop_distance_pct", 0.10))

        # Trading window
        self._trading_start_hour = int(cfg.get("trading_start_hour", 7))
        self._trading_end_hour = int(cfg.get("trading_end_hour", 23))
        timezone_name = str(cfg.get("trading_timezone", "UTC")).strip()
        try:
            self._trading_timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            msg = f"Unknown trading_timezone: {timezone_name!r}"
            raise ValueError(msg) from exc

        # Strategy version for idempotency key
        self._strategy_version = cfg.get("strategy_version")

        periods = (
            self._pmo_first,
            self._pmo_second,
            self._pmo_signal,
            self._ema_period,
            self._swing_length,
        )
        if min(periods) < 1:
            msg = "PMO, EMA, and swing periods must all be positive"
            raise ValueError(msg)
        if not math.isfinite(self._risk_reward_ratio) or self._risk_reward_ratio <= 0:
            msg = "risk_reward_ratio must be finite and positive"
            raise ValueError(msg)
        if self._max_swing_age_bars < self._swing_length + 1:
            msg = "max_swing_age_bars must allow at least one confirmed pivot"
            raise ValueError(msg)
        if (
            not math.isfinite(self._max_stop_distance_pct)
            or not 0 < self._max_stop_distance_pct < 1
        ):
            msg = "max_stop_distance_pct must be finite and between zero and one"
            raise ValueError(msg)
        if not (0 <= self._trading_start_hour < self._trading_end_hour <= _HOURS_PER_DAY):
            msg = "trading hours must satisfy 0 <= start < end <= 24"
            raise ValueError(msg)
        self._indicators = {}
        self._symbol_states.clear()

        # Warmup: need enough bars for PMO + EMA + SwingHighLow to be ready
        self.warmup_bars_needed = max(
            self._pmo_first + self._pmo_second + self._pmo_signal,
            self._ema_period,
            self._swing_length * 2 + 1,
        )

    def _ensure_indicators(self, symbol: str) -> dict[str, Any]:
        """Lazily create indicators for a symbol."""
        if symbol not in self._indicators:
            self._indicators[symbol] = {
                "pmo": PriceMomentumOscillator(self._pmo_first, self._pmo_second, self._pmo_signal),
                "ema": ExponentialMovingAverage(self._ema_period),
                "swing": SwingHighLow(self._swing_length),
                "last_pmo": None,
                "last_signal": None,
                "last_timestamp": None,
            }
        return self._indicators[symbol]

    def on_data(self, state: MarketState) -> None:
        """Process a consolidated bar."""
        prices = (state.open, state.high, state.low, state.close)
        if (
            any(not math.isfinite(price) or price <= 0 for price in prices)
            or state.low > min(state.open, state.close)
            or state.high < max(state.open, state.close)
        ):
            return
        symbol = state.symbol
        ind = self._ensure_indicators(symbol)
        if ind["last_timestamp"] is not None and state.timestamp <= ind["last_timestamp"]:
            return
        ind["last_timestamp"] = state.timestamp
        sym_state = self.state_for(symbol)
        self._assert_long_only_position(symbol, sym_state)
        bar_meta = dict(state.metadata or {})

        # Update indicators
        pmo_val, signal_val = ind["pmo"].update(state.close)
        ind["ema"].update(state.close)
        swing_high, swing_low = ind["swing"].update(  # noqa: RUF059
            high=state.high, low=state.low, timestamp=state.timestamp
        )

        # Check readiness
        if not all(
            (
                ind["pmo"].is_ready,
                ind["ema"].is_ready,
                ind["swing"].is_ready,
            )
        ):
            return

        ema_value = ind["ema"].value

        # Crossover detection
        last_pmo = ind["last_pmo"]
        last_sig = ind["last_signal"]
        pmo_crossed_above = False
        if last_pmo is not None and last_sig is not None:
            pmo_crossed_above = last_pmo <= last_sig and pmo_val > signal_val

        ind["last_pmo"] = pmo_val
        ind["last_signal"] = signal_val

        if self._bootstrapping or not self.warmup_complete(symbol):
            return

        # Trading window
        if not self._is_trading_time(state.timestamp):
            # Force exit if outside hours
            if sym_state.position != 0:
                self._do_exit(
                    symbol,
                    state.close,
                    state.timestamp,
                    "Outside Trading Hours",
                    bar_meta=bar_meta,
                )
            return

        # Position management: check SL/TP for open positions
        if sym_state.position != 0:
            should_exit, reason = self._check_exit(sym_state, state.close)
            if should_exit:
                self._do_exit(symbol, state.close, state.timestamp, reason, bar_meta=bar_meta)
                return

        # Entry signals (only if flat)
        if sym_state.position == 0:
            latest_high = ind["swing"].get_latest_swing_high()
            latest_low = ind["swing"].get_latest_swing_low()
            high_age = ind["swing"].get_swing_high_age()
            low_age = ind["swing"].get_swing_low_age()

            if (
                pmo_crossed_above
                and state.close > ema_value
                and latest_high
                and high_age is not None
                and high_age <= self._max_swing_age_bars
                and state.close > latest_high
                and latest_low
                and low_age is not None
                and low_age <= self._max_swing_age_bars
                # Stop-loss is the latest swing low; only enter when it sits
                # below entry so risk = entry - stop_loss > 0. Swing high/low are
                # independent confirmed pivots, so in an up-trend the latest swing
                # low can print ABOVE entry -> an inverted bracket and immediate
                # stop-out. Guard against that here (see also the emit-layer
                # bracket invariant in PureSignalStrategy.emit_signal).
                and latest_low < state.close
                and (state.close - latest_low) / state.close <= self._max_stop_distance_pct
            ):
                self._do_long_entry(
                    symbol,
                    state.close,
                    latest_low,
                    state.timestamp,
                    bar_meta=bar_meta,
                )

    def _is_trading_time(self, ts: datetime) -> bool:
        normalized = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts
        local = normalized.astimezone(self._trading_timezone)
        return self._trading_start_hour <= local.hour < self._trading_end_hour

    def _assert_long_only_position(self, symbol: str, sym_state: StrategyState) -> None:
        if sym_state.position not in {0, 1}:
            msg = (
                f"SwingHighLowPMO is long-only; invalid position={sym_state.position} for {symbol}"
            )
            raise RuntimeError(msg)

    def _validate_restored_model_state(
        self,
        symbol_states: Mapping[str, StrategyState],
    ) -> None:
        for symbol, sym_state in symbol_states.items():
            self._assert_long_only_position(symbol, sym_state)
            if sym_state.position == 0:
                if {"stop_loss", "take_profit"} & sym_state.custom.keys():
                    msg = f"Flat Swing model state for {symbol!r} retains protective prices"
                    raise ModelStateContractError(msg)
                continue
            if sym_state.entry_price is None or sym_state.entry_time is None:
                msg = f"Swing model state for open symbol {symbol!r} has no entry attribution"
                raise ModelStateContractError(msg)
            stop_loss = sym_state.custom.get("stop_loss")
            take_profit = sym_state.custom.get("take_profit")
            if (
                isinstance(stop_loss, bool)
                or not isinstance(stop_loss, (int, float))
                or isinstance(take_profit, bool)
                or not isinstance(take_profit, (int, float))
                or not math.isfinite(float(stop_loss))
                or not math.isfinite(float(take_profit))
                or not float(stop_loss) < sym_state.entry_price < float(take_profit)
            ):
                msg = f"Swing model state for {symbol!r} has an invalid protective bracket"
                raise ModelStateContractError(msg)

    def _check_exit(self, sym_state: StrategyState, current_price: float) -> tuple[bool, str]:
        if sym_state.position not in {0, 1}:
            msg = f"SwingHighLowPMO is long-only; invalid position={sym_state.position}"
            raise RuntimeError(msg)

        sl = sym_state.custom.get("stop_loss")
        tp = sym_state.custom.get("take_profit")
        if sl is None or tp is None:
            return False, ""

        if sym_state.position == 1:
            if current_price <= sl:
                return True, "Stop Loss"
            if current_price >= tp:
                return True, "Take Profit"
        return False, ""

    def _do_long_entry(
        self,
        symbol: str,
        entry_price: float,
        stop_loss: float,
        bar_ts: datetime,
        *,
        bar_meta: dict[str, Any] | None = None,
    ) -> None:
        risk = entry_price - stop_loss
        tp = entry_price + risk * self._risk_reward_ratio
        sym_state = self.state_for(symbol)
        ext_id = compute_external_signal_id(
            strategy_id=self.strategy_id,
            symbol=symbol,
            action=SignalAction.LONG,
            bar_close_ts=bar_ts,
            strategy_version=self._strategy_version,
            reason="pmo_cross_above",
        )

        self.emit_long(
            symbol=symbol,
            entry_price=entry_price,
            confidence=0.75,
            timestamp=bar_ts,
            horizon="intraday",
            stop_loss=stop_loss,
            take_profit=tp,
            external_signal_id=ext_id,
            metadata={
                "risk_distance": risk,
                "risk_reward": self._risk_reward_ratio,
                "external_signal_id": ext_id,
                **extract_price_provenance(bar_meta),
            },
        )
        # The runtime journals this shared model transition with the signal
        # envelope; it does not represent a per-user broker fill.
        sym_state.position = 1
        sym_state.entry_price = entry_price
        sym_state.entry_time = bar_ts
        sym_state.custom["stop_loss"] = stop_loss
        sym_state.custom["take_profit"] = tp

    def emit_short(
        self,
        symbol: str,
        entry_price: float | None = None,
        confidence: float = 1.0,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        **kwargs: Any,
    ) -> Never:
        """Reject inherited SHORT emission for this long-only strategy."""
        del symbol, entry_price, confidence, stop_loss, take_profit, kwargs
        msg = "SwingHighLowPMO is long-only and cannot emit SHORT signals"
        raise RuntimeError(msg)

    def _do_exit(
        self,
        symbol: str,
        exit_price: float,
        bar_ts: datetime,
        reason: str,
        *,
        bar_meta: dict[str, Any] | None = None,
    ) -> None:
        sym_state = self.state_for(symbol)
        if sym_state.position == 0:
            return
        self._assert_long_only_position(symbol, sym_state)

        entry = sym_state.entry_price or exit_price
        pnl_pct = (exit_price - entry) / entry * 100

        ext_id = compute_external_signal_id(
            strategy_id=self.strategy_id,
            symbol=symbol,
            action=SignalAction.CLOSE,
            bar_close_ts=bar_ts,
            strategy_version=self._strategy_version,
            reason=reason,
        )

        self.emit_close(
            symbol=symbol,
            exit_price=exit_price,
            timestamp=bar_ts,
            reason=reason,
            horizon="intraday",
            external_signal_id=ext_id,
            metadata={
                "pnl_pct": pnl_pct,
                "external_signal_id": ext_id,
                **extract_price_provenance(bar_meta),
            },
        )

        # Reset position state
        sym_state.position = 0
        sym_state.entry_price = None
        sym_state.entry_time = None
        sym_state.custom.pop("stop_loss", None)
        sym_state.custom.pop("take_profit", None)
