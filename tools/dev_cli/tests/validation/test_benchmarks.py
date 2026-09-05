"""Executable benchmark contracts over frozen real Coinbase candles."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from validation_helpers import bars_from_ohlcv, load_market_fixture

from dev_cli.validation.backtest.engine import BacktestConfig
from dev_cli.validation.backtest.execution import (
    ExecutionCostModel,
    FillPolicy,
    execution_bar_times,
)
from dev_cli.validation.benchmarks import (
    BenchmarkDefinition,
    BenchmarkKind,
    VolatilityTargetConfig,
    build_benchmark_strategy,
    calibrate_fixed_volatility_exposure,
    run_consolidated_benchmark,
)
from lib_data.bars import Bar
from lib_strategy.position_sizing import PositionSizingRequest
from lib_strategy.signals.pure_strategy import MarketState
from lib_strategy.signals.signal import Signal, SignalAction

_FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "tests/fixtures/market_data/coinbase_btcusd_1m_2026-06-10.json"
)


def _bars(*, count: int = 400) -> list[Bar]:
    payload = load_market_fixture(_FIXTURE)
    return bars_from_ohlcv(
        payload["bars"][:count],
        symbol="BTCUSD",
        source="coinbase_live",
    )


def _states(bars: list[Bar]) -> list[MarketState]:
    return [
        MarketState(
            symbol=bar.symbol,
            timestamp=execution_bar_times(bar).close_ts,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            metadata={"timeframe": bar.timeframe, "source": bar.source},
        )
        for bar in bars
    ]


def _config(**overrides: object) -> BacktestConfig:
    values: dict[str, object] = {
        "symbol": "BTCUSD",
        "consolidation_minutes": 0,
        "fee_bps": 0.0,
        "execution_costs": ExecutionCostModel(
            scenario_name="zero-cost-test",
            commission_bps=0.0,
            historical_basis="backward-applied-scenario",
        ),
        "fill_policy": FillPolicy.reference_v2(),
    }
    values.update(overrides)
    return BacktestConfig(**values)  # type: ignore[arg-type]


def _signal_key(signal: Signal) -> tuple[datetime, SignalAction, str | None]:
    return signal.timestamp, signal.action, signal.external_signal_id


def test_cash_is_executable_and_remains_flat() -> None:
    bars = _bars(count=20)
    result = run_consolidated_benchmark(BenchmarkDefinition.cash(), bars, _config())

    assert result.definition.kind is BenchmarkKind.CASH
    assert result.report.signals_emitted == 0
    assert result.report.trades == []
    assert result.report.terminal_position is None
    assert result.report.metrics.final_equity == 100_000.0
    assert result.registration_metadata()["terminal_treatment"] == ("mark_to_market_unliquidated")
    registration_config = result.registration_metadata()["backtest_config"]
    assert isinstance(registration_config, dict)
    assert registration_config["fill_policy"]["policy_id"] == "next_open_bracket_v2"
    assert registration_config["execution_costs"]["scenario_name"] == "zero-cost-test"
    assert registration_config["size_pct"] > 0.0  # BacktestConfig invariant.
    assert registration_config["size_pct_execution_relevant"] is False
    assert "always-flat cash" in registration_config["size_pct_note"]


def test_buy_and_hold_decides_on_close_fills_next_open_and_marks_terminal() -> None:
    bars = _bars(count=8)
    definition = BenchmarkDefinition.buy_and_hold(exposure=0.95)
    result = run_consolidated_benchmark(definition, bars, _config())

    terminal = result.report.terminal_position
    assert result.report.signals_emitted == 1
    assert terminal is not None
    assert terminal.entry_ts == execution_bar_times(bars[1]).open_ts
    assert terminal.entry_price == bars[1].open
    assert terminal.quantity == pytest.approx(95_000.0 / bars[1].open)
    assert terminal.mark_price == bars[-1].close
    assert result.terminal_marked_to_market
    assert result.report.trades == []  # Terminal exposure is not silently liquidated.
    assert result.report.equity_curve[-1][1] == pytest.approx(
        100_000.0 + terminal.net_unrealized_pnl
    )


@pytest.mark.parametrize(
    "definition",
    [
        BenchmarkDefinition.sma_long_cash(period=5),
        BenchmarkDefinition.momentum_long_cash(lookback_bars=5),
    ],
)
def test_indicator_benchmarks_have_prefix_invariance_no_lookahead(
    definition: BenchmarkDefinition,
) -> None:
    states = _states(_bars(count=100))
    cutoff = 60
    full = build_benchmark_strategy(definition).run(states)
    prefix = build_benchmark_strategy(definition).run(states[:cutoff])

    cutoff_timestamp = states[cutoff - 1].timestamp
    assert [_signal_key(signal) for signal in full if signal.timestamp <= cutoff_timestamp] == [
        _signal_key(signal) for signal in prefix
    ]


def test_sma_entry_uses_the_open_after_its_completed_bar_decision() -> None:
    bars = _bars(count=80)
    definition = BenchmarkDefinition.sma_long_cash(period=5)
    signals = build_benchmark_strategy(definition).run(_states(bars))
    first_long = next(signal for signal in signals if signal.action is SignalAction.LONG)
    decision_index = next(
        index
        for index, bar in enumerate(bars)
        if execution_bar_times(bar).close_ts == first_long.timestamp
    )

    result = run_consolidated_benchmark(definition, bars, _config())
    first_entry_ts = (
        result.report.trades[0].entry_ts
        if result.report.trades
        else result.report.terminal_position.entry_ts  # type: ignore[union-attr]
    )
    assert first_entry_ts == execution_bar_times(bars[decision_index + 1]).open_ts


def test_flat_boundary_resets_bootstrap_model_state_but_preserves_timing() -> None:
    bars = _bars(count=12)
    evaluation_start = execution_bar_times(bars[5]).close_ts
    result = run_consolidated_benchmark(
        BenchmarkDefinition.buy_and_hold(),
        bars,
        _config(
            evaluation_start=evaluation_start,
            require_flat_model_boundary=True,
        ),
    )

    assert result.report.bootstrap_bars == 5
    assert result.report.flat_model_boundary_applied
    assert result.report.signals_emitted == 1
    assert result.report.terminal_position is not None
    assert result.report.terminal_position.entry_ts == execution_bar_times(bars[6]).open_ts


def test_sma_flat_boundary_preserves_bootstrapped_indicator_history() -> None:
    bars = _bars(count=100)
    period = 5
    evaluation_index = next(
        index
        for index in range(period, len(bars) - 1)
        if bars[index].close
        > sum(bar.close for bar in bars[index - period + 1 : index + 1]) / period
    )
    result = run_consolidated_benchmark(
        BenchmarkDefinition.sma_long_cash(period=period),
        bars,
        _config(
            evaluation_start=execution_bar_times(bars[evaluation_index]).close_ts,
            require_flat_model_boundary=True,
        ),
    )

    assert result.report.bootstrap_bars == evaluation_index
    assert result.report.signals_emitted >= 1
    first_entry = (
        result.report.trades[0].entry_ts
        if result.report.trades
        else result.report.terminal_position.entry_ts  # type: ignore[union-attr]
    )
    assert first_entry == execution_bar_times(bars[evaluation_index + 1]).open_ts


def test_volatility_target_uses_only_frozen_training_window_and_is_bounded() -> None:
    training = _bars(count=400)
    settings = VolatilityTargetConfig(
        maximum_lookback_bars=365,
        minimum_observations=60,
        target_annualized_volatility=0.05,
        exposure_floor=0.01,
        exposure_cap=0.10,
    )
    first = calibrate_fixed_volatility_exposure(
        training,
        settings,
        annualization_factor=365.0 * 24.0 * 60.0,
    )
    second = calibrate_fixed_volatility_exposure(
        training,
        settings,
        annualization_factor=365.0 * 24.0 * 60.0,
    )

    assert settings.exposure_floor <= first.fixed_exposure <= settings.exposure_cap <= 1.0
    assert first.training_end == training[-1].timestamp
    assert first.trailing_start == training[-366].timestamp
    assert first.observations_used == 365
    assert first.calibration_id == second.calibration_id
    assert first.training_data_hash == second.training_data_hash
    assert first.to_manifest_dict()["uses_evaluation_data"] is False
    assert first.to_manifest_dict()["config"] == {
        "maximum_lookback_bars": 365,
        "minimum_observations": 60,
        "target_annualized_volatility": 0.05,
        "exposure_floor": 0.01,
        "exposure_cap": 0.10,
    }

    definition = BenchmarkDefinition.volatility_targeted_buy_and_hold(first)
    assert definition.exposure == first.fixed_exposure
    assert definition.to_manifest_dict()["volatility_calibration"] == (first.to_manifest_dict())
    evaluation = _bars(count=440)[400:420]
    result = run_consolidated_benchmark(definition, evaluation, _config())
    assert result.report.config.size_pct == first.fixed_exposure
    assert result.report.terminal_position is not None
    assert result.report.terminal_position.quantity == pytest.approx(
        100_000.0 * first.fixed_exposure / evaluation[1].open
    )


def test_volatility_target_clamps_to_registered_exposure_floor_and_cap() -> None:
    training = _bars(count=40)
    floor_settings = VolatilityTargetConfig(
        maximum_lookback_bars=30,
        minimum_observations=10,
        target_annualized_volatility=1e-12,
        exposure_floor=0.04,
        exposure_cap=0.80,
    )
    cap_settings = VolatilityTargetConfig(
        maximum_lookback_bars=30,
        minimum_observations=10,
        target_annualized_volatility=100.0,
        exposure_floor=0.01,
        exposure_cap=0.10,
    )

    floor_result = calibrate_fixed_volatility_exposure(
        training,
        floor_settings,
        annualization_factor=365.0 * 24.0 * 60.0,
    )
    cap_result = calibrate_fixed_volatility_exposure(
        training,
        cap_settings,
        annualization_factor=365.0 * 24.0 * 60.0,
    )

    assert floor_result.fixed_exposure == floor_settings.exposure_floor
    assert cap_result.fixed_exposure == cap_settings.exposure_cap
    assert floor_result.observations_used == cap_result.observations_used == 30


def test_training_volatility_sizing_is_one_fixed_oos_exposure_without_lookahead() -> None:
    training = _bars(count=400)
    calibration = calibrate_fixed_volatility_exposure(
        training,
        VolatilityTargetConfig(
            maximum_lookback_bars=365,
            minimum_observations=60,
            target_annualized_volatility=0.05,
            exposure_floor=0.01,
            exposure_cap=0.05,
        ),
        annualization_factor=365.0 * 24.0 * 60.0,
    )
    policy = calibration.to_fixed_position_sizing_policy(
        policy_id="SIZE_TRAINING_VOL_TARGET_5PCT",
        maximum_position_pct=0.05,
        minimum_position_notional=1.0,
    )

    low_price = policy.calculate(
        PositionSizingRequest(
            price=10.0,
            equity=100_000.0,
            buying_power=100_000.0,
        )
    )
    shocked_future_price = policy.calculate(
        PositionSizingRequest(
            price=10_000.0,
            equity=100_000.0,
            buying_power=100_000.0,
        )
    )

    assert policy.method.value == "fixed_pct"
    assert policy.fixed_pct == calibration.fixed_exposure
    calibration_payload = calibration.to_manifest_dict()
    assert calibration_payload["training_data_hash"] == calibration.training_data_hash
    assert calibration.training_end == training[-1].timestamp
    assert calibration_payload["uses_evaluation_data"] is False
    # Evaluation price legitimately changes units, never the training-frozen
    # current-equity exposure or calibration identity.
    assert low_price.requested_notional_value == shocked_future_price.requested_notional_value
    assert low_price.requested_notional_value == pytest.approx(
        100_000.0 * calibration.fixed_exposure
    )


def test_volatility_target_rejects_short_or_unordered_training_history() -> None:
    bars = _bars(count=40)
    settings = VolatilityTargetConfig(
        maximum_lookback_bars=30,
        minimum_observations=20,
        target_annualized_volatility=0.20,
        exposure_floor=0.01,
    )
    available_history = calibrate_fixed_volatility_exposure(
        bars[:25],
        settings,
        annualization_factor=365.0,
    )
    assert available_history.observations_used == 24
    assert available_history.trailing_start == bars[0].timestamp
    with pytest.raises(ValueError, match="insufficient eligible training returns"):
        calibrate_fixed_volatility_exposure(
            bars[:20],
            settings,
            annualization_factor=365.0,
        )
    with pytest.raises(ValueError, match="strictly chronological"):
        calibrate_fixed_volatility_exposure(
            [*bars[:31][::-1]],
            settings,
            annualization_factor=365.0,
        )


def test_benchmark_and_signal_ids_are_content_stable() -> None:
    definition_a = BenchmarkDefinition.momentum_long_cash(lookback_bars=5)
    definition_b = BenchmarkDefinition.momentum_long_cash(lookback_bars=5)
    states = _states(_bars(count=60))
    signals_a = build_benchmark_strategy(definition_a).run(states)
    signals_b = build_benchmark_strategy(definition_b).run(states)

    assert definition_a.benchmark_id == definition_b.benchmark_id
    assert definition_a.to_manifest_dict() == definition_b.to_manifest_dict()
    assert signals_a
    assert [signal.external_signal_id for signal in signals_a] == [
        signal.external_signal_id for signal in signals_b
    ]
    assert all(signal.metadata["benchmark_id"] == definition_a.benchmark_id for signal in signals_a)


def test_benchmark_definition_fails_closed_on_invalid_semantics() -> None:
    with pytest.raises(ValueError, match="cash benchmark exposure"):
        BenchmarkDefinition(kind=BenchmarkKind.CASH, exposure=0.5)
    with pytest.raises(ValueError, match="lookback_bars"):
        BenchmarkDefinition(kind=BenchmarkKind.SMA_LONG_CASH, exposure=0.95)
    with pytest.raises(ValueError, match="no leverage"):
        VolatilityTargetConfig(
            maximum_lookback_bars=30,
            minimum_observations=20,
            target_annualized_volatility=0.20,
            exposure_floor=0.01,
            exposure_cap=1.01,
        )
    with pytest.raises(ValueError, match="must not exceed maximum"):
        VolatilityTargetConfig(
            maximum_lookback_bars=30,
            minimum_observations=31,
            target_annualized_volatility=0.20,
            exposure_floor=0.01,
            exposure_cap=0.10,
        )


def test_fixture_timestamp_contract_is_utc() -> None:
    # Guard against accidentally replacing the real frozen provider fixture
    # with local-time rows, which would invalidate next-open timing assertions.
    assert execution_bar_times(_bars(count=1)[0]).open_ts.tzinfo == UTC
