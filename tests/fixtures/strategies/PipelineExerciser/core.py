"""Deterministic pipeline-exerciser core for the PostgreSQL integration gate.

This is TEST HARNESS code, not a strategy: it emits LONG/CLOSE pairs at a
fixed, pre-registered schedule of real fixture-bar timestamps so the
journal → relay → scoring → outbox → execution → replay → feedback chain can
be asserted deterministically. It lives under ``tests/fixtures`` on purpose —
it has no config.json in ``strategies/indicator``, can never be selected by
``STRATEGY_LIST``, and claims no alpha. The emit contract (external signal
id, TTL expiry, price provenance passthrough, protective levels) mirrors the
production scalpers this gate previously drove, so every downstream
expectation keeps its meaning.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy
from lib_strategy.signals.signal import SignalAction
from lib_strategy.signals.utils import compute_external_signal_id, extract_price_provenance

_EVAL_HORIZON = "1H"


class PipelineExerciserCore(PureSignalStrategy):
    """Emit longs/closes at the exact scheduled fixture timestamps."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            strategy_id=kwargs.pop("strategy_id", "pipeline_exerciser_v1"),
            strategy_type="indicator",
            config=kwargs.pop("config", {}),
            emitter=kwargs.pop("emitter", None),
        )

    @property
    def warmup_bars_needed(self) -> int:
        return 1

    def initialize(self) -> None:
        cfg = self.config
        self._tp_pct = float(cfg.get("take_profit_pct", 0.004))
        self._sl_pct = float(cfg.get("stop_loss_pct", 0.006))
        self._confidence = float(cfg.get("confidence", 0.7))
        self._signal_ttl_seconds = int(cfg.get("signal_ttl_seconds", 60))
        self._strategy_version = cfg.get("strategy_version")
        schedule = cfg.get("schedule") or []
        if not schedule:
            msg = "PipelineExerciser requires a non-empty schedule of entry/close pairs"
            raise ValueError(msg)
        self._entries: dict[datetime, datetime] = {}
        self._closes: set[datetime] = set()
        for entry_iso, close_iso in schedule:
            entry_ts = datetime.fromisoformat(str(entry_iso))
            close_ts = datetime.fromisoformat(str(close_iso))
            if close_ts <= entry_ts:
                msg = "schedule close must be after its entry"
                raise ValueError(msg)
            self._entries[entry_ts] = close_ts
            self._closes.add(close_ts)

    def on_data(self, state: MarketState) -> None:
        symbol = state.symbol
        sym_state = self.state_for(symbol)
        expires_at = state.timestamp + timedelta(seconds=self._signal_ttl_seconds)
        if sym_state.position == 0 and state.timestamp in self._entries:
            if not self.entry_signal_is_actionable(state, expires_at=expires_at):
                return
            entry = state.close
            ext_id = compute_external_signal_id(
                strategy_id=self.strategy_id,
                symbol=symbol,
                action=SignalAction.LONG,
                bar_close_ts=state.timestamp,
                strategy_version=self._strategy_version,
                reason="exerciser_entry",
            )
            self.emit_long(
                symbol=symbol,
                entry_price=entry,
                confidence=self._confidence,
                timestamp=state.timestamp,
                horizon=_EVAL_HORIZON,
                stop_loss=entry * (1 - self._sl_pct),
                take_profit=entry * (1 + self._tp_pct),
                external_signal_id=ext_id,
                expires_at=expires_at,
                strategy_version=self._strategy_version,
                metadata={
                    "external_signal_id": ext_id,
                    "reason": "exerciser_entry",
                    **extract_price_provenance(state.metadata),
                },
            )
            sym_state.position = 1
            sym_state.entry_price = entry
            sym_state.entry_time = state.timestamp
        elif sym_state.position == 1 and state.timestamp in self._closes:
            ext_id = compute_external_signal_id(
                strategy_id=self.strategy_id,
                symbol=symbol,
                action=SignalAction.CLOSE,
                bar_close_ts=state.timestamp,
                strategy_version=self._strategy_version,
                reason="exerciser_close",
            )
            self.emit_close(
                symbol=symbol,
                exit_price=state.close,
                timestamp=state.timestamp,
                reason="exerciser_close",
                horizon=_EVAL_HORIZON,
                external_signal_id=ext_id,
                expires_at=expires_at,
                strategy_version=self._strategy_version,
                metadata={
                    "external_signal_id": ext_id,
                    "reason": "exerciser_close",
                    **extract_price_provenance(state.metadata),
                },
            )
            sym_state.position = 0
            sym_state.entry_price = None
            sym_state.entry_time = None
