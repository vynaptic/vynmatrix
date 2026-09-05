"""Exact-calendar joint-asset selector and boundary-accounting tests."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from validation_helpers import bars_from_ohlcv

from dev_cli.validation.backtest.engine import BacktestConfig, BacktestEngine, BacktestReport
from dev_cli.validation.backtest.execution import FillPolicy
from dev_cli.validation.backtest.folds import EventInterval, TimestampFoldWindow
from dev_cli.validation.backtest.metrics import compute_metrics
from dev_cli.validation.backtest.simulator import ModelExecutionDivergence, TerminalPosition
from dev_cli.validation.backtest.walk_forward import (
    JointAssetAnchoredResult,
    TerminalExitCostPolicy,
    run_joint_asset_timestamp_anchored_walk_forward,
)
from lib_data.bars import Bar
from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy

_SOURCE_START = datetime(2019, 12, 30, tzinfo=UTC)
_SOURCE_END = datetime(2020, 5, 6, tzinfo=UTC)
_TRAINING_START = datetime(2020, 1, 2, tzinfo=UTC)
_TEST_START = datetime(2020, 5, 1, tzinfo=UTC)
_TEST_END = datetime(2020, 5, 6, tzinfo=UTC)
_BOUNDARY_FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "tests/fixtures/market_data/coinbase_btcusdc_daily_boundaries.json"
)


class _CandidateStrategy(PureSignalStrategy):
    def __init__(self, params: Mapping[str, Any]) -> None:
        super().__init__(
            strategy_id=f"candidate_{params['candidate']}",
            strategy_type="indicator",
            config=dict(params),
        )
        self.warmup_bars_needed = 1

    @property
    def supports_flat_model_boundary(self) -> bool:
        return True

    def reset_model_to_flat_for_evaluation(self, symbols: tuple[str, ...]) -> None:
        for symbol in symbols:
            self.state_for(symbol).position = 0

    def initialize(self) -> None:
        return None

    def on_data(self, state: MarketState) -> None:
        del state


ReturnResolver = Callable[[str, str, datetime], float]


class _PanelReportEngine(BacktestEngine):
    """Deterministic report adapter used to isolate selector arithmetic."""

    def __init__(
        self,
        resolver: ReturnResolver,
        *,
        terminal_assets: frozenset[str] = frozenset(),
        failure_mode: str | None = None,
        failure_asset: str | None = None,
    ) -> None:
        self._resolver = resolver
        self._terminal_assets = terminal_assets
        self._failure_mode = failure_mode
        self._failure_asset = failure_asset

    def consolidate(
        self,
        raw_bars: list[Bar],
        *,
        period_minutes: int,
        min_coverage: float = 0.0,
    ) -> list[Bar]:
        del raw_bars, period_minutes, min_coverage
        raise AssertionError("joint selector must not reconsolidate provider daily bars")

    def run_consolidated(
        self,
        strategy: PureSignalStrategy,
        consolidated: list[Bar],
        config: BacktestConfig,
    ) -> BacktestReport:
        failure_active = self._failure_mode is not None and (
            self._failure_asset is None or self._failure_asset == config.symbol
        )
        if failure_active and self._failure_mode == "raise":
            raise RuntimeError("registered candidate failed")
        assert config.evaluation_start is not None
        evaluation = [
            bar
            for bar in consolidated
            if bar.timestamp >= config.evaluation_start
            and (config.evaluation_end is None or bar.timestamp < config.evaluation_end)
        ]
        candidate = str(strategy.config["candidate"])
        equity = config.initial_capital
        equity_curve: list[tuple[datetime, float]] = []
        for bar in evaluation:
            equity *= 1.0 + self._resolver(candidate, config.symbol, bar.timestamp)
            equity_curve.append((bar.timestamp, equity))

        terminal = None
        if config.symbol in self._terminal_assets:
            terminal = TerminalPosition(
                symbol=config.symbol,
                side="long",
                quantity=1.0,
                entry_ts=evaluation[0].timestamp,
                entry_price=100.0,
                mark_price=100.0,
                gross_unrealized_pnl=0.0,
                net_unrealized_pnl=0.0,
                stop_loss=90.0,
                take_profit=None,
                holding_bars=len(evaluation),
            )
        divergences: tuple[ModelExecutionDivergence, ...] = ()
        if failure_active and self._failure_mode == "divergence":
            divergences = (
                ModelExecutionDivergence(
                    start_ts=evaluation[0].timestamp,
                    end_ts=None,
                    model_position=1,
                    execution_position=0,
                    cause="test_unresolved",
                ),
            )
        if failure_active and self._failure_mode == "nonfinite":
            equity_curve[-1] = (equity_curve[-1][0], math.nan)

        return BacktestReport(
            config=config,
            metrics=compute_metrics(
                equity_curve,
                [],
                initial_capital=config.initial_capital,
                annualization_factor=config.annualization_factor,
            ),
            trades=[],
            equity_curve=equity_curve,
            signals_emitted=0,
            consolidated_bars=len(evaluation),
            unfilled_signals=0,
            unmatched_signals=int(failure_active and self._failure_mode == "unmatched"),
            account_ruined=failure_active and self._failure_mode == "ruin",
            terminal_position=terminal,
            model_execution_divergences=divergences,
            bootstrap_bars=sum(bar.timestamp < config.evaluation_start for bar in consolidated),
            flat_model_boundary_applied=config.require_flat_model_boundary,
        )


def _strategy_factory(params: Mapping[str, Any]) -> PureSignalStrategy:
    return _CandidateStrategy(params)


def _source_bars(symbol: str) -> list[Bar]:
    bars: list[Bar] = []
    timestamp = _SOURCE_START
    index = 0
    while timestamp < _SOURCE_END:
        close = 100.0 + index * 0.1
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=timestamp,
                open=close,
                high=close + 1.0,
                low=close - 1.0,
                close=close,
                volume=1_000.0,
                timeframe="1d",
                source="selector_boundary_fixture",
                metadata={"timestamp_semantics": "period_start"},
            )
        )
        timestamp += timedelta(days=1)
        index += 1
    return bars


def _config(symbol: str) -> BacktestConfig:
    return BacktestConfig(
        symbol=symbol,
        consolidation_minutes=1_440,
        initial_capital=100.0,
        size_pct=0.5,
        annualization_factor=365.0,
        fill_policy=FillPolicy.reference_v2(),
    )


def _window() -> TimestampFoldWindow:
    return TimestampFoldWindow(
        train_end_exclusive=_TEST_START,
        test_start=_TEST_START,
        test_end_exclusive=_TEST_END,
    )


def _run_joint(
    engine: BacktestEngine,
    candidates: list[dict[str, str]],
    *,
    assets: tuple[str, ...] = ("BTC", "ETH"),
    training_policy: TerminalExitCostPolicy | None = None,
    oos_policy: TerminalExitCostPolicy | None = None,
    selected_arm_tie_tolerance: float = 1e-12,
    baseline_registry_index: int = 0,
    event_intervals: Mapping[int, EventInterval] | None = None,
) -> JointAssetAnchoredResult:
    return run_joint_asset_timestamp_anchored_walk_forward(
        _strategy_factory,
        candidates,
        {asset: _source_bars(asset) for asset in assets},
        {asset: _config(asset) for asset in assets},
        windows=[_window()],
        training_evaluation_start=_TRAINING_START,
        bootstrap_bars=5,
        training_terminal_exit_cost_policy=training_policy,
        oos_terminal_exit_cost_policy=oos_policy,
        selected_arm_tie_tolerance=selected_arm_tie_tolerance,
        baseline_registry_index=baseline_registry_index,
        event_intervals=event_intervals,
        engine=engine,
    )


def _specialist_resolver(candidate: str, asset: str, timestamp: datetime) -> float:
    parity = timestamp.toordinal() % 2
    if candidate == "specialist":
        primary = 0.05 if parity else -0.01
        return primary if asset == "BTC" else -primary
    return 0.008 if parity else -0.002


def test_joint_selection_differs_from_single_asset_selection() -> None:
    candidates = [{"candidate": "specialist"}, {"candidate": "balanced"}]
    single = _run_joint(
        _PanelReportEngine(_specialist_resolver),
        candidates,
        assets=("BTC",),
    )
    joint = _run_joint(_PanelReportEngine(_specialist_resolver), candidates)

    assert single.windows[0].selected_params == {"candidate": "specialist"}
    assert joint.windows[0].selected_params == {"candidate": "balanced"}
    assert [item.registry_index for item in joint.windows[0].training_score_ledger] == [0, 1]
    assert all(len(item.asset_reports) == 2 for item in joint.windows[0].training_score_ledger)


def test_oos_prices_cannot_change_training_selection() -> None:
    candidates = [{"candidate": "specialist"}, {"candidate": "balanced"}]

    def shocked(candidate: str, asset: str, timestamp: datetime) -> float:
        base = _specialist_resolver(candidate, asset, timestamp)
        return -0.20 if timestamp >= _TEST_START else base

    baseline = _run_joint(_PanelReportEngine(_specialist_resolver), candidates)
    changed_oos = _run_joint(_PanelReportEngine(shocked), candidates)

    assert baseline.windows[0].selected_params == changed_oos.windows[0].selected_params
    assert [
        item.annualized_sharpe for item in baseline.windows[0].training_score_ledger
    ] == pytest.approx(
        [item.annualized_sharpe for item in changed_oos.windows[0].training_score_ledger]
    )
    assert (
        baseline.pooled_oos.metrics.compounded_return_pct
        != changed_oos.pooled_oos.metrics.compounded_return_pct
    )


def test_terminal_exit_cost_is_required_and_applied_without_mutating_reports() -> None:
    engine = _PanelReportEngine(
        lambda _candidate, _asset, _timestamp: 0.001,
        terminal_assets=frozenset({"BTC"}),
    )
    policy = TerminalExitCostPolicy("conservative-test-v1", lambda _report: 2.0)

    with pytest.raises(ValueError, match=r"training.*without an exit-cost policy"):
        _run_joint(engine, [{"candidate": "only"}])
    with pytest.raises(ValueError, match=r"oos.*without an exit-cost policy"):
        _run_joint(
            engine,
            [{"candidate": "only"}],
            training_policy=policy,
        )

    result = _run_joint(
        engine,
        [{"candidate": "only"}],
        training_policy=policy,
        oos_policy=policy,
    )
    window = result.windows[0]
    training_adjustment = window.training_score_ledger[0].terminal_exit_costs[0]
    oos_adjustment = window.oos_terminal_exit_costs[0]
    raw_oos_report = window.oos_asset_reports[0].report

    assert training_adjustment.phase == "training"
    assert oos_adjustment.phase == "oos"
    assert oos_adjustment.policy_id == "conservative-test-v1"
    assert oos_adjustment.adjusted_equity == pytest.approx(oos_adjustment.unadjusted_equity - 2.0)
    assert raw_oos_report.equity_curve[-1][1] == oos_adjustment.unadjusted_equity
    assert window.equal_weight_oos_series.equity_curve[-1][1] < math.prod(1.001 for _ in range(5))


def test_joint_selector_rejects_misaligned_asset_coverage() -> None:
    btc = _source_bars("BTC")
    eth = _source_bars("ETH")
    eth.pop(40)
    with pytest.raises(ValueError, match="identical exact timestamp coverage"):
        run_joint_asset_timestamp_anchored_walk_forward(
            _strategy_factory,
            [{"candidate": "only"}],
            {"BTC": btc, "ETH": eth},
            {"BTC": _config("BTC"), "ETH": _config("ETH")},
            windows=[_window()],
            training_evaluation_start=_TRAINING_START,
            bootstrap_bars=5,
            engine=_PanelReportEngine(lambda _candidate, _asset, _timestamp: 0.0),
        )


def test_candidate_tie_uses_registry_order() -> None:
    def nearly_tied(candidate: str, _asset: str, timestamp: datetime) -> float:
        value = 0.008 if timestamp.toordinal() % 2 else -0.002
        if candidate == "challenger" and value > 0.0:
            return value + 1e-16
        return value

    result = _run_joint(
        _PanelReportEngine(nearly_tied),
        [{"candidate": "challenger"}, {"candidate": "baseline"}],
        baseline_registry_index=1,
    )

    assert result.windows[0].selected_registry_index == 1
    assert result.windows[0].selected_params == {"candidate": "baseline"}
    assert [entry.params for entry in result.windows[0].training_score_ledger] == [
        {"candidate": "challenger"},
        {"candidate": "baseline"},
    ]
    scores = [entry.annualized_sharpe for entry in result.windows[0].training_score_ledger]
    assert 0.0 <= scores[0] - scores[1] <= 1e-12
    assert result.selected_arm_tie_tolerance == 1e-12
    assert result.baseline_registry_index == 1


def test_nonbaseline_tie_uses_registered_order() -> None:
    def returns(candidate: str, _asset: str, timestamp: datetime) -> float:
        if candidate == "baseline":
            return -0.01 if timestamp.toordinal() % 2 else 0.001
        return 0.008 if timestamp.toordinal() % 2 else -0.002

    result = _run_joint(
        _PanelReportEngine(returns),
        [
            {"candidate": "first_challenger"},
            {"candidate": "second_challenger"},
            {"candidate": "baseline"},
        ],
        baseline_registry_index=2,
    )

    assert result.windows[0].selected_registry_index == 0
    assert result.windows[0].selected_params == {"candidate": "first_challenger"}


def test_preconsolidated_daily_panel_is_not_consolidated_again() -> None:
    result = _run_joint(
        _PanelReportEngine(lambda _candidate, _asset, _timestamp: 0.001),
        [{"candidate": "only"}],
    )

    assert [
        timestamp for timestamp, _ in result.windows[0].oos_asset_reports[0].report.equity_curve
    ] == [_TEST_START + timedelta(days=index) for index in range(5)]
    assert all(
        asset_report.report.config.consolidation_minutes == 1_440
        for asset_report in result.windows[0].oos_asset_reports
    )


def test_long_overlap_cannot_create_a_synthetic_sparse_training_stream() -> None:
    source_index = 16
    source_timestamp = _SOURCE_START + timedelta(days=source_index)
    bars = _source_bars("BTC")
    intervals = {
        index: EventInterval(
            observation_index=index,
            start=bar.timestamp + timedelta(days=1),
            end=bar.timestamp + timedelta(days=1),
        )
        for index, bar in enumerate(bars)
    }
    intervals[source_index] = EventInterval(
        observation_index=source_index,
        start=source_timestamp,
        end=_TEST_START + timedelta(days=1),
    )

    with pytest.raises(ValueError, match="execution indices are non-contiguous"):
        _run_joint(
            _PanelReportEngine(lambda _candidate, _asset, _timestamp: 0.001),
            [{"candidate": "only"}],
            event_intervals=intervals,
        )


def test_equal_weight_pooled_oos_series_matches_hand_returns() -> None:
    oos_returns = {
        "BTC": [0.10, -0.10, 0.04, 0.00, 0.02],
        "ETH": [0.00, 0.02, -0.02, 0.02, 0.00],
    }

    def resolver(_candidate: str, asset: str, timestamp: datetime) -> float:
        if timestamp < _TEST_START:
            return 0.001 if timestamp.toordinal() % 2 else -0.0005
        return oos_returns[asset][(timestamp - _TEST_START).days]

    result = _run_joint(
        _PanelReportEngine(resolver),
        [{"candidate": "only"}],
    )
    expected = [0.05, -0.04, 0.01, 0.01, 0.01]

    assert [observation.timestamp for observation in result.pooled_oos.observations] == [
        _TEST_START + timedelta(days=index) for index in range(5)
    ]
    assert [
        observation.daily_return for observation in result.pooled_oos.observations
    ] == pytest.approx(expected)
    assert result.pooled_oos.metrics.compounded_return_pct == pytest.approx(
        (math.prod(1.0 + value for value in expected) - 1.0) * 100.0
    )
    assert all(
        asset_report.report.consolidated_bars == 5
        for asset_report in result.windows[0].oos_asset_reports
    )
    persisted: Any = result.to_dict()
    assert persisted["pooled_oos"]["metrics"]["observations"] == 5
    assert len(persisted["windows"][0]["training_score_ledger"][0]["asset_reports"]) == 2


@pytest.mark.parametrize(
    ("failure_mode", "message"),
    [
        ("nonfinite", "finite"),
        ("ruin", "ruined the account"),
        ("unmatched", "unmatched signals"),
        ("divergence", "unresolved divergence"),
    ],
)
def test_candidate_report_failures_abort_selection(
    failure_mode: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _run_joint(
            _PanelReportEngine(
                lambda _candidate, _asset, _timestamp: 0.001,
                failure_mode=failure_mode,
                failure_asset="ETH",
            ),
            [{"candidate": "only"}],
        )


def test_candidate_execution_exception_aborts_selection() -> None:
    with pytest.raises(RuntimeError, match="registered candidate failed"):
        _run_joint(
            _PanelReportEngine(
                lambda _candidate, _asset, _timestamp: 0.001,
                failure_mode="raise",
            ),
            [{"candidate": "only"}],
        )


def test_daily_boundary_unit_input_maps_to_decision_close_window() -> None:
    fixture = json.loads(_BOUNDARY_FIXTURE.read_text())
    assert fixture["fixture_scope"] == "arithmetic_boundary_unit_input"
    assert fixture["market_evidence"] is False
    bars = bars_from_ohlcv(
        fixture["bars"],
        symbol="BTC-USDC",
        timeframe="1d",
        source=fixture["source"],
    )
    report = BacktestEngine().run_consolidated(
        _CandidateStrategy({"candidate": "boundary"}),
        bars,
        BacktestConfig(
            symbol="BTC-USDC",
            consolidation_minutes=1_440,
            fill_policy=FillPolicy.reference_v2(),
            evaluation_start=datetime(2020, 1, 2, tzinfo=UTC),
            evaluation_end=datetime(2026, 1, 2, tzinfo=UTC),
            require_flat_model_boundary=True,
        ),
    )

    assert len(bars) == 3
    assert report.bootstrap_bars == 1
    assert [timestamp for timestamp, _ in report.equity_curve] == [
        datetime(2020, 1, 2, tzinfo=UTC),
        datetime(2026, 1, 1, tzinfo=UTC),
    ]
    assert datetime(2020, 1, 1, tzinfo=UTC) not in {
        timestamp for timestamp, _ in report.equity_curve
    }
