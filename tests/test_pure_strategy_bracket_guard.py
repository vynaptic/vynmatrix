"""IND-1: PureSignalStrategy suppresses entry signals with inverted SL/TP brackets.

A LONG must have ``stop_loss < entry < take_profit`` and a SHORT the reverse.
An inverted bracket (e.g. a stale swing low printing above entry) yields negative
risk, an immediate stop-out, and a take-profit on the wrong side of entry. The
emit-layer guard in ``PureSignalStrategy.emit_signal`` is the fleet-wide backstop
that keeps any such signal from reaching execution, regardless of which strategy
produced it.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "libs" / "python" / "lib_strategy"))

from lib_strategy.signals.emitter import BacktestSignalEmitter
from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy

TS = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


class _Strat(PureSignalStrategy):
    def initialize(self) -> None:  # pragma: no cover - not exercised here
        ...

    def on_data(self, state: MarketState) -> None:  # pragma: no cover - not exercised here
        ...


def _strat() -> tuple[_Strat, BacktestSignalEmitter]:
    emitter = BacktestSignalEmitter()
    strat = _Strat(
        strategy_id="bracket_guard_test_v1",
        strategy_type="indicator",
        config={},
        emitter=emitter,
    )
    return strat, emitter


# ── Valid brackets are emitted ───────────────────────────────────────────


def test_valid_long_bracket_is_emitted() -> None:
    strat, emitter = _strat()
    strat.emit_long(
        symbol="BTCUSD", entry_price=100.0, stop_loss=95.0, take_profit=110.0, timestamp=TS
    )
    assert len(emitter.get_signals()) == 1


def test_valid_short_bracket_is_emitted() -> None:
    strat, emitter = _strat()
    strat.emit_short(
        symbol="BTCUSD", entry_price=100.0, stop_loss=105.0, take_profit=90.0, timestamp=TS
    )
    assert len(emitter.get_signals()) == 1


def test_entry_without_bracket_is_emitted() -> None:
    # No stop_loss / take_profit -> nothing to validate, must still emit.
    strat, emitter = _strat()
    strat.emit_long(symbol="BTCUSD", entry_price=100.0, timestamp=TS)
    assert len(emitter.get_signals()) == 1


# ── Inverted LONG brackets are suppressed ────────────────────────────────


def test_long_stop_above_entry_is_suppressed() -> None:
    # The exact IND-1 scenario: swing low (stop) prints above entry.
    strat, emitter = _strat()
    sig = strat.emit_long(
        symbol="BTCUSD", entry_price=100.0, stop_loss=105.0, take_profit=110.0, timestamp=TS
    )
    assert emitter.get_signals() == []
    # The Signal is still returned to the caller for bookkeeping.
    assert sig.action.value == "LONG"


def test_long_stop_equal_entry_is_suppressed() -> None:
    strat, emitter = _strat()
    strat.emit_long(
        symbol="BTCUSD", entry_price=100.0, stop_loss=100.0, take_profit=110.0, timestamp=TS
    )
    assert emitter.get_signals() == []


def test_long_take_profit_below_entry_is_suppressed() -> None:
    strat, emitter = _strat()
    strat.emit_long(
        symbol="BTCUSD", entry_price=100.0, stop_loss=95.0, take_profit=99.0, timestamp=TS
    )
    assert emitter.get_signals() == []


# ── Inverted SHORT brackets are suppressed ───────────────────────────────


def test_short_stop_below_entry_is_suppressed() -> None:
    strat, emitter = _strat()
    strat.emit_short(
        symbol="BTCUSD", entry_price=100.0, stop_loss=95.0, take_profit=90.0, timestamp=TS
    )
    assert emitter.get_signals() == []


def test_short_take_profit_above_entry_is_suppressed() -> None:
    strat, emitter = _strat()
    strat.emit_short(
        symbol="BTCUSD", entry_price=100.0, stop_loss=105.0, take_profit=101.0, timestamp=TS
    )
    assert emitter.get_signals() == []


# ── Non-entry actions are never bracket-checked ──────────────────────────


def test_close_is_never_bracket_checked() -> None:
    # CLOSE carries no bracket; odd price fields must not suppress an exit.
    strat, emitter = _strat()
    strat.emit_close(symbol="BTCUSD", exit_price=100.0, timestamp=TS)
    assert len(emitter.get_signals()) == 1
