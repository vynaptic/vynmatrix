"""G8: bootstrap replay suppresses ENTRIES but permits reduce-only EXITS.

The framework keeps historical entries inert while allowing a replayed CLOSE to
flatten matching downstream exposure. Durable restoration of shared model state
is tested at the future model-ledger boundary; this unit test covers only the
base emission policy.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "libs" / "python" / "lib_strategy"))

from lib_strategy.signals.emitter import SignalEmitter
from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy
from lib_strategy.signals.signal import Signal, SignalAction


class _Capture(SignalEmitter):
    def __init__(self) -> None:
        self.emitted: list[Signal] = []

    def emit(self, signal: Signal) -> None:
        self.emitted.append(signal)

    def flush(self) -> None:
        pass


class _EntryExit(PureSignalStrategy):
    """Opens long on the first bar, closes on the next — repeating."""

    @property
    def warmup_bars_needed(self) -> int:
        return 0

    def initialize(self) -> None:
        self._pos = 0

    def on_data(self, state: MarketState) -> None:
        if self._pos == 0:
            self.emit_long(symbol=state.symbol, entry_price=state.close, timestamp=state.timestamp)
            self._pos = 1
        else:
            self.emit_close(symbol=state.symbol, exit_price=state.close, timestamp=state.timestamp)
            self._pos = 0


def _bar(minute: int, close: float) -> MarketState:
    return MarketState(
        symbol="BTCUSD",
        timestamp=datetime(2026, 6, 15, 12, minute, tzinfo=UTC),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1.0,
    )


def _strat(cap: _Capture) -> _EntryExit:
    return _EntryExit(strategy_id="t", strategy_type="indicator", config={}, emitter=cap)


def test_bootstrap_emits_exit_but_not_entry() -> None:
    cap = _Capture()
    # bar0 opens (entry — suppressed), bar1 closes (exit — must still fire).
    _strat(cap).bootstrap_history([_bar(0, 100.0), _bar(1, 110.0)])

    actions = [s.action for s in cap.emitted]
    assert SignalAction.LONG not in actions, "entries must be suppressed in bootstrap"
    assert actions == [SignalAction.CLOSE], "the exit's CLOSE must still be emitted"


def test_live_path_emits_both_entries_and_exits() -> None:
    cap = _Capture()
    strat = _strat(cap)
    strat.initialize()
    strat._is_initialized = True
    # Live processing (not bootstrap): both the entry and the exit emit.
    for bar in (_bar(0, 100.0), _bar(1, 110.0)):
        strat.record_bar(bar.symbol)
        strat.on_data(bar)

    actions = [s.action for s in cap.emitted]
    assert SignalAction.LONG in actions
    assert SignalAction.CLOSE in actions


def test_authoritative_rebuild_suppresses_entries_and_exits() -> None:
    cap = _Capture()
    strat = _strat(cap)

    with strat.suppress_all_emissions():
        strat.bootstrap_history([_bar(0, 100.0), _bar(1, 110.0)])

    assert cap.emitted == []
