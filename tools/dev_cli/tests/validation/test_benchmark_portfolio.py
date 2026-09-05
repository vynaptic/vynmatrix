"""Fixed-weight benchmark portfolio evidence over frozen Coinbase bars."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import pytest
from validation_helpers import bars_from_ohlcv, load_market_fixture

from dev_cli.validation.backtest.engine import BacktestConfig
from dev_cli.validation.backtest.execution import ExecutionCostModel, FillPolicy
from dev_cli.validation.backtest.simulator import ModelExecutionDivergence
from dev_cli.validation.backtest.walk_forward import TerminalExitCostPolicy
from dev_cli.validation.benchmarks import (
    BenchmarkDefinition,
    BenchmarkPortfolioComponent,
    BenchmarkPortfolioMode,
    aggregate_benchmark_portfolio,
    pool_benchmark_portfolio_folds,
    run_consolidated_benchmark,
)
from dev_cli.validation.persistence.backtest_manifest_store import canonical_manifest_bytes

_FIXTURES = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "market_data"


def _daily_unit_bars():  # type: ignore[no-untyped-def]
    boundary_fixture = json.loads(
        (_FIXTURES / "coinbase_btcusdc_daily_boundaries.json").read_text()
    )
    assert boundary_fixture["fixture_scope"] == "arithmetic_boundary_unit_input"
    assert boundary_fixture["market_evidence"] is False
    boundaries = boundary_fixture["bars"]
    current = load_market_fixture(_FIXTURES / "coinbase_btcusdc_1d_2026-06-10.json")["bar"]
    rows = [
        *boundaries,
        {
            "ts": int(current["start"]),
            **{
                field: float(current[field]) for field in ("open", "high", "low", "close", "volume")
            },
        },
    ]
    return bars_from_ohlcv(
        rows,
        symbol="BTCUSDC",
        timeframe="1d",
        source="coinbase_advanced_trade_public",
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        symbol="BTCUSDC",
        consolidation_minutes=0,
        fee_bps=0.0,
        execution_costs=ExecutionCostModel(
            scenario_name="zero-cost-portfolio-test",
            commission_bps=0.0,
            historical_basis="backward-applied-scenario",
        ),
        fill_policy=FillPolicy.reference_v2(),
    )


def _runs(bars=None):  # type: ignore[no-untyped-def]
    selected_bars = list(bars or _daily_unit_bars())
    config = _config()
    cash = run_consolidated_benchmark(
        BenchmarkDefinition.cash(),
        selected_bars,
        config,
    )
    buy_and_hold = run_consolidated_benchmark(
        BenchmarkDefinition.buy_and_hold(),
        selected_bars,
        config,
    )
    trend = run_consolidated_benchmark(
        BenchmarkDefinition.sma_long_cash(period=2),
        selected_bars,
        config,
    )
    return cash, buy_and_hold, trend


def _components(cash, buy_and_hold, trend):  # type: ignore[no-untyped-def]
    return (
        BenchmarkPortfolioComponent("cash", "BTCUSDC", 0.20, cash),
        BenchmarkPortfolioComponent("buy_and_hold", "BTCUSDC", 0.45, buy_and_hold),
        # Exercise direct BacktestReport input as well as BenchmarkRun input.
        BenchmarkPortfolioComponent("sma_trend", "BTCUSDC", 0.35, trend.report),
    )


def _expected_timestamps(run) -> tuple:  # type: ignore[no-untyped-def]
    return tuple(timestamp for timestamp, _equity in run.report.equity_curve)


def _component_returns(report) -> list[float]:  # type: ignore[no-untyped-def]
    previous = report.config.initial_capital
    returns: list[float] = []
    for _timestamp, equity in report.equity_curve:
        returns.append(equity / previous - 1.0)
        previous = equity
    return returns


def test_cash_buy_hold_and_trend_combine_at_exact_fixed_weights() -> None:
    cash, buy_and_hold, trend = _runs()
    components = _components(cash, buy_and_hold, trend)
    timestamps = _expected_timestamps(cash)

    result = aggregate_benchmark_portfolio(
        "btc-benchmark-mix-v1",
        components,
        expected_timestamps=timestamps,
        annualization_factor=365.0,
        mode=BenchmarkPortfolioMode.FULL_PANEL,
    )
    expected_returns = [
        math.fsum((0.20 * cash_return, 0.45 * hold_return, 0.35 * trend_return))
        for cash_return, hold_return, trend_return in zip(
            _component_returns(cash.report),
            _component_returns(buy_and_hold.report),
            _component_returns(trend.report),
            strict=True,
        )
    ]
    expected_growth = []
    growth = 1.0
    for daily_return in expected_returns:
        growth *= 1.0 + daily_return
        expected_growth.append(growth)

    assert result.daily_returns == pytest.approx(expected_returns)
    assert [equity for _timestamp, equity in result.equity_curve] == pytest.approx(expected_growth)
    assert result.components[0].source is cash
    assert result.components[1].source is buy_and_hold
    assert result.components[2].source is trend.report
    assert result.terminal_exit_costs == ()
    assert result.terminal_treatment == "mark_to_market_unliquidated"
    assert result.metrics.initial_capital == 1.0
    assert result.metrics.final_equity == pytest.approx(expected_growth[-1])


def test_oos_terminal_charge_is_auditable_and_does_not_mutate_report() -> None:
    _cash, buy_and_hold, _trend = _runs()
    component = BenchmarkPortfolioComponent(
        "buy_and_hold",
        "BTCUSDC",
        1.0,
        buy_and_hold,
    )
    timestamps = _expected_timestamps(buy_and_hold)
    original_curve = list(buy_and_hold.report.equity_curve)
    policy = TerminalExitCostPolicy("stressed-fixed-exit-v1", lambda _report: 250.0)

    with pytest.raises(ValueError, match="terminal exposure without an exit-cost policy"):
        aggregate_benchmark_portfolio(
            "btc-hold-oos-v1",
            [component],
            expected_timestamps=timestamps,
            annualization_factor=365.0,
            mode=BenchmarkPortfolioMode.OOS_FOLD,
            fold_index=0,
        )

    result = aggregate_benchmark_portfolio(
        "btc-hold-oos-v1",
        [component],
        expected_timestamps=timestamps,
        annualization_factor=365.0,
        mode=BenchmarkPortfolioMode.OOS_FOLD,
        fold_index=0,
        terminal_exit_cost_policy=policy,
    )
    adjustment = result.terminal_exit_costs[0]

    assert adjustment.component_id == "buy_and_hold"
    assert adjustment.asset == "BTCUSDC"
    assert adjustment.phase == "oos"
    assert adjustment.amount == 250.0
    assert adjustment.adjusted_equity == pytest.approx(adjustment.unadjusted_equity - 250.0)
    assert result.equity_curve[-1][1] == pytest.approx(
        (original_curve[-1][1] - 250.0) / buy_and_hold.report.config.initial_capital
    )
    assert buy_and_hold.report.equity_curve == original_curve


def test_alignment_weight_and_symbol_mismatches_fail_closed() -> None:
    cash, buy_and_hold, trend = _runs()
    timestamps = _expected_timestamps(cash)
    missing_report = replace(
        trend.report,
        equity_curve=trend.report.equity_curve[:-1],
    )
    misaligned = (
        BenchmarkPortfolioComponent("cash", "BTCUSDC", 0.5, cash),
        BenchmarkPortfolioComponent("trend", "BTCUSDC", 0.5, missing_report),
    )

    with pytest.raises(ValueError, match="missing or unexpected observations"):
        aggregate_benchmark_portfolio(
            "misaligned-v1",
            misaligned,
            expected_timestamps=timestamps,
            annualization_factor=365.0,
            mode=BenchmarkPortfolioMode.FULL_PANEL,
        )
    with pytest.raises(ValueError, match=r"weights must sum to 1\.0"):
        aggregate_benchmark_portfolio(
            "bad-weights-v1",
            (
                BenchmarkPortfolioComponent("cash", "BTCUSDC", 0.4, cash),
                BenchmarkPortfolioComponent("hold", "BTCUSDC", 0.5, buy_and_hold),
            ),
            expected_timestamps=timestamps,
            annualization_factor=365.0,
            mode=BenchmarkPortfolioMode.FULL_PANEL,
        )
    with pytest.raises(ValueError, match="expected symbol ETHUSDC"):
        BenchmarkPortfolioComponent("cash", "ETHUSDC", 1.0, cash)


def test_aligned_registered_calendar_gap_is_not_filled_or_rejected() -> None:
    cash, _buy_and_hold, trend = _runs()
    retained_curve = tuple(
        observation for index, observation in enumerate(cash.report.equity_curve) if index != 1
    )
    expected_timestamps = tuple(timestamp for timestamp, _equity in retained_curve)
    components = (
        BenchmarkPortfolioComponent(
            "cash",
            "BTCUSDC",
            0.5,
            replace(cash.report, equity_curve=retained_curve),
        ),
        BenchmarkPortfolioComponent(
            "trend",
            "BTCUSDC",
            0.5,
            replace(
                trend.report,
                equity_curve=tuple(
                    observation
                    for index, observation in enumerate(trend.report.equity_curve)
                    if index != 1
                ),
            ),
        ),
    )

    result = aggregate_benchmark_portfolio(
        "registered-gap-v1",
        components,
        expected_timestamps=expected_timestamps,
        annualization_factor=365.0,
        mode=BenchmarkPortfolioMode.FULL_PANEL,
    )
    assert result.expected_timestamps == expected_timestamps

    mismatched = (
        components[0],
        BenchmarkPortfolioComponent(
            "trend-missing",
            "BTCUSDC",
            0.5,
            replace(
                trend.report,
                equity_curve=tuple(
                    observation
                    for index, observation in enumerate(trend.report.equity_curve)
                    if index not in {1, 2}
                ),
            ),
        ),
    )
    with pytest.raises(ValueError, match="missing or unexpected observations"):
        aggregate_benchmark_portfolio(
            "registered-gap-mismatch-v1",
            mismatched,
            expected_timestamps=expected_timestamps,
            annualization_factor=365.0,
            mode=BenchmarkPortfolioMode.FULL_PANEL,
        )

    unexpected_excluded = (
        components[0],
        BenchmarkPortfolioComponent(
            "trend-extra",
            "BTCUSDC",
            0.5,
            trend.report,
        ),
    )
    with pytest.raises(ValueError, match="missing or unexpected observations"):
        aggregate_benchmark_portfolio(
            "registered-gap-extra-v1",
            unexpected_excluded,
            expected_timestamps=expected_timestamps,
            annualization_factor=365.0,
            mode=BenchmarkPortfolioMode.FULL_PANEL,
        )


@pytest.mark.parametrize(
    ("report_update", "message"),
    [
        ({"account_ruined": True}, "ruined the account"),
        ({"unmatched_signals": 1}, "has unmatched signals"),
        (
            {
                "model_execution_divergences": (
                    ModelExecutionDivergence(
                        start_ts=_daily_unit_bars()[0].timestamp,
                        end_ts=None,
                        model_position=1,
                        execution_position=0,
                        cause="test_unresolved",
                    ),
                )
            },
            "has unresolved divergence",
        ),
        (
            {
                "model_execution_divergences": (
                    ModelExecutionDivergence(
                        start_ts=_daily_unit_bars()[0].timestamp,
                        end_ts=_daily_unit_bars()[1].timestamp,
                        model_position=1,
                        execution_position=1,
                        cause="position_lineage_mismatch",
                    ),
                )
            },
            "has position lineage divergence",
        ),
    ],
)
def test_invalid_component_report_states_fail_closed(
    report_update: dict[str, object],
    message: str,
) -> None:
    cash, _buy_and_hold, _trend = _runs()
    report = replace(cash.report, **report_update)  # type: ignore[arg-type]
    component = BenchmarkPortfolioComponent("cash", "BTCUSDC", 1.0, report)

    with pytest.raises(ValueError, match=message):
        aggregate_benchmark_portfolio(
            "invalid-report-v1",
            [component],
            expected_timestamps=_expected_timestamps(cash),
            annualization_factor=365.0,
            mode=BenchmarkPortfolioMode.FULL_PANEL,
        )


def test_nonfinite_component_equity_fails_closed() -> None:
    cash, _buy_and_hold, _trend = _runs()
    nonfinite = replace(
        cash.report,
        equity_curve=[*cash.report.equity_curve[:-1], (cash.report.equity_curve[-1][0], math.nan)],
    )

    with pytest.raises(ValueError, match="nonfinite or ruined equity"):
        aggregate_benchmark_portfolio(
            "nonfinite-report-v1",
            [BenchmarkPortfolioComponent("cash", "BTCUSDC", 1.0, nonfinite)],
            expected_timestamps=_expected_timestamps(cash),
            annualization_factor=365.0,
            mode=BenchmarkPortfolioMode.FULL_PANEL,
        )


def test_terminal_symbol_mismatch_fails_even_for_unliquidated_full_panel() -> None:
    _cash, buy_and_hold, _trend = _runs()
    assert buy_and_hold.report.terminal_position is not None
    mismatched_terminal = replace(
        buy_and_hold.report.terminal_position,
        symbol="ETHUSDC",
    )
    mismatched_report = replace(
        buy_and_hold.report,
        terminal_position=mismatched_terminal,
    )

    with pytest.raises(ValueError, match="terminal position symbol does not match"):
        aggregate_benchmark_portfolio(
            "terminal-symbol-mismatch-v1",
            [BenchmarkPortfolioComponent("hold", "BTCUSDC", 1.0, mismatched_report)],
            expected_timestamps=_expected_timestamps(buy_and_hold),
            annualization_factor=365.0,
            mode=BenchmarkPortfolioMode.FULL_PANEL,
        )


def test_oos_fold_portfolios_pool_chronologically_and_deterministically() -> None:
    bars = _daily_unit_bars()
    first_cash, first_hold, _first_trend = _runs(bars[:2])
    second_cash, second_hold, _second_trend = _runs(bars[2:])
    policy = TerminalExitCostPolicy("stressed-fixed-exit-v1", lambda _report: 25.0)

    def fold(index, cash, hold):  # type: ignore[no-untyped-def]
        return aggregate_benchmark_portfolio(
            "btc-cash-hold-v1",
            (
                BenchmarkPortfolioComponent("cash", "BTCUSDC", 0.5, cash),
                BenchmarkPortfolioComponent("hold", "BTCUSDC", 0.5, hold),
            ),
            expected_timestamps=_expected_timestamps(cash),
            annualization_factor=365.0,
            mode=BenchmarkPortfolioMode.OOS_FOLD,
            fold_index=index,
            terminal_exit_cost_policy=policy,
        )

    first = fold(0, first_cash, first_hold)
    second = fold(1, second_cash, second_hold)
    pooled = pool_benchmark_portfolio_folds(
        [first, second],
        annualization_factor=365.0,
    )
    replay = pool_benchmark_portfolio_folds(
        [first, second],
        annualization_factor=365.0,
    )

    assert [item.fold_index for item in pooled.observations] == [0, 0, 1, 1]
    assert [item.daily_return for item in pooled.observations] == pytest.approx(
        [*first.daily_returns, *second.daily_returns]
    )
    assert pooled.to_dict() == replay.to_dict()
    json.dumps(first.to_dict(), sort_keys=True, allow_nan=False)
    canonical_manifest_bytes({"payload": first.to_dict()})
    canonical_manifest_bytes({"payload": pooled.to_dict()})
    assert json.dumps(first.to_dict(), sort_keys=True, allow_nan=False) == json.dumps(
        first.to_dict(),
        sort_keys=True,
        allow_nan=False,
    )

    overlapping_second = replace(
        second,
        expected_timestamps=first.expected_timestamps,
        equity_curve=first.equity_curve,
    )
    with pytest.raises(ValueError, match="duplicate, overlapping"):
        pool_benchmark_portfolio_folds(
            [first, overlapping_second],
            annualization_factor=365.0,
        )

    changed_weight = replace(
        second.components[0],
        weight=0.4,
    )
    inconsistent_second = replace(
        second,
        components=(changed_weight, second.components[1]),
    )
    with pytest.raises(ValueError, match="weights or components differ"):
        pool_benchmark_portfolio_folds(
            [first, inconsistent_second],
            annualization_factor=365.0,
        )
