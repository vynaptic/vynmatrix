"""
PureSignalStrategy - Framework-agnostic base class for signal-only strategies.

All indicator strategies should implement this interface.
The strategy ONLY emits signals; it never executes trades directly.

This enables:
- Same strategy code works in backtest, paper, and live modes
- Clean separation between signal generation and execution
- One framework-independent core shared by historical replay and SignalWorker
"""

from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from lib_common.logging import get_logger
from lib_common.time_utils import now_utc
from lib_strategy.signals.emitter import LogSignalEmitter, SignalEmitter
from lib_strategy.signals.signal import Signal, SignalAction
from lib_strategy.signals.utils import compute_external_signal_id, ensure_utc

logger = get_logger(__name__)


class ModelStateContractError(RuntimeError):
    """Persisted strategy-model state is absent, malformed, or incompatible."""


@dataclass
class MarketState:
    """
    Represents current market state passed to strategies.

    Runtime data loaders translate canonical source bars to this format.
    This provides a unified interface for indicator strategies.

    Attributes:
        symbol: Trading symbol
        timestamp: Current bar/tick timestamp
        open: Open price
        high: High price
        low: Low price
        close: Close price
        volume: Volume
        indicators: Dictionary of indicator values (e.g., {"rsi": 65, "ema_50": 100.5})
        metadata: Additional market context
    """

    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    indicators: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyState:
    """
    Represents shared strategy-model state.

    This records the model's intended/virtual position between bars. It is not
    a per-user broker position and must never be interpreted as proof that a
    binding qualified, an order passed risk checks, or a broker filled it. A
    single signal core can serve many users whose execution outcomes differ.

    The DB-fed runtime reconstructs indicators from authoritative price history
    and restores this versioned shared-model state before it accepts a newer
    source bar. Tenant broker outcomes remain separate account projections.

    Attributes:
        position: Model position (1=long, -1=short, 0=flat)
        entry_price: Entry price of current position
        entry_time: Entry timestamp
        bars_in_trade: Number of bars since entry
        custom: Strategy-specific state
    """

    position: int = 0  # 1=long, -1=short, 0=flat
    entry_price: float | None = None
    entry_time: datetime | None = None
    bars_in_trade: int = 0
    custom: dict[str, Any] = field(default_factory=dict)


class PureSignalStrategy(ABC):
    """
    Abstract base class for pure signal-only strategies.

    Strategies extending this class:
    - Implement on_data() to process market data
    - Call emit_signal() to generate trading signals
    - NEVER execute trades directly

    Multi-symbol support:
        Use ``state_for(symbol)`` to get per-symbol StrategyState.

    Warmup/bootstrap:
        Override ``warmup_bars_needed`` to declare how many bars are needed
        before signals should be emitted. Call ``bootstrap_history(bars)``
        on startup to feed historical data without emitting signals.

    Example:
        >>> class MyStrategy(PureSignalStrategy):
        ...     def initialize(self) -> None:
        ...         self.rsi_period = self.config.get("rsi_period", 14)
        ...
        ...     def on_data(self, state: MarketState) -> None:
        ...         rsi = state.indicators.get("rsi")
        ...         if rsi and rsi < 30:
        ...             self.emit_long(state.symbol, state.close, confidence=0.8)
        ...         elif rsi and rsi > 70:
        ...             self.emit_short(state.symbol, state.close, confidence=0.8)
    """

    MODEL_STATE_SCHEMA_VERSION = 1

    def __init__(
        self,
        strategy_id: str,
        strategy_type: str,
        config: dict[str, Any] | None = None,
        emitter: SignalEmitter | None = None,
    ) -> None:
        """
        Initialize strategy.

        Args:
            strategy_id: Unique strategy identifier
            strategy_type: Strategy type (currently ``indicator``)
            config: Strategy configuration
            emitter: SignalEmitter for output (defaults to LogSignalEmitter)
        """
        self.strategy_id = strategy_id
        self.strategy_type = strategy_type
        self.config = config or {}
        self._emitter = emitter or LogSignalEmitter()
        self._is_initialized = False

        # Per-symbol state for multi-symbol strategies
        self._symbol_states: dict[str, StrategyState] = {}

        # Warmup tracking
        self._warmup_bars_needed: int = 0  # Override via warmup_bars_needed property
        self._warmup_bars_received: dict[str, int] = {}  # per-symbol count
        self._bootstrapping: bool = False  # True during bootstrap_history()
        self._emission_suppression_depth: int = 0

    @abstractmethod
    def initialize(self) -> None:
        """
        Initialize strategy-specific components.

        Called once before first on_data() call.
        Set up indicators, load models, etc.
        """

    @abstractmethod
    def on_data(self, state: MarketState) -> None:
        """
        Process new market data and emit signals.

        Called for each new bar/tick.
        Implement trading logic here.

        Args:
            state: Current market state
        """

    @property
    def warmup_bars_needed(self) -> int:
        """Number of historical bars needed before signals should be emitted.

        Override in subclass to declare warmup requirements. The default
        (0) means no warmup is needed and signals can be emitted immediately.
        """
        return self._warmup_bars_needed

    @warmup_bars_needed.setter
    def warmup_bars_needed(self, value: int) -> None:
        self._warmup_bars_needed = max(0, value)

    def state_for(self, symbol: str) -> StrategyState:
        """Get or create per-symbol StrategyState.

        Every symbol gets independent position and entry tracking.

        Args:
            symbol: Trading symbol (e.g. "BTCUSD")

        Returns:
            StrategyState for the given symbol
        """
        if symbol not in self._symbol_states:
            self._symbol_states[symbol] = StrategyState()
        return self._symbol_states[symbol]

    @property
    def model_state_schema_version(self) -> int:
        """Version of this core's durable model-state payload."""
        version = self.MODEL_STATE_SCHEMA_VERSION
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            msg = f"Invalid model-state schema version for {self.strategy_id!r}: {version!r}"
            raise ModelStateContractError(msg)
        return version

    def serialize_model_state(self) -> dict[str, Any]:
        """Return a JSON-safe, versioned snapshot of shared model state.

        Indicator values are reconstructed from authoritative price history on
        startup. This contract persists the strategy's desired/virtual
        position, entry attribution, and strategy-specific custom state that
        cannot be inferred from a tenant's broker fills.
        """
        if not self._is_initialized:
            msg = f"Cannot serialize uninitialized strategy {self.strategy_id!r}"
            raise ModelStateContractError(msg)

        symbol_states = {
            symbol: self._serialize_symbol_state(state)
            for symbol, state in sorted(self._symbol_states.items())
        }
        snapshot: dict[str, Any] = {
            "schema_version": self.model_state_schema_version,
            "strategy_id": self.strategy_id,
            "strategy_type": self.strategy_type,
            "strategy_version": self._configured_strategy_version(),
            "symbol_states": symbol_states,
        }
        self._validate_restored_model_state(
            {
                symbol: self._deserialize_symbol_state(symbol, payload)
                for symbol, payload in symbol_states.items()
            }
        )
        try:
            json.dumps(snapshot, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            msg = f"Model-state snapshot for {self.strategy_id!r} is not JSON-safe"
            raise ModelStateContractError(msg) from exc
        return snapshot

    def restore_model_state(self, snapshot: Mapping[str, Any]) -> None:
        """Restore a compatible shared-model snapshot after indicator bootstrap."""
        if not self._is_initialized:
            msg = f"Cannot restore uninitialized strategy {self.strategy_id!r}"
            raise ModelStateContractError(msg)
        if not isinstance(snapshot, Mapping):
            msg = "Persisted model-state snapshot must be an object"
            raise ModelStateContractError(msg)

        expected_identity = {
            "schema_version": self.model_state_schema_version,
            "strategy_id": self.strategy_id,
            "strategy_type": self.strategy_type,
            "strategy_version": self._configured_strategy_version(),
        }
        actual_identity = {key: snapshot.get(key) for key in expected_identity}
        if actual_identity != expected_identity:
            msg = (
                f"Incompatible model-state identity for {self.strategy_id!r}: "
                f"expected={expected_identity!r} actual={actual_identity!r}"
            )
            raise ModelStateContractError(msg)

        raw_states = snapshot.get("symbol_states")
        if not isinstance(raw_states, Mapping):
            msg = "Persisted model-state symbol_states must be an object"
            raise ModelStateContractError(msg)
        restored: dict[str, StrategyState] = {}
        for raw_symbol, raw_state in raw_states.items():
            symbol = str(raw_symbol).strip().upper()
            if not symbol or symbol != raw_symbol:
                msg = f"Persisted model-state symbol is not canonical: {raw_symbol!r}"
                raise ModelStateContractError(msg)
            if symbol in restored:
                msg = f"Persisted model-state repeats symbol {symbol!r}"
                raise ModelStateContractError(msg)
            restored[symbol] = self._deserialize_symbol_state(symbol, raw_state)

        self._validate_restored_model_state(restored)
        self._symbol_states = restored

    def _configured_strategy_version(self) -> str | None:
        raw = self.config.get("strategy_version")
        if raw is None:
            return None
        version = str(raw).strip()
        return version or None

    @staticmethod
    def _serialize_symbol_state(state: StrategyState) -> dict[str, Any]:
        entry_time = ensure_utc(state.entry_time).isoformat() if state.entry_time else None
        return {
            "position": state.position,
            "entry_price": state.entry_price,
            "entry_time": entry_time,
            "bars_in_trade": state.bars_in_trade,
            "custom": dict(state.custom),
        }

    @staticmethod
    def _deserialize_symbol_state(symbol: str, payload: Any) -> StrategyState:
        if not isinstance(payload, Mapping):
            msg = f"Persisted model state for {symbol!r} must be an object"
            raise ModelStateContractError(msg)
        position = payload.get("position")
        if (
            isinstance(position, bool)
            or not isinstance(position, int)
            or position not in {-1, 0, 1}
        ):
            msg = f"Persisted model position for {symbol!r} is invalid: {position!r}"
            raise ModelStateContractError(msg)
        entry_price = payload.get("entry_price")
        if entry_price is not None:
            if (
                isinstance(entry_price, bool)
                or not isinstance(entry_price, (int, float))
                or not math.isfinite(float(entry_price))
                or float(entry_price) <= 0
            ):
                msg = f"Persisted entry price for {symbol!r} is invalid"
                raise ModelStateContractError(msg)
            entry_price = float(entry_price)
        entry_time = payload.get("entry_time")
        if entry_time is not None:
            try:
                parsed_entry_time = datetime.fromisoformat(str(entry_time))
            except ValueError as exc:
                msg = f"Persisted entry time for {symbol!r} is invalid"
                raise ModelStateContractError(msg) from exc
            entry_time = ensure_utc(parsed_entry_time)
        bars_in_trade = payload.get("bars_in_trade", 0)
        if (
            isinstance(bars_in_trade, bool)
            or not isinstance(bars_in_trade, int)
            or bars_in_trade < 0
        ):
            msg = f"Persisted bars_in_trade for {symbol!r} is invalid"
            raise ModelStateContractError(msg)
        custom = payload.get("custom", {})
        if not isinstance(custom, Mapping):
            msg = f"Persisted custom state for {symbol!r} must be an object"
            raise ModelStateContractError(msg)
        return StrategyState(
            position=position,
            entry_price=entry_price,
            entry_time=entry_time,
            bars_in_trade=bars_in_trade,
            custom=dict(custom),
        )

    def _validate_restored_model_state(
        self,
        symbol_states: Mapping[str, StrategyState],
    ) -> None:
        """Validate strategy-specific state invariants before use."""
        del symbol_states

    def bootstrap_history(self, bars: list[MarketState]) -> None:
        """Feed historical bars to warm up indicators without emitting signals.

        This is called by the signal worker on startup before live processing
        begins. Subclasses should override ``on_data()`` to update indicators
        regardless of bootstrapping state; the base class suppresses emission
        during bootstrap by setting ``self._bootstrapping = True``.

        Args:
            bars: Historical bars sorted oldest-first
        """
        if not self._is_initialized:
            self.initialize()
            self._is_initialized = True

        self._bootstrapping = True
        try:
            for bar in bars:
                self.record_bar(bar.symbol)
                self.on_data(bar)
        finally:
            self._bootstrapping = False

    def rebuild_model_history(self, bars: list[MarketState]) -> None:
        """Recompute indicators and shared model decisions without side effects.

        Historical correction recovery uses this only with a fresh strategy
        instance. Unlike ordinary startup bootstrap, decision logic runs after
        warmup so the desired position is reconstructed from corrected history.
        Every emitter side effect is suppressed; the worker persists the final
        snapshot before it clears the captured watermark generation.
        """
        if not self._is_initialized:
            self.initialize()
            self._is_initialized = True
        if self._warmup_bars_received or self._symbol_states:
            msg = f"Historical model rebuild for {self.strategy_id!r} requires a fresh core"
            raise ModelStateContractError(msg)

        with self.suppress_all_emissions():
            for bar in bars:
                self.record_bar(bar.symbol)
                self.on_data(bar)

    @contextmanager
    def suppress_all_emissions(self) -> Iterator[None]:
        """Suppress every emitter side effect during authoritative state replay.

        Ordinary startup bootstrap may emit a reduce-only CLOSE to flatten a
        restored downstream position. A historical price correction is
        different: replay is rebuilding an in-memory candidate from changed
        history, so neither entries nor exits may escape before its generation
        fence is acknowledged.
        """

        self._emission_suppression_depth += 1
        try:
            yield
        finally:
            self._emission_suppression_depth -= 1

    @property
    def supports_flat_model_boundary(self) -> bool:
        """Whether this core can reset only execution-model state after bootstrap.

        The default is deliberately fail-closed. The runtime cannot infer
        which private fields in an arbitrary stateful strategy represent a
        virtual position rather than warmed indicators or learned parameters.
        """

        return False

    def reset_model_to_flat_for_evaluation(self, symbols: tuple[str, ...]) -> None:
        """Reset virtual position state while preserving bootstrapped signals.

        Stateful strategies must override this hook and advertise
        :attr:`supports_flat_model_boundary`.  It is invoked after indicator
        bootstrap and before the first scored bar.
        """

        del symbols
        msg = f"Strategy {self.strategy_id!r} does not implement a flat model boundary"
        raise NotImplementedError(msg)

    def apply_flat_model_evaluation_boundary(self, symbols: tuple[str, ...]) -> None:
        """Initialize if needed and apply the advertised fail-closed boundary hook."""

        if not self.supports_flat_model_boundary:
            msg = f"Strategy {self.strategy_id!r} does not support a flat model boundary"
            raise RuntimeError(msg)
        if not self._is_initialized:
            self.initialize()
            self._is_initialized = True
        self.reset_model_to_flat_for_evaluation(symbols)

    def record_bar(self, symbol: str) -> None:
        """Record that one more bar has been observed for the given symbol."""
        self._warmup_bars_received[symbol] = self._warmup_bars_received.get(symbol, 0) + 1

    def warmup_complete(self, symbol: str | None = None) -> bool:
        """Check if warmup period is complete.

        When ``warmup_bars_needed`` is 0 (default), always returns True.
        Otherwise checks that at least that many bars have been received
        for the given symbol (or any symbol if symbol is None).

        Args:
            symbol: Optional symbol to check. If None, returns True only
                    when ALL tracked symbols have enough bars, or if no
                    warmup is needed.

        Returns:
            True if strategy is ready to generate signals
        """
        needed = self.warmup_bars_needed
        if needed <= 0:
            return True
        if symbol is not None:
            return self._warmup_bars_received.get(symbol, 0) >= needed
        # If no symbols tracked yet, not warmed up
        if not self._warmup_bars_received:
            return False
        return all(v >= needed for v in self._warmup_bars_received.values())

    @staticmethod
    def _entry_bracket_is_valid(signal: Signal) -> bool:
        """Reject directionally-inconsistent SL/TP brackets on entry signals.

        A LONG must have ``stop_loss < entry < take_profit``; a SHORT the
        reverse. An inverted bracket (e.g. a stale swing low printing above
        entry) yields negative risk, an immediate stop-out, and a take-profit
        on the wrong side of entry, so it must never reach execution. Each leg
        is optional, so it is only checked when both it and ``entry_price`` are
        present. Non-entry actions (CLOSE/HOLD) carry no bracket and are valid.
        """
        if signal.action not in (SignalAction.LONG, SignalAction.SHORT):
            return True
        entry = signal.entry_price
        if entry is None:
            return True
        sl = signal.stop_loss
        tp = signal.take_profit
        if signal.action == SignalAction.LONG:
            sl_ok = sl is None or sl < entry
            tp_ok = tp is None or tp > entry
        else:  # SHORT
            sl_ok = sl is None or sl > entry
            tp_ok = tp is None or tp < entry
        return sl_ok and tp_ok

    def entry_signal_is_actionable(
        self,
        state: MarketState,
        *,
        expires_at: datetime,
    ) -> bool:
        """Return whether an entry still has execution time when evaluated.

        Production workers attach their wall-clock evaluation time to the
        ``MarketState``. Short-lived strategies must check that context before
        emitting an entry and advancing shared model state; downstream scoring
        acceptance is not proof that execution accepted the order. Historical
        engines intentionally omit the processing clock and retain deterministic
        event-time behavior.

        Paper/live construction without worker timing context fails closed.
        ``CLOSE`` signals must not use this entry-only gate because execution
        permits stale reduce-only intent.
        """
        processing_timestamp = state.metadata.get("processing_timestamp")
        if processing_timestamp is None:
            signal_source = str(self.config.get("signal_source") or "").strip().lower()
            if signal_source in {"paper", "live"}:
                logger.error(
                    "Suppressing entry without production processing timestamp",
                    strategy_id=self.strategy_id,
                    symbol=state.symbol,
                    signal_source=signal_source,
                )
                return False
            return True
        if not isinstance(processing_timestamp, datetime):
            logger.error(
                "Suppressing entry with invalid processing timestamp",
                strategy_id=self.strategy_id,
                symbol=state.symbol,
                processing_timestamp_type=type(processing_timestamp).__name__,
            )
            return False

        processed_at = ensure_utc(processing_timestamp)
        deadline = ensure_utc(expires_at)
        if processed_at >= deadline:
            logger.info(
                "Suppressing expired entry before signal emission",
                strategy_id=self.strategy_id,
                symbol=state.symbol,
                bar_timestamp=ensure_utc(state.timestamp).isoformat(),
                processing_timestamp=processed_at.isoformat(),
                expires_at=deadline.isoformat(),
            )
            return False
        return True

    def emit_signal(
        self,
        action: SignalAction,
        symbol: str,
        confidence: float = 1.0,
        timestamp: datetime | None = None,
        entry_price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        horizon: str | None = None,
        expected_return: float | None = None,
        predicted_risk: float | None = None,
        horizon_days: float | None = None,
        size_hint: float | None = None,
        asset_class: str | None = None,
        sector: str | None = None,
        industry: str | None = None,
        index: str | None = None,
        instrument_id: int | str | None = None,
        strategy_version: str | None = None,
        source: str | None = None,
        external_signal_id: str | None = None,
        expires_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Signal:
        """
        Emit a trading signal.

        This is the ONLY way to express trading intent.
        Never call broker APIs or execute trades directly.

        Args:
            action: Signal action (LONG, SHORT, CLOSE, HOLD)
            symbol: Trading symbol
            confidence: Confidence score [0.0, 1.0]
            timestamp: Signal generation timestamp
            entry_price: Suggested entry price
            stop_loss: Suggested stop loss
            take_profit: Suggested take profit
            horizon: Trading horizon
            expected_return: Explicit calibrated return forecast over the horizon
            predicted_risk: Explicit calibrated risk forecast over the horizon
            horizon_days: Numeric evaluation horizon in days
            size_hint: Optional position size hint
            asset_class: Asset class classification
            sector: Optional sector grouping
            industry: Optional industry grouping
            index: Optional index grouping
            instrument_id: Optional instrument identifier
            strategy_version: Optional strategy version
            source: Optional source environment
            expires_at: Optional hard expiry for execution eligibility
            metadata: Additional signal metadata

        Returns:
            The emitted Signal
        """
        payload_metadata = metadata or {}
        ts = timestamp or now_utc()
        # Strategy configs carry stable attribution fields.  Resolve them here
        # so exits and older cores cannot silently drop the fields merely
        # because a convenience call omitted them.  Runtime source is injected
        # by the runner (``paper``/``live``); a backtest constructed directly
        # without that setting deliberately remains unattributed rather than
        # being mislabeled as a live or paper signal.
        resolved_asset_class = asset_class or self.config.get("asset_class")
        resolved_strategy_version = strategy_version or self.config.get("strategy_version")
        resolved_source = source or self.config.get("signal_source")
        if resolved_asset_class is not None:
            resolved_asset_class = str(resolved_asset_class)
        if resolved_strategy_version is not None:
            resolved_strategy_version = str(resolved_strategy_version)
        if resolved_source is not None:
            resolved_source = str(resolved_source)
        # Idempotency by construction: every emission carries a deterministic
        # external_signal_id derived from (strategy, symbol, action, bar close).
        # A core may pass one explicitly (e.g. SwingHighLowPMO, with a
        # strategy-specific reason); otherwise the base class computes it so the
        # whole fleet dedups on re-ingestion of the same bar (worker restart,
        # NOTIFY redelivery, dual-runtime overlap) without per-core boilerplate.
        resolved_external_signal_id = external_signal_id or payload_metadata.get(
            "external_signal_id"
        )
        if resolved_external_signal_id is None:
            resolved_external_signal_id = compute_external_signal_id(
                strategy_id=self.strategy_id,
                symbol=symbol,
                action=action,
                bar_close_ts=ts,
                strategy_version=resolved_strategy_version,
                reason=payload_metadata.get("exit_reason"),
            )
        signal = Signal(
            strategy_id=self.strategy_id,
            strategy_type=self.strategy_type,
            symbol=symbol,
            action=action,
            confidence=confidence,
            timestamp=ts,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            horizon=horizon,
            expected_return=expected_return,
            predicted_risk=predicted_risk,
            horizon_days=horizon_days,
            size_hint=size_hint,
            asset_class=resolved_asset_class,
            sector=sector,
            industry=industry,
            index=index,
            instrument_id=instrument_id,
            strategy_version=resolved_strategy_version,
            source=resolved_source,
            external_signal_id=resolved_external_signal_id,
            expires_at=expires_at,
            metadata=payload_metadata,
        )

        # Suppress emission until warmup completes. During bootstrap (history
        # replay) suppress ENTRY emissions — acting on a historical entry would
        # open a stale position — but still allow reduce-only EXITS (CLOSE). A
        # replayed close can safely flatten downstream exposure when a restored
        # model state reaches its exit; on a fresh deploy with no matching open
        # execution it deduplicates/no-ops downstream (G8). Durable model-state
        # restoration is a separate promotion requirement.
        is_exit = signal.action == SignalAction.CLOSE
        should_emit = (
            self._emission_suppression_depth == 0
            and self.warmup_complete(symbol)
            and (not self._bootstrapping or is_exit)
        )
        if should_emit and not self._entry_bracket_is_valid(signal):
            logger.error(
                "Suppressing entry signal with invalid SL/TP bracket",
                strategy_id=self.strategy_id,
                symbol=symbol,
                action=signal.action.value,
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
            )
            should_emit = False
        if should_emit:
            self._emitter.emit(signal)
        return signal

    def emit_long(
        self,
        symbol: str,
        entry_price: float | None = None,
        confidence: float = 1.0,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        **kwargs: Any,
    ) -> Signal:
        """Convenience method to emit LONG signal."""
        return self.emit_signal(
            action=SignalAction.LONG,
            symbol=symbol,
            confidence=confidence,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            **kwargs,
        )

    def emit_short(
        self,
        symbol: str,
        entry_price: float | None = None,
        confidence: float = 1.0,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        **kwargs: Any,
    ) -> Signal:
        """Convenience method to emit SHORT signal."""
        return self.emit_signal(
            action=SignalAction.SHORT,
            symbol=symbol,
            confidence=confidence,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            **kwargs,
        )

    def emit_close(
        self,
        symbol: str,
        exit_price: float | None = None,
        reason: str | None = None,
        **kwargs: Any,
    ) -> Signal:
        """Convenience method to emit CLOSE signal."""
        metadata = kwargs.pop("metadata", {})
        if reason:
            metadata["exit_reason"] = reason

        return self.emit_signal(
            action=SignalAction.CLOSE,
            symbol=symbol,
            entry_price=exit_price,  # Using entry_price field for exit price
            metadata=metadata,
            **kwargs,
        )

    def flush(self) -> None:
        """Flush any buffered signals."""
        self._emitter.flush()

    def run(self, data: list[MarketState]) -> list[Signal]:
        """
        Run strategy on historical data (for backtesting).

        Args:
            data: List of MarketState objects

        Returns:
            List of generated signals
        """
        from lib_strategy.signals.emitter import BacktestSignalEmitter  # noqa: PLC0415

        # Use backtest emitter to collect signals
        original_emitter = self._emitter
        backtest_emitter = BacktestSignalEmitter()
        self._emitter = backtest_emitter

        try:
            if not self._is_initialized:
                self.initialize()
                self._is_initialized = True

            for state in data:
                self.record_bar(state.symbol)
                self.on_data(state)

            self.flush()
            return list(backtest_emitter.get_signals())
        finally:
            self._emitter = original_emitter
