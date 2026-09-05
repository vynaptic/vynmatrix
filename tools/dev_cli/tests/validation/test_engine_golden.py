"""Deterministic validation-engine golden and unit tests.

Drives a minimal momentum strategy over a FROZEN fixture of REAL Coinbase
BTC-USD 1m candles through the production BarConsolidator + strategy.run(),
and locks the resulting metrics. The strategy is intentionally trivial — the
harness (consolidation convention, no-lookahead fills, P&L + metrics math) is
what's under test, on real market data.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from validation_helpers import bars_from_ohlcv, load_market_fixture

from dev_cli.validation.backtest.engine import BacktestConfig, BacktestEngine, BacktestReport
from dev_cli.validation.backtest.execution import execution_bar_times
from dev_cli.validation.backtest.metrics import max_drawdown_pct
from dev_cli.validation.backtest.simulator import FillSimulator
from lib_data.bars import Bar
from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy
from lib_strategy.signals.signal import Signal, SignalAction
from lib_strategy.signals.utils import compute_external_signal_id

_FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "tests/fixtures/market_data/coinbase_btcusd_1m_2026-06-10.json"
)


class _MomentumStrategy(PureSignalStrategy):
    """Go long after an up bar, flatten after a down bar. Deterministic: signal
    timestamps come from the bar (never wall-clock)."""

    def __init__(self) -> None:
        super().__init__(strategy_id="momentum_test", strategy_type="indicator")

    @property
    def warmup_bars_needed(self) -> int:
        return 2

    def initialize(self) -> None:
        self._prev_close: float | None = None
        self._position = 0

    def on_data(self, state: MarketState) -> None:
        if self._prev_close is not None and self.warmup_complete(state.symbol):
            if state.close > self._prev_close and self._position <= 0:
                self.emit_long(
                    symbol=state.symbol, entry_price=state.close, timestamp=state.timestamp
                )
                self._position = 1
            elif state.close < self._prev_close and self._position > 0:
                self.emit_close(
                    symbol=state.symbol, exit_price=state.close, timestamp=state.timestamp
                )
                self._position = 0
        self._prev_close = state.close


def _run() -> BacktestReport:
    fixture = load_market_fixture(_FIXTURE)
    bars = bars_from_ohlcv(fixture["bars"], symbol="BTCUSD", source="coinbase_live")
    cfg = BacktestConfig(symbol="BTCUSD", consolidation_minutes=15)
    return BacktestEngine().run(_MomentumStrategy(), bars, cfg)


def test_backtest_is_deterministic() -> None:
    assert _run().to_dict() == _run().to_dict()


def test_consolidation_uses_production_period_close_stamping() -> None:
    report = _run()
    # 1501 1m bars -> 100 complete 15m periods (trailing partial dropped).
    assert report.consolidated_bars == 100
    # Production BarConsolidator stamps the bar at the period CLOSE (:15, not :00).
    first_ts = report.equity_curve[0][0]
    assert first_ts == datetime(2026, 6, 10, 0, 15, tzinfo=UTC)
    assert all(ts.minute % 15 == 0 for ts, _ in report.equity_curve)


def test_golden_metrics_locked() -> None:
    report = _run()
    d = report.to_dict()
    assert d["execution_policy_id"] == "next_open_bracket_v2"
    assert d["cost_scenario"] == "configured_flat_fee"
    assert report.config.fill_policy.timestamp_contract == "explicit_interval"
    # Locked golden values for the real-data fixture (regression guard).
    assert d["signals_emitted"] == 59
    assert d["total_trades"] == 29
    assert d["winning_trades"] == 5
    assert d["losing_trades"] == 24
    # The last-bar signal has no next bar to fill against -> dropped (no lookahead).
    assert d["unfilled_signals"] == 1
    assert d["final_equity"] == pytest.approx(93051.8315637, rel=1e-6)
    assert d["total_return_pct"] == pytest.approx(-6.9481684, rel=1e-6)
    assert d["max_drawdown_pct"] == pytest.approx(6.9481684, rel=1e-6)
    assert d["win_rate_pct"] == pytest.approx(17.2413793, rel=1e-6)
    assert d["profit_factor"] == pytest.approx(0.1285144, rel=1e-6)
    # ~1 day of data -> fewer than 2 daily returns -> ratios are undefined (0.0).
    assert d["sharpe_ratio"] == 0.0
    assert d["sortino_ratio"] == 0.0


class _LedgerStrategy(PureSignalStrategy):
    """Emit a fixed sequence while recording how often the core is invoked."""

    def __init__(self) -> None:
        super().__init__(strategy_id="ledger_test", strategy_type="indicator")
        self.run_calls = 0
        self.on_data_calls = 0

    def initialize(self) -> None:
        self._sequence_index = 0

    def run(self, data: list[MarketState]) -> list[Signal]:
        self.run_calls += 1
        return super().run(data)

    def on_data(self, state: MarketState) -> None:
        self.on_data_calls += 1
        actions = (SignalAction.LONG, SignalAction.CLOSE, SignalAction.HOLD)
        if self._sequence_index >= len(actions):
            return
        action = actions[self._sequence_index]
        self.emit_signal(
            action=action,
            symbol=state.symbol,
            confidence=0.75,
            timestamp=state.timestamp,
            entry_price=state.close,
            stop_loss=state.close * 0.9 if action is SignalAction.LONG else None,
            take_profit=state.close * 1.2 if action is SignalAction.LONG else None,
            horizon="swing",
            expected_return=0.02,
            predicted_risk=0.01,
            horizon_days=5.0,
            expires_at=state.timestamp + timedelta(days=1),
            metadata={"private_runtime_note": "must-not-enter-evidence"},
        )
        self._sequence_index += 1


def _run_ledger_strategy() -> tuple[_LedgerStrategy, BacktestReport]:
    strategy = _LedgerStrategy()
    report = BacktestEngine().run(
        strategy,
        _bars(),
        BacktestConfig(symbol="BTCUSD", consolidation_minutes=15),
    )
    return strategy, report


def test_raw_signal_ledger_preserves_emission_order_and_single_core_run() -> None:
    strategy, report = _run_ledger_strategy()

    assert strategy.run_calls == 1
    assert strategy.on_data_calls == report.consolidated_bars
    assert report.signals_emitted == len(report.raw_signal_ledger) == 3
    assert [item.action for item in report.raw_signal_ledger] == ["LONG", "CLOSE", "HOLD"]
    assert [item.generated_at for item in report.raw_signal_ledger] == [
        datetime(2026, 6, 10, 0, 15, tzinfo=UTC),
        datetime(2026, 6, 10, 0, 30, tzinfo=UTC),
        datetime(2026, 6, 10, 0, 45, tzinfo=UTC),
    ]
    assert [item.external_signal_id for item in report.raw_signal_ledger] == [
        compute_external_signal_id(
            strategy_id="ledger_test",
            symbol="BTCUSD",
            action=action,
            bar_close_ts=timestamp,
        )
        for action, timestamp in zip(
            (SignalAction.LONG, SignalAction.CLOSE, SignalAction.HOLD),
            (item.generated_at for item in report.raw_signal_ledger),
            strict=True,
        )
    ]


def test_raw_signal_ledger_is_stable_immutable_and_json_safe() -> None:
    _, first = _run_ledger_strategy()
    _, replay = _run_ledger_strategy()

    assert first.raw_signal_ledger == replay.raw_signal_ledger
    assert first.raw_signal_ledger[0].canonical_json == replay.raw_signal_ledger[0].canonical_json
    assert "signal_id" not in json.loads(first.raw_signal_ledger[0].canonical_json)
    assert "private_runtime_note" not in first.raw_signal_ledger[0].canonical_json
    assert first.raw_signal_ledger[0].valid_until == datetime(
        2026,
        6,
        11,
        0,
        15,
        tzinfo=UTC,
    )
    json.dumps(first.to_dict(), allow_nan=False)
    with pytest.raises(FrozenInstanceError):
        first.raw_signal_ledger[0].confidence = 0.1  # type: ignore[misc]


def _bars():  # type: ignore[no-untyped-def]
    fixture = load_market_fixture(_FIXTURE)
    return bars_from_ohlcv(fixture["bars"], symbol="BTCUSD", source="coinbase_live")


# --- focused unit tests: simulator no-lookahead + P&L, metrics math -----------


def _bar(minute: int, *, open_: float, close: float) -> Bar:
    ts = datetime(2026, 1, 1, 0, minute, tzinfo=UTC)
    return Bar(
        symbol="X",
        timestamp=ts,
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        volume=1.0,
        timeframe="1m",
        metadata={"timestamp_semantics": "period_start"},
    )


def test_simulator_fills_next_bar_open_no_lookahead() -> None:
    # Signal decided at bar0 close fills at bar1 OPEN (not bar0 close/open).
    bars = [
        _bar(0, open_=100.0, close=100.0),
        _bar(1, open_=110.0, close=115.0),
        _bar(2, open_=120.0, close=120.0),
        _bar(3, open_=90.0, close=90.0),
    ]
    long_at_0 = Signal(
        strategy_id="s",
        strategy_type="indicator",
        symbol="X",
        action=SignalAction.LONG,
        confidence=1.0,
        timestamp=execution_bar_times(bars[0]).close_ts,
    )
    close_at_2 = Signal(
        strategy_id="s",
        strategy_type="indicator",
        symbol="X",
        action=SignalAction.CLOSE,
        confidence=1.0,
        timestamp=execution_bar_times(bars[2]).close_ts,
    )
    sim = FillSimulator(initial_capital=100_000.0, size_pct=1.0, fee_bps=0.0)
    result = sim.run(bars, [long_at_0, close_at_2])

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_price == 110.0  # bar1 open (next bar after the bar0 signal)
    assert trade.exit_price == 90.0  # bar3 open (next bar after the bar2 close signal)
    assert trade.side == "long"
    # qty = 100k / 110; pnl = (90-110)*qty, no fees.
    assert trade.pnl == pytest.approx((90.0 - 110.0) * (100_000.0 / 110.0), rel=1e-9)


def test_simulator_zero_fee_long_profit() -> None:
    bars = [
        _bar(0, open_=100.0, close=100.0),
        _bar(1, open_=100.0, close=100.0),
        _bar(2, open_=200.0, close=200.0),
    ]
    long0 = Signal(
        strategy_id="s",
        strategy_type="indicator",
        symbol="X",
        action=SignalAction.LONG,
        confidence=1.0,
        timestamp=execution_bar_times(bars[0]).close_ts,
    )
    close1 = Signal(
        strategy_id="s",
        strategy_type="indicator",
        symbol="X",
        action=SignalAction.CLOSE,
        confidence=1.0,
        timestamp=execution_bar_times(bars[1]).close_ts,
    )
    sim = FillSimulator(initial_capital=100_000.0, size_pct=1.0, fee_bps=0.0)
    result = sim.run(bars, [long0, close1])
    # entry bar1 open=100, exit bar2 open=200 -> doubled.
    assert result.trades[0].pnl == pytest.approx(100_000.0, rel=1e-9)


def test_max_drawdown_pct_math() -> None:
    # 100 -> 120 (peak) -> 90 (trough) -> 110: worst dd = (120-90)/120 = 25%.
    assert max_drawdown_pct([100, 120, 90, 110]) == pytest.approx(25.0)
    assert max_drawdown_pct([100, 101, 102]) == pytest.approx(0.0)


def test_simulator_drops_unfilled_last_bar_signal() -> None:
    bars = [_bar(0, open_=100.0, close=100.0), _bar(1, open_=100.0, close=100.0)]
    long_last = Signal(
        strategy_id="s",
        strategy_type="indicator",
        symbol="X",
        action=SignalAction.LONG,
        confidence=1.0,
        timestamp=execution_bar_times(bars[1]).close_ts,
    )
    result = FillSimulator().run(bars, [long_last])
    assert result.unfilled_signals == 1
    assert result.trades == []
