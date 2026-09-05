"""Terminal-liquidation and execution-derived event-horizon regressions."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from validation_helpers import bars_from_ohlcv, load_market_fixture

from dev_cli.validation.backtest.engine import BacktestConfig, BacktestEngine, BacktestReport
from dev_cli.validation.backtest.execution import ExecutionCostModel, FillPolicy
from dev_cli.validation.backtest.folds import (
    TimestampFoldWindow,
    build_report_event_intervals,
    build_timestamp_anchored_folds,
)
from dev_cli.validation.backtest.simulator import TerminalPosition
from dev_cli.validation.backtest.walk_forward import (
    build_terminal_liquidation_cost_policy,
    estimate_terminal_liquidation,
)
from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy

_FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "tests/fixtures/market_data/coinbase_btcusd_1m_2026-06-10.json"
)
_COST_MODEL = ExecutionCostModel(
    scenario_name="expected-test",
    version="execution-cost-test-v1",
    commission_bps=10.0,
    half_spread_bps=5.0,
    slippage_bps=3.0,
    impact_bps=2.0,
    latency_bars=0,
    historical_basis="backward-applied-scenario",
)


class _ScheduledPositionStrategy(PureSignalStrategy):
    def __init__(self, *, side: str, close_index: int | None) -> None:
        super().__init__(strategy_id=f"scheduled_{side}", strategy_type="indicator")
        self._side = side
        self._close_index = close_index

    def initialize(self) -> None:
        self._index = 0

    def on_data(self, state: MarketState) -> None:
        if self._index == 1:
            if self._side == "long":
                self.emit_long(
                    symbol=state.symbol,
                    entry_price=state.close,
                    timestamp=state.timestamp,
                )
            else:
                self.emit_short(
                    symbol=state.symbol,
                    entry_price=state.close,
                    timestamp=state.timestamp,
                )
        elif self._close_index is not None and self._index == self._close_index:
            self.emit_close(
                symbol=state.symbol,
                exit_price=state.close,
                timestamp=state.timestamp,
            )
        self._index += 1


def _report(*, side: str = "long", close_index: int | None = None) -> BacktestReport:
    fixture = load_market_fixture(_FIXTURE)
    bars = bars_from_ohlcv(
        fixture["bars"],
        symbol="BTCUSD",
        source="coinbase_live",
    )
    return BacktestEngine().run(
        _ScheduledPositionStrategy(side=side, close_index=close_index),
        bars,
        BacktestConfig(
            symbol="BTCUSD",
            consolidation_minutes=15,
            initial_capital=100_000.0,
            size_pct=0.50,
            execution_costs=_COST_MODEL,
            fill_policy=FillPolicy.reference_v2(),
        ),
    )


@pytest.mark.parametrize(("side", "transaction_side"), [("long", -1), ("short", 1)])
def test_terminal_liquidation_matches_simulator_cost_math_without_mutation(
    side: str,
    transaction_side: int,
) -> None:
    report = _report(side=side)
    before = report.to_dict()

    estimate = estimate_terminal_liquidation(report, _COST_MODEL)
    policy = build_terminal_liquidation_cost_policy(
        _COST_MODEL,
        policy_id="expected-terminal-liquidation-v1",
    )
    policy_cost = policy.estimator(report)

    terminal = report.terminal_position
    assert terminal is not None
    adverse_rate = _COST_MODEL.adverse_price_bps / 10_000.0
    expected_execution_price = terminal.mark_price * (1.0 + transaction_side * adverse_rate)
    reference_notional = terminal.quantity * terminal.mark_price
    expected_commission = (
        terminal.quantity * expected_execution_price * _COST_MODEL.commission_bps / 10_000.0
    )

    assert estimate.position_side == side
    assert estimate.transaction_side == transaction_side
    assert estimate.execution_price == pytest.approx(expected_execution_price)
    assert estimate.costs.commission == pytest.approx(expected_commission)
    assert estimate.costs.half_spread == pytest.approx(
        reference_notional * _COST_MODEL.half_spread_bps / 10_000.0
    )
    assert estimate.costs.slippage == pytest.approx(
        reference_notional * _COST_MODEL.slippage_bps / 10_000.0
    )
    assert estimate.costs.impact == pytest.approx(
        reference_notional * _COST_MODEL.impact_bps / 10_000.0
    )
    assert policy.policy_id == "expected-terminal-liquidation-v1"
    assert policy_cost == pytest.approx(estimate.total_cost)
    assert report.to_dict() == before


def test_terminal_liquidation_rejects_absent_or_invalid_exposure() -> None:
    closed = _report(close_index=5)
    with pytest.raises(ValueError, match="requires an open terminal position"):
        estimate_terminal_liquidation(closed, _COST_MODEL)

    open_report = _report()
    assert open_report.terminal_position is not None
    invalid_side = replace(open_report.terminal_position, side="flat")
    invalid_quantity = replace(open_report.terminal_position, quantity=math.nan)
    with pytest.raises(ValueError, match="side must be long or short"):
        estimate_terminal_liquidation(
            replace(open_report, terminal_position=invalid_side),
            _COST_MODEL,
        )
    with pytest.raises(ValueError, match="quantity must be finite and positive"):
        estimate_terminal_liquidation(
            replace(open_report, terminal_position=invalid_quantity),
            _COST_MODEL,
        )
    with pytest.raises(ValueError, match="final mark requires zero latency"):
        estimate_terminal_liquidation(
            open_report,
            replace(_COST_MODEL, latency_bars=1),
        )


def test_report_event_intervals_union_exact_trade_and_terminal_horizons() -> None:
    closed = _report(close_index=10)
    terminal = _report()
    timestamps = tuple(timestamp for timestamp, _ in closed.equity_curve)

    intervals = build_report_event_intervals(timestamps, [closed, terminal])
    entry_timestamp = closed.raw_signal_ledger[0].generated_at
    entry_index = timestamps.index(entry_timestamp)

    assert set(intervals) == set(range(len(timestamps)))
    assert intervals[entry_index].start == entry_timestamp
    assert intervals[entry_index].end == timestamps[-1]
    assert all(
        interval.start == interval.end
        for index, interval in intervals.items()
        if index != entry_index
    )


def test_boundary_crossing_execution_interval_is_purged() -> None:
    report = _report(close_index=10)
    timestamps = tuple(timestamp for timestamp, _ in report.equity_curve)
    intervals = build_report_event_intervals(timestamps, [report])
    entry_timestamp = report.raw_signal_ledger[0].generated_at
    entry_index = timestamps.index(entry_timestamp)

    fold = build_timestamp_anchored_folds(
        timestamps,
        windows=(
            TimestampFoldWindow(
                train_end_exclusive=timestamps[5],
                test_start=timestamps[5],
                test_end_exclusive=timestamps[15],
            ),
        ),
        bootstrap_bars=0,
        pre_oos_purge=timedelta(0),
        embargo_bars=0,
        event_intervals=intervals,
    )[0]

    assert entry_index < fold.test_indices[0]
    assert intervals[entry_index].end > fold.test_start_ts
    assert entry_index in fold.purged_training_indices
    assert entry_index not in fold.training_indices


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("unmatched", "contains unmatched signals"),
        ("order", "raw-signal ledger is not strictly ordered"),
        ("coverage", "does not exactly cover the timestamp grid"),
        ("missing_trade", "entry signal has no exact execution match"),
        ("latency", "does not use zero-latency expected costs"),
    ],
)
def test_report_event_intervals_fail_closed_on_inexact_evidence(
    mutation: str,
    message: str,
) -> None:
    report = _report(close_index=5)
    timestamps = tuple(timestamp for timestamp, _ in report.equity_curve)
    mutated = report
    supplied_timestamps = timestamps
    if mutation == "unmatched":
        mutated = replace(report, unmatched_signals=1)
    elif mutation == "order":
        mutated = replace(report, raw_signal_ledger=tuple(reversed(report.raw_signal_ledger)))
    elif mutation == "coverage":
        supplied_timestamps = timestamps[:-1]
    elif mutation == "missing_trade":
        mutated = replace(report, trades=[])
    else:
        assert report.config.execution_costs is not None
        config = replace(
            report.config,
            execution_costs=replace(report.config.execution_costs, latency_bars=1),
        )
        mutated = replace(report, config=config)

    with pytest.raises(ValueError, match=message):
        build_report_event_intervals(supplied_timestamps, [mutated])


def test_report_event_intervals_reject_invalid_terminal_identity() -> None:
    report = _report()
    assert isinstance(report.terminal_position, TerminalPosition)
    invalid = replace(report.terminal_position, symbol="ETHUSD")
    timestamps = tuple(timestamp for timestamp, _ in report.equity_curve)

    with pytest.raises(ValueError, match="terminal exposure is invalid"):
        build_report_event_intervals(
            timestamps,
            [replace(report, terminal_position=invalid)],
        )
