"""Focused contracts for equity portfolio diagnostics."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from dev_cli.validation.backtest.equity_diagnostics import (
    AccountingView,
    AttributionEvidence,
    BenchmarkEvidence,
    BenchmarkRole,
    BenchmarkSourceKind,
    BootstrapProtocol,
    CapacityProtocol,
    CSCVProtocol,
    EquityDiagnosticsEvidence,
    FactorPnlContribution,
    LiquidityCapacityObservation,
    PerformanceSeriesEvidence,
    PortfolioPnlAttribution,
    RollingWindow,
    SharpeTrialEvidence,
    StrategyPerformanceEvidence,
    build_equity_diagnostics,
    complete_diagnostics_payload,
    strategy_performance_evidence,
    unavailable_diagnostics_payload,
)
from dev_cli.validation.backtest.equity_portfolio import (
    BacktestResult,
    DailyPortfolioExposure,
    Trade,
)
from dev_cli.validation.backtest.equity_portfolio_types import EQUITY_TURNOVER_POLICY_ID
from dev_cli.validation.contribution_diagnostics import TradeContribution
from dev_cli.validation.cscv import (
    CandidateDailyReturnSeries,
    TimestampedDailyReturn,
)

_INITIAL_CAPITAL = 1_000.0
_CURVE_TIMESTAMPS = tuple(datetime(2020, month, 20, 21) for month in range(1, 13))


def _series(
    series_id: str,
    label: str,
    accounting: AccountingView,
    returns: tuple[float, ...],
) -> PerformanceSeriesEvidence:
    values = [_INITIAL_CAPITAL]
    for daily_return in returns:
        values.append(values[-1] * (1.0 + daily_return))
    assert len(values) == len(_CURVE_TIMESTAMPS)
    return PerformanceSeriesEvidence(
        series_id=series_id,
        label=label,
        accounting=accounting,
        equity_curve=tuple(zip(_CURVE_TIMESTAMPS, values, strict=True)),
        initial_capital=_INITIAL_CAPITAL,
        exposure_seconds=(_CURVE_TIMESTAMPS[-1] - _CURVE_TIMESTAMPS[0]).total_seconds(),
    )


def _strategy() -> StrategyPerformanceEvidence:
    net_returns = (
        0.012,
        -0.009,
        0.017,
        0.004,
        -0.006,
        0.014,
        0.011,
        -0.004,
        0.013,
        0.006,
        0.009,
    )
    gross_returns = tuple(value + 0.001 for value in net_returns)
    return StrategyPerformanceEvidence(
        gross=_series(
            "strategy_gross",
            "Strategy gross",
            AccountingView.GROSS,
            gross_returns,
        ),
        net=_series(
            "strategy_net",
            "Strategy net",
            AccountingView.NET,
            net_returns,
        ),
    )


def _benchmark(
    *,
    benchmark_id: str,
    label: str,
    role: BenchmarkRole,
    source_kind: BenchmarkSourceKind,
    returns: tuple[float, ...],
) -> BenchmarkEvidence:
    gross_returns = tuple(value + 0.0005 for value in returns)
    return BenchmarkEvidence(
        benchmark_id=benchmark_id,
        label=label,
        role=role,
        source_kind=source_kind,
        gross=_series(
            f"{benchmark_id}_gross",
            f"{label} gross",
            AccountingView.GROSS,
            gross_returns,
        ),
        net=_series(
            f"{benchmark_id}_net",
            f"{label} net",
            AccountingView.NET,
            returns,
        ),
        construction="content-addressed total-return curve",
        cost_policy="registered benchmark execution costs v1",
        adjustment_policy="split and dividend adjusted",
        source_observation_ids=(f"{benchmark_id}-source",),
    )


def _cscv_protocol() -> CSCVProtocol:
    registered_dates = tuple(
        day for year in range(2020, 2026) for day in (date(year, 1, 15), date(year, 7, 15))
    )

    def candidate(
        candidate_id: str,
        scale: float,
    ) -> CandidateDailyReturnSeries:
        observations = tuple(
            TimestampedDailyReturn(
                timestamp=datetime.combine(
                    day,
                    datetime.min.time(),
                    tzinfo=UTC,
                ),
                daily_return=scale * (0.001 + ((index % 4) - 1.5) * 0.0002),
            )
            for index, day in enumerate(registered_dates)
        )
        return CandidateDailyReturnSeries(
            candidate_id=candidate_id,
            observations=observations,
        )

    return CSCVProtocol(
        candidate_series=(
            candidate("baseline", 1.0),
            candidate("neighbor", -0.7),
        ),
        registered_candidate_order=("baseline", "neighbor"),
        calendar_years=tuple(range(2020, 2026)),
        registered_calendar_dates=registered_dates,
        annualization_factor=252.0,
    )


def _attribution(
    strategy: StrategyPerformanceEvidence,
) -> AttributionEvidence:
    net_pnl = strategy.net.equity_curve[-1][1] - strategy.net.initial_capital
    first = net_pnl * 0.4
    second = net_pnl - first

    def observation(
        contribution_id: str,
        timestamp: datetime,
        contribution: float,
        *,
        asset: str,
        sector: str,
        regime: str,
    ) -> PortfolioPnlAttribution:
        momentum = contribution * 0.6
        return PortfolioPnlAttribution(
            contribution_id=contribution_id,
            asset=asset,
            timestamp=timestamp,
            net_contribution=contribution,
            sector=sector,
            regime=regime,
            factors=(
                FactorPnlContribution("price_momentum", momentum),
                FactorPnlContribution("quality", contribution - momentum),
            ),
            source_observation_id=f"{contribution_id}-source",
        )

    first_ts = datetime(2020, 6, 30, 20, tzinfo=UTC)
    second_ts = datetime(2020, 12, 31, 21, tzinfo=UTC)
    return AttributionEvidence(
        observations=(
            observation(
                "pnl-1",
                first_ts,
                first,
                asset="AAA",
                sector="technology",
                regime="expansion",
            ),
            observation(
                "pnl-2",
                second_ts,
                second,
                asset="BBB",
                sector="health_care",
                regime="contraction",
            ),
        ),
        closed_trade_contributions=(
            TradeContribution(
                trade_id="trade-1",
                asset="AAA",
                exit_ts=first_ts,
                net_contribution=first,
            ),
            TradeContribution(
                trade_id="trade-2",
                asset="BBB",
                exit_ts=second_ts,
                net_contribution=second,
            ),
        ),
        methodology_id="daily_additive_net_pnl_factor_attribution_v1",
        reconciliation_tolerance=1e-8,
    )


def _capacity() -> CapacityProtocol:
    decision_at = datetime(2020, 6, 30, 20, tzinfo=UTC)
    return CapacityProtocol(
        aum_levels=(1_000_000.0, 10_000_000.0, 100_000_000.0),
        maximum_participation=0.1,
        observations=(
            LiquidityCapacityObservation(
                order_id="order-1",
                symbol="AAA",
                decision_at=decision_at,
                available_at=datetime(2020, 6, 30, 19, tzinfo=UTC),
                absolute_weight_change=0.02,
                point_in_time_dollar_adv=50_000_000.0,
                execution_sessions=1,
                adv_window_sessions=20,
                source_observation_id="adv-1",
            ),
            LiquidityCapacityObservation(
                order_id="order-2",
                symbol="BBB",
                decision_at=decision_at,
                available_at=datetime(2020, 6, 30, 19, tzinfo=UTC),
                absolute_weight_change=0.04,
                point_in_time_dollar_adv=100_000_000.0,
                execution_sessions=1,
                adv_window_sessions=20,
                source_observation_id="adv-2",
            ),
        ),
    )


def _evidence(
    strategy: StrategyPerformanceEvidence,
) -> EquityDiagnosticsEvidence:
    spy_returns = (
        0.009,
        -0.018,
        0.012,
        0.003,
        -0.011,
        0.009,
        0.008,
        -0.007,
        0.011,
        0.004,
        0.006,
    )
    equal_weight_returns = (
        0.008,
        -0.022,
        0.013,
        0.002,
        -0.014,
        0.011,
        0.007,
        -0.009,
        0.009,
        0.003,
        0.005,
    )
    return EquityDiagnosticsEvidence(
        annualization_factor=252.0,
        spy=_benchmark(
            benchmark_id="spy",
            label="SPY total return",
            role=BenchmarkRole.SPY_TOTAL_RETURN,
            source_kind=BenchmarkSourceKind.SPY_ADJUSTED_SECURITY,
            returns=spy_returns,
        ),
        equal_weight=_benchmark(
            benchmark_id="equal_weight",
            label="Reconstructed S&P 500 equal weight",
            role=BenchmarkRole.SP500_EQUAL_WEIGHT,
            source_kind=(BenchmarkSourceKind.RECONSTRUCTED_EQUAL_WEIGHT_PORTFOLIO_APPROXIMATION),
            returns=equal_weight_returns,
        ),
        rolling_windows=(RollingWindow("three_observations", 3),),
        bootstrap=BootstrapProtocol(
            block_size=2,
            samples=100,
            confidence=0.9,
            seed=20260727,
        ),
        sharpe_trials=SharpeTrialEvidence(
            effective_trials=3.0,
            trial_sharpe_std=0.5,
            trial_sharpe_std_source="frozen-trial-ledger-sha256",
            benchmark_sharpe=0.0,
            selection_contaminated=True,
            max_autocorrelation_lag=2,
        ),
        cscv=_cscv_protocol(),
        attribution=_attribution(strategy),
        capacity=_capacity(),
    )


def test_complete_diagnostics_reuse_every_registered_shared_primitive() -> None:
    strategy = _strategy()
    result = build_equity_diagnostics(strategy, _evidence(strategy))

    assert len(result.series) == 6
    assert all(item.monthly_returns for item in result.series)
    assert all(item.annual_returns for item in result.series)
    assert all(item.rolling_returns for item in result.series)
    assert len(result.comparisons) == 4
    assert all(item.aligned_daily_observations == 11 for item in result.comparisons)
    assert any(item.downside_capture_pct is not None for item in result.comparisons)
    assert result.benchmarks[1].is_approximation is True
    assert result.benchmarks[1].display_label.endswith("(approximation)")
    assert result.return_uncertainty.annualized_net_return_pct.seed == 20260727
    assert result.sharpe_diagnostics.effective_trials == 3.0
    assert result.cscv_pbo.registered_candidate_order == (
        "baseline",
        "neighbor",
    )
    assert len(result.cscv_pbo.splits) == 20
    dimensions = {item.dimension for item in result.attribution.buckets}
    assert dimensions == {
        "calendar_month",
        "calendar_year",
        "factor",
        "regime",
        "sector",
    }
    assert result.attribution.contribution_robustness.closed_trades == 2
    assert result.capacity.maximum_supported_aum == pytest.approx(250_000_000.0)
    assert all(point.feasible for point in result.capacity.curve)
    payload = complete_diagnostics_payload(result)
    assert payload["available"] is True
    assert payload["comparison_eligible"] is True
    assert payload["headline_eligible"] is None
    assert payload["headline_eligibility_evaluated"] is False


def test_bootstrap_is_deterministic_for_the_registered_seed() -> None:
    strategy = _strategy()
    evidence = _evidence(strategy)

    first = build_equity_diagnostics(strategy, evidence)
    second = build_equity_diagnostics(strategy, evidence)

    assert first.return_uncertainty == second.return_uncertainty


def test_capacity_rejects_liquidity_not_available_at_decision_time() -> None:
    with pytest.raises(
        ValueError,
        match="unavailable at decision time",
    ):
        LiquidityCapacityObservation(
            order_id="order",
            symbol="AAA",
            decision_at=datetime(2020, 1, 1, tzinfo=UTC),
            available_at=datetime(2020, 1, 2, tzinfo=UTC),
            absolute_weight_change=0.02,
            point_in_time_dollar_adv=1_000_000.0,
            execution_sessions=1,
            adv_window_sessions=20,
            source_observation_id="adv",
        )


def test_attribution_must_reconcile_to_net_portfolio_pnl() -> None:
    strategy = _strategy()
    evidence = _evidence(strategy)
    broken_observation = PortfolioPnlAttribution(
        contribution_id="broken",
        asset="AAA",
        timestamp=datetime(2020, 12, 31, tzinfo=UTC),
        net_contribution=1.0,
        sector="technology",
        regime="expansion",
        factors=(FactorPnlContribution("quality", 1.0),),
        source_observation_id="broken-source",
    )
    broken = EquityDiagnosticsEvidence(
        annualization_factor=evidence.annualization_factor,
        spy=evidence.spy,
        equal_weight=evidence.equal_weight,
        rolling_windows=evidence.rolling_windows,
        bootstrap=evidence.bootstrap,
        sharpe_trials=evidence.sharpe_trials,
        cscv=evidence.cscv,
        attribution=AttributionEvidence(
            observations=(broken_observation,),
            closed_trade_contributions=(
                TradeContribution(
                    trade_id="broken-trade",
                    asset="AAA",
                    exit_ts=datetime(2020, 12, 31, tzinfo=UTC),
                    net_contribution=1.0,
                ),
            ),
            methodology_id="method",
            reconciliation_tolerance=0.0,
        ),
        capacity=evidence.capacity,
    )

    with pytest.raises(
        ValueError,
        match="does not reconcile to net portfolio P&L",
    ):
        build_equity_diagnostics(strategy, broken)


def test_equal_weight_cannot_be_silently_labelled_as_spy() -> None:
    returns = (0.001,) * 11
    with pytest.raises(
        ValueError,
        match="equal-weight role cannot use SPY",
    ):
        _benchmark(
            benchmark_id="bad",
            label="Bad equal weight",
            role=BenchmarkRole.SP500_EQUAL_WEIGHT,
            source_kind=BenchmarkSourceKind.SPY_ADJUSTED_SECURITY,
            returns=returns,
        )


def test_result_adapter_preserves_gross_and_net_ledgers() -> None:
    curve_dates = (
        datetime(2020, 1, 2, 21),
        datetime(2020, 1, 3, 21),
    )
    trade = Trade(
        symbol="AAA",
        entry_session=date(2020, 1, 2),
        entry_fill=101.0,
        shares=2.0,
        entry_reference=100.0,
        exit_session=date(2020, 1, 3),
        exit_fill=109.0,
        exit_reference=110.0,
    )
    result = BacktestResult(
        equity_curve=[
            (curve_dates[0], 1_000.0),
            (curve_dates[1], 1_016.0),
        ],
        gross_equity_curve=[
            (curve_dates[0], 1_000.0),
            (curve_dates[1], 1_020.0),
        ],
        trades=[trade],
        cost_ledger=[
            {
                "execution_price": 100.0,
                "quantity": 2.0,
                "turnover_eligible": True,
            },
            {
                "execution_price": 110.0,
                "quantity": 2.0,
                "turnover_eligible": True,
            },
        ],
        corporate_action_ledger=[],
        terminal_disposition_ledger=[],
        rebalance_log=[],
        skipped_entries=0,
        daily_exposures=[
            DailyPortfolioExposure(
                timestamp=curve_dates[0],
                gross_invested_notional=200.0,
                net_invested_notional=200.0,
                gross_exposure_ratio=0.2,
                net_exposure_ratio=0.2,
            ),
            DailyPortfolioExposure(
                timestamp=curve_dates[1],
                gross_invested_notional=0.0,
                net_invested_notional=0.0,
                gross_exposure_ratio=0.0,
                net_exposure_ratio=0.0,
            ),
        ],
        policies={"turnover": EQUITY_TURNOVER_POLICY_ID},
    )

    evidence = strategy_performance_evidence(
        result,
        initial_capital=1_000.0,
    )

    assert evidence.net.trade_pnls == (16.0,)
    assert evidence.gross.trade_pnls == (20.0,)
    assert evidence.net.gross_trade_pnls == (20.0,)
    assert evidence.net.turnover_notional == 420.0


def test_absent_evidence_is_explicitly_headline_ineligible() -> None:
    payload = unavailable_diagnostics_payload("registered evidence not supplied")

    assert payload == {
        "available": False,
        "comparison_eligible": False,
        "headline_eligible": False,
        "headline_eligibility_evaluated": True,
        "reason": "registered evidence not supplied",
    }
