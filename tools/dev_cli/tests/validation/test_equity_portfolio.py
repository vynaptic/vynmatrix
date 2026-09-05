"""Portfolio simulator tests: PIT universe, feeder contract, fills, accounting."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

# The production core, loaded the production way.
from pathlib import Path

import pytest

from dev_cli.validation.backtest.equity_portfolio import (
    BacktestResult,
    CostConfig,
    DailyBar,
    EquityCorporateAction,
    EquityCorporateActionKind,
    EquityDataset,
    EquityPortfolioBacktest,
    EquityPortfolioError,
    EquitySessionPanel,
    EquityTerminalDisposition,
    EquityTerminalDispositionKind,
    ExplicitRawCorporateActionPolicy,
    InstitutionalEquityExecutionCostPolicy,
    PanelExecutionUniversePolicy,
    PortfolioConfig,
    StaticExecutionUniverseCadencePolicy,
    SynchronizedStrategyPanelContextPolicy,
    UniverseSelection,
    _finra_taf_bps,
    _section31_bps,
    earnings_distances,
    is_member_asof,
    median_dollar_adv,
    rebalance_sessions,
    select_universe,
    vix_percentiles,
    worst_trades,
)
from dev_cli.validation.backtest.execution import ExecutionCostModel
from lib_strategy.signals.loading import load_pure_strategy_core
from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy
from lib_strategy.signals.signal import SignalAction

_CORE_DIR = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "fixtures"
    / "strategies"
    / "EquityBasketExerciser"
)


def _sessions(count: int, start: date = date(2019, 1, 2)) -> list[date]:
    day = start
    out: list[date] = []
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _bars_from_closes(sessions: list[date], closes: list[float], volume: float) -> list[DailyBar]:
    return [
        DailyBar(
            session=s,
            open=c * 0.999,
            high=c * 1.006,
            low=c * 0.994,
            close=c,
            raw_open=c * 0.999,
            raw_high=c * 1.006,
            raw_low=c * 0.994,
            raw_close=c,
            split_adjusted_open=c * 0.999,
            split_adjusted_high=c * 1.006,
            split_adjusted_low=c * 0.994,
            split_adjusted_close=c,
            split_adjusted_volume=volume,
            split_adjustment_factor=1.0,
        )
        for s, c in zip(sessions, closes, strict=True)
    ]


def _drifting_closes(
    count: int, *, seed: int, overrides: dict[int, float] | None = None
) -> list[float]:
    rng = random.Random(seed)
    level = 100.0
    out: list[float] = []
    for i in range(count):
        ret = (overrides or {}).get(i, 0.001 + rng.gauss(0.0, 0.004))
        level *= math.exp(ret)
        out.append(level)
    return out


def _full_membership(symbols: list[str]) -> dict[str, list[tuple[date, date | None]]]:
    return {s: [(date(2018, 1, 1), None)] for s in symbols}


def _make_dataset(
    count: int,
    stock_overrides: dict[str, dict[int, float]],
    *,
    volumes: dict[str, float] | None = None,
) -> EquityDataset:
    sessions = _sessions(count)
    index_closes = [100.0 * math.exp(0.001 * (i + 1)) for i in range(count)]
    bars = {}
    for k, (symbol, overrides) in enumerate(sorted(stock_overrides.items())):
        closes = _drifting_closes(count, seed=11 + k, overrides=overrides)
        bars[symbol] = _bars_from_closes(sessions, closes, (volumes or {}).get(symbol, 1e9))
    vix_days = _sessions(count + 800, start=date(2016, 1, 4))
    vix = {d: 15.0 + (i % 10) for i, d in enumerate(vix_days)}
    return EquityDataset(
        sessions=sessions,
        bars=bars,
        benchmark_price=dict(zip(sessions, index_closes, strict=True)),
        benchmark_total_return=dict(zip(sessions, index_closes, strict=True)),
        vix=vix,
        earnings={s: [] for s in bars},
        membership=_full_membership(list(bars)),
        benchmark_total_return_kind="adjusted_security",
        benchmark_total_return_symbol="SPY",
    )


def _core_factory() -> PureSignalStrategy:
    cls = load_pure_strategy_core(_CORE_DIR)
    return cls(
        config={
            "strategy_version": "1.0.0",
            "basket_size": "2",
            "reentry_gap_sessions": "30",
        }
    )


DIP_START = 300


def _dip(start: int, days: int = 3, ret: float = -0.02) -> dict[int, float]:
    return {start + i: ret for i in range(days)}


# ---------------------------------------------------------------------------
# universe construction
# ---------------------------------------------------------------------------


def test_median_dollar_adv_requires_enough_observations() -> None:
    sessions = _sessions(50)
    bars = _bars_from_closes(sessions, [100.0] * 50, volume=1e6)
    adv, observations = median_dollar_adv(bars, before=sessions[-1] + timedelta(days=1), window=126)
    assert adv is None
    assert observations == 50


def test_select_universe_ranks_by_adv_and_respects_membership() -> None:
    dataset = _make_dataset(
        200,
        {"AAA": {}, "BBB": {}, "CCC": {}},
        volumes={"AAA": 3e9, "BBB": 2e9, "CCC": 1e9},
    )
    membership = dict(dataset.membership)
    membership["AAA"] = [(date(2018, 1, 1), date(2019, 6, 1))]  # expired member
    dataset = EquityDataset(**{**dataset.__dict__, "membership": membership})
    config = PortfolioConfig(
        eval_start=dataset.sessions[150], eval_end=dataset.sessions[-1], universe_size=2
    )
    chosen, margin = select_universe(dataset, asof=dataset.sessions[190], config=config)
    assert chosen == ["BBB", "CCC"]  # AAA excluded: not a member as-of
    assert "asof" in margin


def test_is_member_asof_interval_logic() -> None:
    membership = {"AAA": [(date(2020, 1, 1), date(2020, 6, 30))]}
    assert is_member_asof(membership, "AAA", date(2020, 3, 1)) is True
    assert is_member_asof(membership, "AAA", date(2020, 7, 1)) is False
    assert is_member_asof(membership, "ZZZ", date(2020, 3, 1)) is False


def test_rebalance_sessions_first_of_quarter_months() -> None:
    sessions = _sessions(600)
    marks = rebalance_sessions(sessions, sessions[0], sessions[-1])
    months = {(m.year, m.month) for m in marks}
    assert all(m.month in {1, 4, 7, 10} for m in marks)
    assert len(months) == len(marks)  # one per quarter month


def test_panel_policies_leave_point_in_time_eligibility_to_the_panel() -> None:
    dataset = _make_dataset(80, {"AAA": {}, "BBB": {}})
    panel_marker = object()
    dataset = EquityDataset(
        **{
            **dataset.__dict__,
            "panel_inputs": {dataset.sessions[-1]: panel_marker},
        }
    )
    selection = PanelExecutionUniversePolicy().select(
        dataset,
        asof=dataset.sessions[-1],
        config=PortfolioConfig(
            eval_start=dataset.sessions[-2],
            eval_end=dataset.sessions[-1],
        ),
    )

    assert selection.symbols == ("AAA", "BBB")
    assert selection.detail["eligibility_owner"] == "revision_pinned_strategy_panel"
    assert (
        StaticExecutionUniverseCadencePolicy().sessions(
            dataset.sessions,
            start=dataset.sessions[0],
            end=dataset.sessions[-1],
        )
        == ()
    )

    class PanelStrategy:
        def __init__(self) -> None:
            self.received: list[object] = []

        def panel_ready_input(self, panel_input):  # type: ignore[no-untyped-def]
            return panel_input

        def serialize_panel_input(self, panel_input):  # type: ignore[no-untyped-def]
            return {"panel": panel_input}

        def deserialize_panel_input(self, payload):  # type: ignore[no-untyped-def]
            return payload["panel"]

        def evaluate_panel(self, panel_input):  # type: ignore[no-untyped-def]
            self.received.append(panel_input)

    strategy = PanelStrategy()
    policy = SynchronizedStrategyPanelContextPolicy()
    policy.on_panel(
        strategy=strategy,  # type: ignore[arg-type]
        panel=EquitySessionPanel(
            session=dataset.sessions[-1],
            session_index=len(dataset.sessions) - 1,
            effective_symbols=selection.symbols,
            bars={},
            panel_input=panel_marker,
        ),
    )
    assert strategy.received == [panel_marker]


def test_panel_execution_universe_rejects_missing_panels() -> None:
    dataset = _make_dataset(80, {"AAA": {}})
    with pytest.raises(EquityPortfolioError, match="immutable panel inputs"):
        PanelExecutionUniversePolicy().select(
            dataset,
            asof=dataset.sessions[-1],
            config=PortfolioConfig(
                eval_start=dataset.sessions[-2],
                eval_end=dataset.sessions[-1],
            ),
        )


def test_earnings_distances_trading_days() -> None:
    sessions = _sessions(20)
    at = earnings_distances([sessions[10]], sessions)
    assert at(7) == (3, None)
    assert at(10) == (0, 0)
    assert at(12) == (None, 2)


def test_vix_percentiles_need_min_history() -> None:
    dataset = _make_dataset(50, {"AAA": {}})
    percentiles = vix_percentiles(dataset)
    assert set(percentiles) == set(dataset.sessions)
    assert all(v is None or 0.0 <= v <= 1.0 for v in percentiles.values())


@pytest.mark.parametrize(
    ("session", "expected_bps"),
    [
        (date(2020, 1, 2), 0.207),
        (date(2020, 2, 18), 0.221),
        (date(2021, 2, 25), 0.051),
        (date(2022, 5, 14), 0.229),
        (date(2023, 2, 27), 0.080),
        (date(2024, 5, 22), 0.278),
        (date(2025, 5, 14), 0.000),
    ],
)
def test_section31_fee_schedule_matches_effective_sec_advisories(
    session: date,
    expected_bps: float,
) -> None:
    assert _section31_bps(session) == expected_bps


@pytest.mark.parametrize(
    ("session", "rate_per_share", "maximum_fee"),
    [
        (date(2020, 1, 2), 0.000119, 5.95),
        (date(2021, 12, 31), 0.000119, 5.95),
        (date(2022, 1, 3), 0.000130, 6.49),
        (date(2023, 1, 3), 0.000145, 7.27),
        (date(2024, 1, 2), 0.000166, 8.30),
        (date(2025, 12, 31), 0.000166, 8.30),
    ],
)
def test_finra_taf_schedule_applies_per_share_rate_and_cap(
    session: date,
    rate_per_share: float,
    maximum_fee: float,
) -> None:
    reference_price = 10.0
    uncapped_shares = 1_000.0
    uncapped_notional = reference_price * uncapped_shares
    uncapped_bps = _finra_taf_bps(
        session,
        reference_price=reference_price,
        reference_notional=uncapped_notional,
    )
    assert uncapped_bps / 10_000.0 * uncapped_notional == pytest.approx(
        uncapped_shares * rate_per_share
    )

    capped_notional = reference_price * 100_000.0
    capped_bps = _finra_taf_bps(
        session,
        reference_price=reference_price,
        reference_notional=capped_notional,
    )
    assert capped_bps / 10_000.0 * capped_notional == pytest.approx(maximum_fee)


# ---------------------------------------------------------------------------
# end-to-end simulation with the production core
# ---------------------------------------------------------------------------


def test_end_to_end_basket_entry_books_next_open_fill_and_costs() -> None:
    # The basket core goes long every fed symbol on its first post-bootstrap
    # bar; both entries must fill at the NEXT session open plus 5 bps.
    dataset = _make_dataset(DIP_START + 20, {"AAA": {}, "BBB": {}})
    config = PortfolioConfig(
        eval_start=dataset.sessions[280],
        eval_end=dataset.sessions[-1],
        initial_capital=1_000_000.0,
        universe_size=2,
    )
    result = EquityPortfolioBacktest(dataset, _core_factory, config).run()

    entered = [position for position in result.terminal_positions if position.symbol == "AAA"]
    assert len(entered) == 1
    position = entered[0]
    trigger_idx = dataset.sessions.index(position.entry_session) - 1
    trigger_bar = dataset.bars["AAA"][trigger_idx + 1]
    # Fill = next session open plus 5 bps (no stress in this dataset).
    assert position.entry_fill == pytest.approx(trigger_bar.open * (1 + 5.0 / 10_000.0))
    assert result.trades == []
    assert result.metrics["trade_count"] == 0.0
    assert result.metrics["terminal_position_count"] == 2.0
    assert len(result.daily_exposures) == len(result.equity_curve)
    assert result.metrics["peak_gross_exposure_pct"] >= result.metrics["average_gross_exposure_pct"]
    assert result.metrics["time_in_market_pct"] == result.metrics["exposure_pct"]
    assert result.metrics["two_way_turnover_x_per_year"] == result.metrics["turnover_x_per_year"]
    assert result.metrics["two_way_traded_notional"] == pytest.approx(
        sum(
            row["execution_price"] * row["quantity"]
            for row in result.cost_ledger
            if row["turnover_eligible"]
        )
    )
    assert result.metrics["max_drawdown_pct"] >= 0.0
    assert result.period_returns.get("monthly")
    assert result.benchmark["beta_vs_benchmark"] is not None
    assert result.benchmark["benchmark_symbol"] == "SPY"
    assert result.benchmark["benchmark_kind"] == "adjusted_security"
    eval_index = dataset.sessions.index(result.equity_curve[0][0].date())
    benchmark_base_session = dataset.sessions[eval_index - 1]
    expected_benchmark_return = (
        dataset.benchmark_total_return[result.equity_curve[-1][0].date()]
        / dataset.benchmark_total_return[benchmark_base_session]
        - 1.0
    ) * 100.0
    assert result.benchmark["benchmark_base_session"] == benchmark_base_session.isoformat()
    assert result.benchmark["benchmark_total_return_pct"] == pytest.approx(
        expected_benchmark_return
    )


def test_portfolio_engine_uses_injected_policies_and_panel_boundary() -> None:
    dataset = _make_dataset(DIP_START + 20, {"AAA": {}, "BBB": {}})
    config = PortfolioConfig(
        eval_start=dataset.sessions[280],
        eval_end=dataset.sessions[-1],
        universe_size=2,
    )
    observed_panels: list[EquitySessionPanel] = []

    class UniversePolicy:
        policy_id = "registered_universe"

        def select(self, _dataset, *, asof, config):  # type: ignore[no-untyped-def]
            del _dataset, config
            return UniverseSelection(symbols=("AAA",), detail={"asof": asof.isoformat()})

    class CadencePolicy:
        policy_id = "registered_cadence"

        def sessions(self, available_sessions, *, start, end):  # type: ignore[no-untyped-def]
            del available_sessions, start, end
            return ()

    class PanelPolicy:
        policy_id = "registered_panel"

        def metadata(  # type: ignore[no-untyped-def]
            self,
            *,
            dataset,
            symbol,
            bar,
            session_index,
            vix_percentile,
            earnings_distance,
        ):
            del dataset, symbol, session_index, vix_percentile, earnings_distance
            return {"benchmark_close": 100.0, "session": bar.session.isoformat()}

        def on_panel(self, *, strategy, panel):  # type: ignore[no-untyped-def]
            del strategy
            observed_panels.append(panel)

    class TargetPolicy:
        policy_id = "registered_targets"

        def entry_notional(  # type: ignore[no-untyped-def]
            self,
            *,
            signal,
            previous_equity,
            available_cash,
        ):
            del signal
            return min(previous_equity * 0.25, available_cash)

    class CostPolicy:
        policy_id = "registered_costs"
        latency_sessions = 0

        def assumptions(self):  # type: ignore[no-untyped-def]
            return {"registered": True}

        def entry_fill(  # type: ignore[no-untyped-def]
            self,
            *,
            reference_open,
            reference_notional,
            available_daily_notional,
            session,
            stressed,
        ):
            del available_daily_notional, reference_notional, session, stressed
            return reference_open

        def exit_fill(  # type: ignore[no-untyped-def]
            self,
            *,
            reference_price,
            reference_notional,
            available_daily_notional,
            session,
            stressed,
        ):
            del available_daily_notional, reference_notional, session, stressed
            return reference_price

        def cost_components(  # type: ignore[no-untyped-def]
            self,
            *,
            reference_price,
            execution_price,
            quantity,
            reference_notional,
            available_daily_notional,
            session,
            side,
            stressed,
        ):
            del (
                reference_price,
                execution_price,
                quantity,
                reference_notional,
                available_daily_notional,
                session,
                side,
                stressed,
            )
            return {
                "commission": 0.0,
                "half_spread": 0.0,
                "impact": 0.0,
                "regulatory_fee": 0.0,
                "slippage": 0.0,
                "total": 0.0,
            }

    result = EquityPortfolioBacktest(
        dataset,
        _core_factory,
        config,
        universe_policy=UniversePolicy(),
        cadence_policy=CadencePolicy(),
        panel_context_policy=PanelPolicy(),
        target_allocation_policy=TargetPolicy(),
        execution_cost_policy=CostPolicy(),
    ).run()

    assert result.policies == {
        "cadence": "registered_cadence",
        "corporate_actions": "total_return_adjusted_actions_v1",
        "execution_cost": "registered_costs",
        "panel_context": "registered_panel",
        "target_allocation": "registered_targets",
        "terminal_liquidation_sensitivity": (
            "non_actionable_hypothetical_immediate_exit_at_terminal_mark_v1"
        ),
        "terminal_treatment": "mark_to_market_unliquidated",
        "turnover": "two_way_execution_notional_over_average_daily_net_nav_annualized_v1",
        "universe": "registered_universe",
    }
    assert observed_panels
    assert all(panel.effective_symbols == ("AAA",) for panel in observed_panels)
    assert result.gross_equity_curve == result.equity_curve
    assert result.gross_metrics["total_return_pct"] == result.metrics["total_return_pct"]
    assert result.execution_assumptions == {"registered": True}


def test_open_impact_and_terminal_sensitivity_use_only_prior_observations() -> None:
    dataset = _make_dataset(DIP_START + 20, {"AAA": {}})
    eval_start = dataset.sessions[280]
    entry_session = dataset.sessions[281]
    final_session = dataset.sessions[-1]
    extreme_same_day_volume = 10**15
    amended_bars = [
        DailyBar(
            session=bar.session,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            raw_open=bar.raw_open,
            raw_high=bar.raw_high,
            raw_low=bar.raw_low,
            raw_close=bar.raw_close,
            split_adjusted_open=bar.split_adjusted_open,
            split_adjusted_high=bar.split_adjusted_high,
            split_adjusted_low=bar.split_adjusted_low,
            split_adjusted_close=bar.split_adjusted_close,
            split_adjusted_volume=(
                extreme_same_day_volume
                if bar.session in {entry_session, final_session}
                else bar.split_adjusted_volume
            ),
            split_adjustment_factor=bar.split_adjustment_factor,
        )
        for bar in dataset.bars["AAA"]
    ]
    amended_vix = dict(dataset.vix)
    amended_vix[eval_start] = 1.0
    amended_vix[dataset.sessions[-2]] = 1_000_000.0
    dataset = EquityDataset(
        **{
            **dataset.__dict__,
            "bars": {"AAA": amended_bars},
            "vix": amended_vix,
        }
    )
    config = PortfolioConfig(
        eval_start=eval_start,
        eval_end=final_session,
        initial_capital=100_000.0,
        universe_size=1,
    )
    observed_entries: list[tuple[date, float | None, bool]] = []
    observed_exits: list[tuple[date, float | None, bool]] = []

    class EnterOnceCore(PureSignalStrategy):
        def __init__(self) -> None:
            super().__init__("enter_once", "indicator")

        def initialize(self) -> None:
            return None

        def on_data(self, state: MarketState) -> None:
            if state.timestamp.date() == eval_start:
                self.emit_signal(
                    SignalAction.LONG,
                    state.symbol,
                    timestamp=state.timestamp,
                    entry_price=state.close,
                    size_hint=0.5,
                )

    class ObservingCostPolicy:
        policy_id = "observing_cutoff_known_liquidity"
        latency_sessions = 0

        def assumptions(self):  # type: ignore[no-untyped-def]
            return {"liquidity_cutoff": "strictly_before_execution_session"}

        def entry_fill(  # type: ignore[no-untyped-def]
            self,
            *,
            reference_open,
            reference_notional,
            available_daily_notional,
            session,
            stressed,
        ):
            del reference_notional
            observed_entries.append((session, available_daily_notional, stressed))
            return reference_open

        def exit_fill(  # type: ignore[no-untyped-def]
            self,
            *,
            reference_price,
            reference_notional,
            available_daily_notional,
            session,
            stressed,
        ):
            del reference_notional
            observed_exits.append((session, available_daily_notional, stressed))
            return reference_price

        def cost_components(  # type: ignore[no-untyped-def]
            self,
            *,
            reference_price,
            execution_price,
            quantity,
            reference_notional,
            available_daily_notional,
            session,
            side,
            stressed,
        ):
            del (
                available_daily_notional,
                execution_price,
                quantity,
                reference_notional,
                reference_price,
                session,
                side,
                stressed,
            )
            return {
                "commission": 0.0,
                "half_spread": 0.0,
                "impact": 0.0,
                "regulatory_fee": 0.0,
                "slippage": 0.0,
                "total": 0.0,
            }

    result = EquityPortfolioBacktest(
        dataset,
        EnterOnceCore,
        config,
        execution_cost_policy=ObservingCostPolicy(),
    ).run()

    expected_entry_adv, _entry_observations = median_dollar_adv(
        amended_bars,
        before=entry_session,
        window=config.adv_window,
    )
    expected_terminal_adv, _terminal_observations = median_dollar_adv(
        amended_bars,
        before=final_session,
        window=config.adv_window,
    )
    assert observed_entries == [(entry_session, expected_entry_adv, False)]
    assert observed_exits == [(final_session, expected_terminal_adv, True)]
    assert not [row for row in result.cost_ledger if row["side"] == "sell"]
    assert result.trades == []
    assert len(result.terminal_positions) == 1
    assert result.terminal_liquidation_sensitivity is not None
    assert result.terminal_liquidation_sensitivity["actionable"] is False
    assert result.terminal_liquidation_sensitivity["primary_metrics_affected"] is False
    assert result.terminal_liquidation_sensitivity["turnover_affected"] is False
    assert result.terminal_liquidation_sensitivity["marked_terminal_nav"] == pytest.approx(
        result.equity_curve[-1][1]
    )
    assert expected_entry_adv is not None
    assert expected_entry_adv < (
        next(bar.raw_close for bar in amended_bars if bar.session == entry_session)
        * extreme_same_day_volume
    )


def test_equity_accounting_is_consistent() -> None:
    overrides = _dip(DIP_START)  # price path detail; the identity must hold regardless
    dataset = _make_dataset(DIP_START + 15, {"AAA": overrides, "BBB": {}})
    config = PortfolioConfig(
        eval_start=dataset.sessions[280], eval_end=dataset.sessions[-1], universe_size=2
    )
    result = EquityPortfolioBacktest(dataset, _core_factory, config).run()
    values = [v for _, v in result.equity_curve]
    assert all(v > 0 for v in values)
    # Terminal survivors remain marked; realized plus marked unrealized PnL reconciles NAV.
    total_pnl = sum(t.pnl for t in result.trades) + sum(
        position.net_unrealized_pnl for position in result.terminal_positions
    )
    assert values[-1] == pytest.approx(config.initial_capital + total_pnl, rel=1e-9)


def test_institutional_costs_create_reconciled_gross_net_ledgers_and_delay() -> None:
    dataset = _make_dataset(DIP_START + 20, {"AAA": {}, "BBB": {}})
    config = PortfolioConfig(
        eval_start=dataset.sessions[280],
        eval_end=dataset.sessions[-1],
        universe_size=2,
    )
    baseline = EquityPortfolioBacktest(dataset, _core_factory, config).run()
    costs = InstitutionalEquityExecutionCostPolicy(
        cost_model=ExecutionCostModel(
            scenario_name="equity_expected",
            commission_bps=1.0,
            half_spread_bps=2.0,
            slippage_bps=1.0,
            impact_bps=0.5,
            latency_bars=1,
        ),
        participation_impact_bps_at_full_daily_volume=20.0,
        stress_multiplier=1.5,
    )

    result = EquityPortfolioBacktest(
        dataset,
        _core_factory,
        config,
        execution_cost_policy=costs,
    ).run()

    baseline_entry = min(position.entry_session for position in baseline.terminal_positions)
    delayed_entry = min(position.entry_session for position in result.terminal_positions)
    assert delayed_entry > baseline_entry
    assert result.gross_equity_curve[-1][1] > result.equity_curve[-1][1]
    assert result.metrics["total_execution_cost"] > 0.0
    assert result.metrics["gross_to_net_total_return_drag_pct"] > 0.0
    assert result.terminal_liquidation_sensitivity is not None
    assert result.terminal_liquidation_sensitivity["estimated_total_cost"] > 0.0
    assert result.metrics["final_equity"] == pytest.approx(
        result.terminal_liquidation_sensitivity["marked_terminal_nav"]
    )
    assert (
        result.metrics["final_equity"]
        > result.terminal_liquidation_sensitivity["estimated_liquidated_nav"]
    )
    assert result.metrics["total_execution_cost"] == pytest.approx(
        sum(row["components"]["total"] for row in result.cost_ledger)
    )
    assert all(
        row["components"]["total"]
        == pytest.approx(sum(value for name, value in row["components"].items() if name != "total"))
        for row in result.cost_ledger
    )


def test_hold_signal_reconciles_an_incumbent_to_the_new_target_weight() -> None:
    dataset = _make_dataset(DIP_START + 10, {"AAA": {}})
    eval_start = dataset.sessions[280]
    hold_session = dataset.sessions[282]
    config = PortfolioConfig(
        eval_start=eval_start,
        eval_end=dataset.sessions[-1],
        initial_capital=100_000.0,
        universe_size=1,
    )

    class TargetRevisionCore(PureSignalStrategy):
        def __init__(self) -> None:
            super().__init__("target_revision", "indicator")

        def initialize(self) -> None:
            return None

        def on_data(self, state: MarketState) -> None:
            session = state.timestamp.date()
            if session == eval_start:
                self.emit_signal(
                    SignalAction.LONG,
                    state.symbol,
                    timestamp=state.timestamp,
                    entry_price=state.close,
                    size_hint=0.50,
                    metadata={"target_weight_drift_fraction": 0.15},
                )
            elif session == hold_session:
                self.emit_signal(
                    SignalAction.HOLD,
                    state.symbol,
                    timestamp=state.timestamp,
                    entry_price=state.close,
                    size_hint=0.20,
                    metadata={"target_weight_drift_fraction": 0.15},
                )

    result = EquityPortfolioBacktest(dataset, TargetRevisionCore, config).run()

    partial = [trade for trade in result.trades if trade.exit_reason == "target_rebalance"]
    assert len(partial) == 1
    assert partial[0].shares > 0.0
    target_events = [
        row["event"]
        for row in result.rebalance_log
        if row.get("event") in {"target_buy", "target_reduce"}
    ]
    assert target_events == ["target_buy", "target_reduce"]
    assert any(row["side"] == "sell" for row in result.cost_ledger)


def test_target_reductions_fund_increases_independently_of_signal_priority() -> None:
    sessions = _sessions(270)
    flat_bars = _bars_from_closes(sessions, [100.0] * len(sessions), volume=1e9)
    dataset = EquityDataset(
        sessions=sessions,
        bars={"AAA": flat_bars, "BBB": flat_bars},
        benchmark_price=dict.fromkeys(sessions, 100.0),
        benchmark_total_return=dict.fromkeys(sessions, 100.0),
        vix=dict.fromkeys(sessions, 15.0),
        earnings={"AAA": [], "BBB": []},
        membership=_full_membership(["AAA", "BBB"]),
        benchmark_total_return_kind="adjusted_security",
        benchmark_total_return_symbol="SPY",
    )
    eval_start = sessions[262]
    rebalance_session = sessions[264]
    config = PortfolioConfig(
        eval_start=eval_start,
        eval_end=sessions[-1],
        initial_capital=100_000.0,
        universe_size=2,
        costs=CostConfig(base_bps=0.0),
    )

    def run(*, underweight_first: bool) -> BacktestResult:
        class ReductionFirstCore(PureSignalStrategy):
            def __init__(self) -> None:
                super().__init__("reduction_first", "indicator")

            def initialize(self) -> None:
                return None

            def on_data(self, state: MarketState) -> None:
                session = state.timestamp.date()
                if session == eval_start:
                    target = {"AAA": 0.20, "BBB": 0.80}[state.symbol]
                elif session == rebalance_session:
                    target = 0.50
                else:
                    return
                confidence = 0.90 if (state.symbol == "AAA") is underweight_first else 0.80
                self.emit_signal(
                    SignalAction.LONG if session == eval_start else SignalAction.HOLD,
                    state.symbol,
                    timestamp=state.timestamp,
                    entry_price=state.close,
                    confidence=confidence,
                    size_hint=target,
                    metadata={"target_weight_drift_fraction": 0.0},
                )

        return EquityPortfolioBacktest(dataset, ReductionFirstCore, config).run()

    underweight_first = run(underweight_first=True)
    overweight_first = run(underweight_first=False)
    for result in (underweight_first, overweight_first):
        rebalances = [
            row
            for row in result.rebalance_log
            if row.get("event") in {"target_buy", "target_reduce"}
            and row.get("session") > rebalance_session.isoformat()
        ]
        assert [(row["event"], row["symbol"]) for row in rebalances] == [
            ("target_reduce", "BBB"),
            ("target_buy", "AAA"),
        ]
        assert rebalances[0]["target_notional"] == pytest.approx(rebalances[1]["target_notional"])
        assert rebalances[1]["execution_notional"] > 29_000.0
    assert underweight_first.equity_curve == overweight_first.equity_curve


def test_target_additions_and_reductions_preserve_fifo_trade_statistics() -> None:
    dataset = _make_dataset(DIP_START + 12, {"AAA": {}})
    eval_start = dataset.sessions[280]
    add_session = dataset.sessions[282]
    reduce_session = dataset.sessions[284]
    config = PortfolioConfig(
        eval_start=eval_start,
        eval_end=dataset.sessions[-1],
        initial_capital=100_000.0,
        universe_size=1,
    )

    class FifoTargetCore(PureSignalStrategy):
        def __init__(self) -> None:
            super().__init__("fifo_target", "indicator")

        def initialize(self) -> None:
            return None

        def on_data(self, state: MarketState) -> None:
            targets = {
                eval_start: (SignalAction.LONG, 0.20),
                add_session: (SignalAction.HOLD, 0.80),
                reduce_session: (SignalAction.HOLD, 0.10),
            }
            decision = targets.get(state.timestamp.date())
            if decision is None:
                return
            action, target = decision
            self.emit_signal(
                action,
                state.symbol,
                timestamp=state.timestamp,
                entry_price=state.close,
                size_hint=target,
                metadata={"target_weight_drift_fraction": 0.0},
            )

    result = EquityPortfolioBacktest(dataset, FifoTargetCore, config).run()

    reductions = [trade for trade in result.trades if trade.exit_reason == "target_rebalance"]
    assert len(reductions) == 2
    assert reductions[0].entry_session < reductions[1].entry_session
    assert reductions[0].holding_sessions > reductions[1].holding_sessions
    assert len(result.terminal_positions) == 1
    assert result.terminal_positions[0].entry_session == reductions[1].entry_session
    assert not [trade for trade in result.trades if trade.exit_reason == "end_of_window"]
    accounted_pnl = sum(trade.pnl for trade in result.trades) + sum(
        position.net_unrealized_pnl for position in result.terminal_positions
    )
    assert accounted_pnl == pytest.approx(result.equity_curve[-1][1] - config.initial_capital)


def test_target_weight_drift_inside_strategy_band_does_not_trade() -> None:
    dataset = _make_dataset(DIP_START + 10, {"AAA": {}})
    eval_start = dataset.sessions[280]
    hold_session = dataset.sessions[282]
    config = PortfolioConfig(
        eval_start=eval_start,
        eval_end=dataset.sessions[-1],
        initial_capital=100_000.0,
        universe_size=1,
    )

    class BandedTargetCore(PureSignalStrategy):
        def __init__(self) -> None:
            super().__init__("banded_target", "indicator")

        def initialize(self) -> None:
            return None

        def on_data(self, state: MarketState) -> None:
            session = state.timestamp.date()
            if session not in {eval_start, hold_session}:
                return
            self.emit_signal(
                SignalAction.LONG if session == eval_start else SignalAction.HOLD,
                state.symbol,
                timestamp=state.timestamp,
                entry_price=state.close,
                size_hint=0.50 if session == eval_start else 0.46,
                metadata={"target_weight_drift_fraction": 0.15},
            )

    result = EquityPortfolioBacktest(dataset, BandedTargetCore, config).run()

    target_events = [
        row["event"]
        for row in result.rebalance_log
        if row.get("event") in {"target_buy", "target_reduce"}
    ]
    assert target_events == ["target_buy"]
    assert not [trade for trade in result.trades if trade.exit_reason == "target_rebalance"]


def test_explicit_dividend_action_is_lineaged_and_credited_once() -> None:
    dataset = _make_dataset(DIP_START + 20, {"AAA": {}, "BBB": {}})
    action_session = dataset.sessions[285]
    dataset = EquityDataset(
        **{
            **dataset.__dict__,
            "corporate_actions": {
                "AAA": (
                    EquityCorporateAction(
                        symbol="AAA",
                        effective_session=action_session,
                        kind=EquityCorporateActionKind.CASH_DIVIDEND,
                        value=1.0,
                        source_observation_id="AAA:dividend:2019-02",
                        currency="USD",
                    ),
                )
            },
        }
    )
    config = PortfolioConfig(
        eval_start=dataset.sessions[280],
        eval_end=dataset.sessions[-1],
        universe_size=2,
    )

    result = EquityPortfolioBacktest(
        dataset,
        _core_factory,
        config,
        corporate_action_policy=ExplicitRawCorporateActionPolicy(),
    ).run()

    assert len(result.corporate_action_ledger) == 1
    action = result.corporate_action_ledger[0]
    assert action["source_observation_id"] == "AAA:dividend:2019-02"
    assert action["cash_payment"] == 0.0
    assert action["cash_available_session"] is None
    assert action["currency"] == "USD"
    assert action["receivable_credit"] > 0.0
    assert action["encoded_in_adjusted_prices"] is False
    assert result.metrics["terminal_dividend_receivable"] == pytest.approx(
        action["receivable_credit"]
    )


def test_cash_dividend_requires_canonical_currency() -> None:
    with pytest.raises(ValueError, match="cash-dividend currency"):
        EquityCorporateAction(
            symbol="AAA",
            effective_session=date(2024, 1, 2),
            kind=EquityCorporateActionKind.CASH_DIVIDEND,
            value=1.0,
            source_observation_id="AAA:dividend:missing-currency",
        )


def test_raw_split_and_dividend_accounting_matches_adjusted_total_return_without_double_count() -> (
    None
):
    sessions = _sessions(270)
    dividend_session = sessions[265]
    split_session = sessions[267]
    bars = []
    for session in sessions:
        raw_price = 100.0 if session < dividend_session else 99.0
        if session >= split_session:
            raw_price = 49.5
        bars.append(
            DailyBar(
                session=session,
                open=49.5,
                high=49.5,
                low=49.5,
                close=49.5,
                raw_open=raw_price,
                raw_high=raw_price,
                raw_low=raw_price,
                raw_close=raw_price,
                split_adjusted_open=raw_price,
                split_adjusted_high=raw_price,
                split_adjusted_low=raw_price,
                split_adjusted_close=raw_price,
                split_adjusted_volume=1e9,
                split_adjustment_factor=1.0,
            )
        )
    actions = (
        EquityCorporateAction(
            symbol="AAA",
            effective_session=dividend_session,
            kind=EquityCorporateActionKind.CASH_DIVIDEND,
            value=1.0,
            source_observation_id="AAA:dividend:source",
            currency="USD",
        ),
        EquityCorporateAction(
            symbol="AAA",
            effective_session=split_session,
            kind=EquityCorporateActionKind.SPLIT,
            value=2.0,
            source_observation_id="AAA:split:source",
        ),
    )
    dataset = EquityDataset(
        sessions=sessions,
        bars={"AAA": bars},
        benchmark_price=dict.fromkeys(sessions, 100.0),
        benchmark_total_return=dict.fromkeys(sessions, 100.0),
        vix=dict.fromkeys(sessions, 15.0),
        earnings={"AAA": []},
        membership=_full_membership(["AAA"]),
        benchmark_total_return_kind="adjusted_security",
        benchmark_total_return_symbol="SPY",
        corporate_actions={"AAA": actions},
    )
    eval_start = sessions[262]
    config = PortfolioConfig(
        eval_start=eval_start,
        eval_end=sessions[-1],
        initial_capital=100_000.0,
        universe_size=1,
        costs=CostConfig(base_bps=0.0),
    )

    class FullyInvestedCore(PureSignalStrategy):
        def __init__(self) -> None:
            super().__init__("fully_invested", "indicator")

        def initialize(self) -> None:
            return None

        def on_data(self, state: MarketState) -> None:
            if state.timestamp.date() == eval_start:
                self.emit_signal(
                    SignalAction.LONG,
                    state.symbol,
                    timestamp=state.timestamp,
                    entry_price=state.close,
                    size_hint=1.0,
                )

    adjusted = EquityPortfolioBacktest(dataset, FullyInvestedCore, config).run()
    raw = EquityPortfolioBacktest(
        dataset,
        FullyInvestedCore,
        config,
        corporate_action_policy=ExplicitRawCorporateActionPolicy(),
    ).run()

    assert [timestamp for timestamp, _value in raw.equity_curve] == [
        timestamp for timestamp, _value in adjusted.equity_curve
    ]
    assert [value for _timestamp, value in raw.equity_curve] == pytest.approx(
        [value for _timestamp, value in adjusted.equity_curve]
    )
    raw_actions = {row["kind"]: row for row in raw.corporate_action_ledger}
    assert raw_actions["cash_dividend"]["cash_payment"] == 0.0
    assert raw_actions["cash_dividend"]["receivable_credit"] == pytest.approx(1_000.0)
    assert raw_actions["split"]["share_multiplier"] == 2.0
    assert all(row["encoded_in_adjusted_prices"] is False for row in raw_actions.values())
    assert all(
        row["cash_payment"] == 0.0
        and row["receivable_credit"] == 0.0
        and row["encoded_in_adjusted_prices"] is True
        for row in adjusted.corporate_action_ledger
    )


def test_missing_terminal_disposition_fails_closed_without_last_close_fill() -> None:
    dataset = _make_dataset(DIP_START + 20, {"AAA": {}, "BBB": {}})
    final_aaa_bar_index = 290
    dataset = EquityDataset(
        **{
            **dataset.__dict__,
            "bars": {
                **dataset.bars,
                "AAA": dataset.bars["AAA"][: final_aaa_bar_index + 1],
            },
        }
    )
    config = PortfolioConfig(
        eval_start=dataset.sessions[280],
        eval_end=dataset.sessions[-1],
        universe_size=2,
    )

    with pytest.raises(EquityPortfolioError, match="verified terminal disposition"):
        EquityPortfolioBacktest(dataset, _core_factory, config).run()


@pytest.mark.parametrize(
    ("kind", "settlement_price"),
    [
        (EquityTerminalDispositionKind.CASH_SETTLEMENT, 25.0),
        (EquityTerminalDispositionKind.ZERO_RECOVERY, 0.0),
    ],
)
def test_verified_terminal_disposition_is_lineaged_without_fabricated_execution(
    kind: EquityTerminalDispositionKind,
    settlement_price: float,
) -> None:
    dataset = _make_dataset(DIP_START + 20, {"AAA": {}, "BBB": {}})
    final_aaa_bar_index = 290
    effective_session = dataset.sessions[final_aaa_bar_index + 1]
    source_observation_id = f"AAA:{kind.value}:verified"
    dataset = EquityDataset(
        **{
            **dataset.__dict__,
            "bars": {
                **dataset.bars,
                "AAA": dataset.bars["AAA"][: final_aaa_bar_index + 1],
            },
            "terminal_dispositions": {
                "AAA": (
                    EquityTerminalDisposition(
                        symbol="AAA",
                        effective_session=effective_session,
                        kind=kind,
                        settlement_price=settlement_price,
                        source_observation_id=source_observation_id,
                    ),
                )
            },
        }
    )
    config = PortfolioConfig(
        eval_start=dataset.sessions[280],
        eval_end=dataset.sessions[-1],
        universe_size=2,
    )

    result = EquityPortfolioBacktest(dataset, _core_factory, config).run()

    aaa = next(
        trade
        for trade in result.trades
        if trade.symbol == "AAA" and trade.exit_reason == f"terminal_{kind.value}"
    )
    assert aaa.exit_fill == settlement_price
    assert aaa.exit_reference == settlement_price
    assert aaa.exit_cost == 0.0
    assert result.terminal_disposition_ledger == [
        {
            "effective_session": effective_session.isoformat(),
            "kind": kind.value,
            "position_affected": True,
            "quantity": aaa.shares,
            "settlement_cash": settlement_price * aaa.shares,
            "settlement_price": settlement_price,
            "source_observation_id": source_observation_id,
            "symbol": "AAA",
        }
    ]
    terminal_cost = next(
        row
        for row in result.cost_ledger
        if row["symbol"] == "AAA" and row["session"] == effective_session.isoformat()
    )
    assert terminal_cost["execution_price"] == settlement_price
    assert terminal_cost["components"]["total"] == 0.0
    assert terminal_cost["event_kind"] == "verified_terminal_disposition"
    assert terminal_cost["turnover_eligible"] is False


def test_universe_rotation_force_closes_positions() -> None:
    # AAA's volume collapses at session 445; the 126-session ADV median flips
    # below BBB by the 2021-Q2 rebalance (~session 566), which must force-close
    # the basket's AAA position with reason universe_rotation.
    sessions_needed = 580
    overrides = _dip(440)
    dataset = _make_dataset(
        sessions_needed,
        {"AAA": overrides, "BBB": {}},
        volumes={"AAA": 3e9, "BBB": 2e9},
    )
    # After session 445, AAA volume collapses so the next rebalance drops it.
    bars_aaa = [
        DailyBar(
            session=b.session,
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            raw_open=b.raw_open,
            raw_high=b.raw_high,
            raw_low=b.raw_low,
            raw_close=b.raw_close,
            split_adjusted_open=b.split_adjusted_open,
            split_adjusted_high=b.split_adjusted_high,
            split_adjusted_low=b.split_adjusted_low,
            split_adjusted_close=b.split_adjusted_close,
            split_adjusted_volume=(b.split_adjusted_volume if i < 445 else 1.0),
            split_adjustment_factor=b.split_adjustment_factor,
        )
        for i, b in enumerate(dataset.bars["AAA"])
    ]
    dataset = EquityDataset(**{**dataset.__dict__, "bars": {**dataset.bars, "AAA": bars_aaa}})
    config = PortfolioConfig(
        eval_start=dataset.sessions[430], eval_end=dataset.sessions[-1], universe_size=1
    )
    result = EquityPortfolioBacktest(dataset, _core_factory, config).run()
    # The basket always holds AAA while it leads the pool, so the volume
    # collapse MUST produce a forced universe_rotation close.
    aaa_reasons = {t.exit_reason for t in result.trades if t.symbol == "AAA"}
    assert "universe_rotation" in aaa_reasons
    assert result.rebalance_log  # margin diagnostics recorded


def test_worst_trades_orders_by_pnl() -> None:
    overrides = _dip(DIP_START)
    overrides[DIP_START + 3] = -0.20  # disaster
    dataset = _make_dataset(DIP_START + 12, {"AAA": overrides, "BBB": {}})
    config = PortfolioConfig(
        eval_start=dataset.sessions[280], eval_end=dataset.sessions[-1], universe_size=2
    )
    result = EquityPortfolioBacktest(dataset, _core_factory, config).run()
    worst = worst_trades(result, count=3)
    if len(worst) >= 2:
        assert worst[0].pnl <= worst[1].pnl
