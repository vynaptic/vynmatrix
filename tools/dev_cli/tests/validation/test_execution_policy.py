"""Execution-policy tests over the frozen real Coinbase BTC-USD fixture."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from validation_helpers import bars_from_ohlcv, load_market_fixture

from dev_cli.validation.backtest.engine import BacktestConfig, BacktestEngine
from dev_cli.validation.backtest.execution import (
    ExecutionCostModel,
    FillPolicy,
    execution_bar_times,
    normalize_execution_bar,
)
from dev_cli.validation.backtest.simulator import FillSimulator, ModelExecutionDivergence
from dev_cli.validation.persistence.backtest_manifest_store import canonical_manifest_bytes
from dev_cli.validation.position_sizing import SIZING_EVIDENCE_SCHEMA, CoinbaseSizingRules
from lib_data.bars import Bar
from lib_strategy.position_sizing import PositionSizingPolicy
from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy
from lib_strategy.signals.signal import Signal, SignalAction

_FIXTURE_ROOT = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "market_data"
_FIXTURE = _FIXTURE_ROOT / "coinbase_btcusd_1m_2026-06-10.json"
_DAILY_FIXTURE = _FIXTURE_ROOT / "coinbase_btcusdc_1d_2026-06-10.json"


def _real_bars() -> list[Bar]:
    payload = load_market_fixture(_FIXTURE)
    return bars_from_ohlcv(
        payload["bars"],
        symbol="BTCUSD",
        source="coinbase_live",
    )


def _signal(
    bars: list[Bar],
    index: int,
    action: SignalAction,
    *,
    entry_price: float | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    reason: str | None = None,
) -> Signal:
    metadata = {"exit_reason": reason} if reason else {}
    return Signal(
        strategy_id="execution_policy_test",
        strategy_type="indicator",
        symbol=bars[index].symbol,
        action=action,
        confidence=1.0,
        timestamp=execution_bar_times(bars[index]).close_ts,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        metadata=metadata,
    )


def _zero_costs(*, latency_bars: int = 0) -> ExecutionCostModel:
    return ExecutionCostModel(
        scenario_name="zero-reference",
        commission_bps=0.0,
        latency_bars=latency_bars,
        historical_basis="backward-applied-scenario",
    )


def _reference_simulator(*, cost_model: ExecutionCostModel | None = None) -> FillSimulator:
    return FillSimulator(
        initial_capital=100_000.0,
        size_pct=0.95,
        cost_model=cost_model or _zero_costs(),
        fill_policy=FillPolicy.reference_v2(),
    )


def _coinbase_rules() -> CoinbaseSizingRules:
    return CoinbaseSizingRules(
        base_increment="0.00000001",
        quote_increment="0.01",
        minimum_order_notional=1.0,
        source="frozen_coinbase_product_metadata",
    )


def test_reference_policy_fills_market_decisions_only_at_next_open() -> None:
    bars = _real_bars()[:4]
    signals = [
        _signal(bars, 0, SignalAction.LONG, stop_loss=60_000.0),
        _signal(bars, 2, SignalAction.CLOSE, reason="trailing_stop"),
    ]
    result = _reference_simulator().run(bars, signals)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_reference_price == bars[1].open
    assert trade.exit_reference_price == bars[3].open
    assert trade.exit_reason == "trailing_stop"
    assert result.execution_policy_id == "next_open_bracket_v2"
    assert result.exit_reason_counts == {"trailing_stop": 1}
    assert result.holding_periods_bars == (2,)
    assert result.turnover_notional > 0.0
    assert result.exposure_seconds == 120.0


def test_fixed_binding_sizing_uses_fill_time_equity_and_next_open_price() -> None:
    bars = _real_bars()[:3]
    policy = PositionSizingPolicy.fixed_percent(
        policy_id="SIZE_CURRENT_BINDING_FIXED_EQUITY_1PCT",
        fixed_pct=0.01,
        max_position_pct=0.05,
        minimum_position_notional=1.0,
    )
    simulator = FillSimulator(
        initial_capital=100_000.0,
        size_pct=0.95,
        cost_model=_zero_costs(),
        fill_policy=FillPolicy.reference_v2(),
        sizing_policy=policy,
        coinbase_sizing_rules=_coinbase_rules(),
    )

    result = simulator.run(
        bars,
        [_signal(bars, 0, SignalAction.LONG, stop_loss=60_000.0)],
    )

    assert result.terminal_position is not None
    assert len(result.sizing_decisions) == 1
    evidence = result.sizing_decisions[0]
    assert evidence.fill_ts == execution_bar_times(bars[1]).open_ts
    assert evidence.reference_price == bars[1].open
    assert evidence.sizing.equity == 100_000.0
    assert evidence.sizing.requested_notional_value == 1_000.0
    assert evidence.sizing.quantity == result.terminal_position.quantity
    assert evidence.provider_translation is not None
    assert evidence.provider_translation.market_order_size is not None
    assert evidence.provider_translation.market_order_size.denomination.value == "quote_size"
    assert evidence.execution_outcome == "accepted"
    assert result.sizing_policy is policy


def test_sizing_ledger_separates_reference_risk_from_post_adverse_fill_risk() -> None:
    bars = _real_bars()[:3]
    stop = 60_000.0
    policy = PositionSizingPolicy.initial_stop_risk(
        policy_id="SIZE_INITIAL_STOP_RISK_1PCT",
        risk_pct=0.01,
        fixed_pct_fallback=0.01,
        max_position_pct=0.05,
        minimum_position_notional=1.0,
    )
    costs = ExecutionCostModel(
        scenario_name="adverse-fill-risk",
        commission_bps=0.0,
        half_spread_bps=10.0,
        slippage_bps=20.0,
        impact_bps=5.0,
        historical_basis="backward-applied-scenario",
    )

    result = FillSimulator(
        initial_capital=100_000.0,
        cost_model=costs,
        fill_policy=FillPolicy.reference_v2(),
        sizing_policy=policy,
        coinbase_sizing_rules=_coinbase_rules(),
    ).run(bars, [_signal(bars, 0, SignalAction.LONG, stop_loss=stop)])

    evidence = result.sizing_decisions[0]
    assert result.terminal_position is not None
    assert evidence.filled_quantity is not None
    assert evidence.post_fill_initial_stop_risk_amount is not None
    assert evidence.filled_quantity == result.terminal_position.quantity
    assert evidence.provider_translation is not None
    assert evidence.provider_translation.market_order_size is not None
    assert evidence.provider_translation.market_order_size.denomination.value == "quote_size"
    assert evidence.filled_quantity < evidence.provider_translation.executable_quantity_at_reference
    assert evidence.fill_notional_value == pytest.approx(
        float(evidence.provider_translation.market_order_size.amount)
    )
    assert evidence.fill_notional_value == pytest.approx(
        evidence.filled_quantity * evidence.execution_price
    )
    assert result.turnover_notional == pytest.approx(evidence.fill_notional_value)
    assert evidence.post_fill_initial_stop_risk_amount == pytest.approx(
        abs(evidence.execution_price - stop) * evidence.filled_quantity
    )
    assert evidence.post_fill_initial_stop_risk_pct == pytest.approx(
        evidence.post_fill_initial_stop_risk_amount / evidence.sizing.equity
    )
    assert evidence.post_fill_initial_stop_risk_amount > (
        evidence.provider_translation.effective_sizing_risk_amount
    )


def test_coinbase_precision_rejection_remains_separate_from_domain_sizing() -> None:
    bars = _real_bars()[:3]
    policy = PositionSizingPolicy.fixed_percent(
        policy_id="provider-boundary",
        fixed_pct=0.000011,
        max_position_pct=0.05,
        minimum_position_notional=1.0,
    )
    rules = CoinbaseSizingRules(
        base_increment="1",
        quote_increment="0.01",
        minimum_order_notional=1.0,
        source="coarse_test_product_metadata",
    )

    result = FillSimulator(
        initial_capital=100_000.0,
        cost_model=_zero_costs(),
        fill_policy=FillPolicy.reference_v2(),
        sizing_policy=policy,
        coinbase_sizing_rules=rules,
    ).run(bars, [_signal(bars, 0, SignalAction.SHORT, stop_loss=70_000.0)])

    evidence = result.sizing_decisions[0]
    assert evidence.sizing.accepted is True
    assert evidence.provider_translation is not None
    assert evidence.provider_translation.rejection_reason == "quantized_to_zero"
    assert evidence.execution_outcome == "rejected"
    assert result.terminal_position is None


def test_initial_stop_risk_sizing_is_frozen_before_later_gap_stop() -> None:
    # The same observed Coinbase gap used by the protective-order test. Sizing
    # sees only the next-open entry and the emitted initial stop; the later gap
    # changes exit PnL, never the already-frozen quantity.
    bars = _real_bars()[14:19]
    stop = 61_699.60
    policy = PositionSizingPolicy.initial_stop_risk(
        policy_id="SIZE_INITIAL_STOP_RISK_1PCT",
        risk_pct=0.01,
        fixed_pct_fallback=0.01,
        max_position_pct=0.05,
        minimum_position_notional=1.0,
    )
    result = FillSimulator(
        initial_capital=100_000.0,
        size_pct=0.95,
        cost_model=_zero_costs(),
        fill_policy=FillPolicy.reference_v2(),
        sizing_policy=policy,
        coinbase_sizing_rules=_coinbase_rules(),
    ).run(bars, [_signal(bars, 0, SignalAction.LONG, stop_loss=stop)])

    assert len(result.sizing_decisions) == 1
    sizing = result.sizing_decisions[0].sizing
    assert sizing.price == bars[1].open
    assert sizing.stop_loss == stop
    assert sizing.capped_by_max_position is True
    assert sizing.notional_value <= 5_000.0
    assert len(result.trades) == 1
    assert result.trades[0].quantity == sizing.quantity
    assert result.trades[0].exit_reason == "stop_loss_gap"
    assert result.trades[0].exit_reference_price == 61_699.30


def test_execution_costs_are_adverse_and_decomposed() -> None:
    bars = _real_bars()[:4]
    signals = [
        _signal(bars, 0, SignalAction.LONG),
        _signal(bars, 2, SignalAction.CLOSE),
    ]
    zero = _reference_simulator().run(bars, signals)
    costs = ExecutionCostModel(
        scenario_name="expected",
        commission_bps=8.0,
        half_spread_bps=2.0,
        slippage_bps=3.0,
        impact_bps=1.0,
        historical_basis="backward-applied-scenario",
    )
    charged = _reference_simulator(cost_model=costs).run(bars, signals)

    assert charged.trades[0].entry_price > charged.trades[0].entry_reference_price
    assert charged.trades[0].exit_price < charged.trades[0].exit_reference_price
    assert charged.trades[0].pnl < zero.trades[0].pnl
    assert charged.cost_breakdown.commission > 0.0
    assert charged.cost_breakdown.half_spread > 0.0
    assert charged.cost_breakdown.slippage > 0.0
    assert charged.cost_breakdown.impact > 0.0
    assert charged.trades[0].gross_pnl - charged.trades[0].fees == pytest.approx(
        charged.trades[0].pnl
    )


def test_real_observed_gap_through_stop_fills_at_open() -> None:
    # The frozen Coinbase fixture has an observed down gap from bar 16's low
    # (61699.83) to bar 17's open (61699.30).  The stop is not touched earlier.
    bars = _real_bars()[14:19]
    stop = 61_699.60
    entry = _signal(bars, 0, SignalAction.LONG, stop_loss=stop)
    result = _reference_simulator().run(bars, [entry])

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "stop_loss_gap"
    assert result.trades[0].exit_reference_price == 61_699.30
    assert result.trades[0].exit_reference_price < stop
    assert result.protective_stop_exits == 1
    assert len(result.model_execution_divergences) == 1


def test_same_bar_stop_and_target_is_conservatively_stop_first() -> None:
    bars = _real_bars()[:3]
    # Entry fills at bar 1 open 61686.73.  Its actual range touches both levels.
    entry = _signal(
        bars,
        0,
        SignalAction.LONG,
        stop_loss=61_670.0,
        take_profit=61_690.0,
    )
    result = _reference_simulator().run(bars, [entry])

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "stop_loss"
    assert result.trades[0].exit_reference_price == 61_670.0
    assert result.ambiguous_bars == 1
    assert result.trades[0].exit_ts - result.trades[0].entry_ts == timedelta(minutes=1)
    assert result.exposure_seconds == 60.0


def test_post_gap_invalid_entry_bracket_is_rejected_fail_closed() -> None:
    bars = _real_bars()[63:67]
    # The real next open gaps above this target, making it invalid after fill.
    entry = _signal(
        bars,
        0,
        SignalAction.LONG,
        entry_price=bars[0].close,
        stop_loss=61_000.0,
        take_profit=61_879.0,
    )
    result = _reference_simulator().run(bars, [entry])

    assert bars[1].open > 61_879.0
    assert result.rejected_entries == 1
    assert result.trades == []
    assert result.terminal_position is None
    assert result.turnover_notional == 0.0


def test_rejection_divergence_stays_open_until_retry_actually_fills() -> None:
    bars = _real_bars()[63:69]
    rejected = _signal(
        bars,
        0,
        SignalAction.LONG,
        stop_loss=61_000.0,
        take_profit=61_879.0,
    )
    retry = _signal(bars, 2, SignalAction.LONG)
    result = _reference_simulator().run(bars, [rejected, retry])

    assert result.rejected_entries == 1
    assert result.terminal_position is not None
    assert len(result.model_execution_divergences) == 1
    divergence = result.model_execution_divergences[0]
    assert divergence.cause == "invalid_entry_bracket"
    assert divergence.start_ts == execution_bar_times(bars[1]).open_ts
    assert divergence.end_ts == execution_bar_times(bars[3]).open_ts


def test_close_cancels_a_latency_delayed_unfilled_entry() -> None:
    bars = _real_bars()[:5]
    signals = [
        _signal(bars, 0, SignalAction.LONG),
        _signal(bars, 1, SignalAction.CLOSE),
    ]
    result = _reference_simulator(cost_model=_zero_costs(latency_bars=2)).run(bars, signals)

    assert result.cancelled_signals == 1
    assert result.unfilled_signals == 0
    assert result.trades == []
    assert result.terminal_position is None
    assert not result.model_execution_divergences


def test_same_side_reentry_cannot_hide_stale_execution_bracket_lineage() -> None:
    bars = _real_bars()[:7]
    signals = [
        _signal(bars, 0, SignalAction.LONG, take_profit=61_820.0),
        _signal(bars, 2, SignalAction.CLOSE),
        _signal(bars, 3, SignalAction.LONG, take_profit=62_000.0),
        _signal(bars, 5, SignalAction.CLOSE),
    ]
    result = _reference_simulator(cost_model=_zero_costs(latency_bars=1)).run(
        bars,
        signals,
    )

    assert result.cancelled_signals == 1
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "take_profit"
    assert result.trades[0].exit_reference_price == 61_820.0
    lineage = [
        item
        for item in result.model_execution_divergences
        if item.cause == "position_lineage_mismatch"
    ]
    assert len(lineage) == 1
    assert lineage[0].end_ts is not None


def test_terminal_position_is_marked_not_silently_closed() -> None:
    bars = _real_bars()[:3]
    entry = _signal(bars, 1, SignalAction.LONG, stop_loss=60_000.0)
    result = _reference_simulator().run(bars, [entry])

    assert result.trades == []
    assert result.terminal_position is not None
    assert result.terminal_position.entry_price == bars[2].open
    assert result.terminal_position.entry_ts == execution_bar_times(bars[2]).open_ts
    assert result.terminal_position.mark_price == bars[2].close
    assert result.terminal_position.stop_loss == 60_000.0
    assert result.equity_curve[-1][1] == pytest.approx(
        100_000.0 + result.terminal_position.net_unrealized_pnl
    )
    assert result.closed_gross_pnl == 0.0
    assert result.gross_pnl == result.terminal_gross_pnl
    assert result.turnover_notional > result.closed_turnover_notional == 0.0
    assert result.equity_curve[-1][1] - 100_000.0 == pytest.approx(
        result.gross_pnl - result.cost_breakdown.total
    )
    assert result.exposure_seconds == 60.0


def test_signal_ledger_counts_unmatched_ignored_and_unfilled() -> None:
    bars = _real_bars()[:2]
    duplicate_close = bars[0].timestamp
    signals = [
        _signal(bars, 0, SignalAction.HOLD),
        _signal(bars, 0, SignalAction.LONG),
        _signal(bars, 0, SignalAction.LONG),
        replace(
            _signal(bars, 1, SignalAction.CLOSE),
            timestamp=duplicate_close - timedelta(minutes=1),
        ),
    ]
    result = _reference_simulator().run(bars, signals)

    assert result.unmatched_signals == 1
    assert result.ignored_signals == 2
    assert result.unfilled_signals == 0
    assert result.terminal_position is not None


def test_reference_policy_retains_last_bar_decision_as_unfilled() -> None:
    bars = _real_bars()[:2]
    result = _reference_simulator().run(
        bars,
        [_signal(bars, 1, SignalAction.LONG)],
    )

    assert result.unfilled_signals == 1
    assert result.trades == []
    assert result.terminal_position is None


class _MetadataWarmupStrategy(PureSignalStrategy):
    def __init__(self) -> None:
        super().__init__(strategy_id="metadata_warmup", strategy_type="indicator")

    @property
    def warmup_bars_needed(self) -> int:
        return 2

    def initialize(self) -> None:
        self.seen: list[MarketState] = []
        self._entered = False

    def on_data(self, state: MarketState) -> None:
        self.seen.append(state)
        if self.warmup_complete(state.symbol) and not self._bootstrapping and not self._entered:
            self.emit_long(
                state.symbol,
                state.close,
                timestamp=state.timestamp,
                stop_loss=60_000.0,
            )
            self._entered = True


class _FlatBoundaryWarmupStrategy(_MetadataWarmupStrategy):
    @property
    def supports_flat_model_boundary(self) -> bool:
        return True

    def reset_model_to_flat_for_evaluation(self, symbols: tuple[str, ...]) -> None:
        self.boundary_symbols = symbols
        self.bootstrap_observations_at_boundary = len(self.seen)
        self._entered = False


def test_half_open_evaluation_bootstraps_without_trading_and_populates_metadata() -> None:
    bars = _real_bars()[:6]
    strategy = _MetadataWarmupStrategy()
    config = BacktestConfig(
        symbol="BTCUSD",
        consolidation_minutes=0,
        fee_bps=0.0,
        evaluation_start=execution_bar_times(bars[2]).close_ts,
        evaluation_end=execution_bar_times(bars[6 - 1]).close_ts,
        price_source="coinbase_live",
        asset_class="crypto",
        requested_product="BTC-USDC",
        canonical_product="BTC-USD",
        fill_policy=FillPolicy.reference_v2(),
    )
    for bar in bars:
        bar.metadata.update(
            {
                "asset_class": "crypto",
                "requested_product": "BTC-USDC",
                "canonical_product": "BTC-USD",
            }
        )
    report = BacktestEngine().run_consolidated(strategy, bars, config)

    assert report.bootstrap_bars == 2
    assert report.consolidated_bars == 3
    assert report.equity_curve[0][0] == execution_bar_times(bars[2]).close_ts
    assert report.equity_curve[-1][0] == execution_bar_times(bars[4]).close_ts
    assert report.signals_emitted == 1
    assert report.terminal_position is not None
    metadata = strategy.seen[-1].metadata
    assert metadata["source"] == "coinbase_live"
    assert metadata["price_source"] == "coinbase_live"
    assert metadata["timeframe"] == "1m"
    assert metadata["price_timeframe"] == "1m"
    assert metadata["coverage"] == 1.0
    assert metadata["asset_class"] == "crypto"
    assert metadata["requested_product"] == "BTC-USDC"
    assert metadata["canonical_product"] == "BTC-USD"


def test_frozen_signal_evidence_replay_changes_only_registered_sizing() -> None:
    bars = _real_bars()[:6]
    source_config = BacktestConfig(
        symbol="BTCUSD",
        consolidation_minutes=0,
        size_pct=0.10,
        fee_bps=0.0,
        evaluation_start=execution_bar_times(bars[2]).close_ts,
        evaluation_end=execution_bar_times(bars[5]).close_ts,
        fill_policy=FillPolicy.reference_v2(),
        require_flat_model_boundary=True,
    )
    source = BacktestEngine().run_consolidated(
        _FlatBoundaryWarmupStrategy(),
        bars,
        source_config,
    )
    sizing = PositionSizingPolicy.fixed_percent(
        policy_id="SIZE_CURRENT_BINDING_FIXED_EQUITY_1PCT",
        fixed_pct=0.01,
        max_position_pct=0.05,
        minimum_position_notional=1.0,
    )

    replay = BacktestEngine().replay_consolidated_signal_evidence(
        bars,
        source.raw_signal_ledger,
        replace(
            source_config,
            sizing_policy=sizing,
            coinbase_sizing_rules=_coinbase_rules(),
        ),
        source_bootstrap_bars=source.bootstrap_bars,
        source_flat_model_boundary_applied=source.flat_model_boundary_applied,
    )

    assert replay.raw_signal_ledger == source.raw_signal_ledger
    assert replay.signals_emitted == source.signals_emitted == 1
    assert replay.sizing_policy is sizing
    assert replay.terminal_position is not None
    assert source.terminal_position is not None
    assert replay.terminal_position.quantity < source.terminal_position.quantity
    assert replay.bootstrap_bars == source.bootstrap_bars == 2
    assert replay.flat_model_boundary_applied is True

    duplicate = source.raw_signal_ledger + source.raw_signal_ledger
    with pytest.raises(ValueError, match="external_signal_id values must be unique"):
        BacktestEngine().replay_consolidated_signal_evidence(
            bars,
            duplicate,
            replace(
                source_config,
                sizing_policy=sizing,
                coinbase_sizing_rules=_coinbase_rules(),
            ),
            source_bootstrap_bars=source.bootstrap_bars,
            source_flat_model_boundary_applied=True,
        )

    with pytest.raises(ValueError, match="source bootstrap count differs"):
        BacktestEngine().replay_consolidated_signal_evidence(
            bars,
            source.raw_signal_ledger,
            replace(
                source_config,
                sizing_policy=sizing,
                coinbase_sizing_rules=_coinbase_rules(),
            ),
            source_bootstrap_bars=source.bootstrap_bars - 1,
            source_flat_model_boundary_applied=True,
        )


def test_backtest_report_contains_strict_json_safe_sizing_ledgers() -> None:
    bars = _real_bars()[:6]
    policy = PositionSizingPolicy.fixed_percent(
        policy_id="SIZE_CURRENT_BINDING_FIXED_EQUITY_1PCT",
        fixed_pct=0.01,
        max_position_pct=0.05,
        minimum_position_notional=1.0,
    )
    report = BacktestEngine().run_consolidated(
        _MetadataWarmupStrategy(),
        bars,
        BacktestConfig(
            symbol="BTCUSD",
            consolidation_minutes=0,
            fee_bps=0.0,
            fill_policy=FillPolicy.reference_v2(),
            sizing_policy=policy,
            coinbase_sizing_rules=_coinbase_rules(),
        ),
    )

    payload = report.to_dict()
    encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
    canonical_manifest_bytes({"payload": payload})

    assert payload["sizing_policy"] == policy.to_dict()
    assert payload["sizing_evidence_schema"] == SIZING_EVIDENCE_SCHEMA
    assert payload["coinbase_sizing_rules"] == _coinbase_rules().to_dict()
    assert len(payload["sizing_decision_ledger"]) == 1
    assert payload["sizing_decision_ledger"][0]["sizing"]["policy_id"] == policy.policy_id
    assert payload["sizing_decision_ledger"][0]["sizing_evidence_schema"] == SIZING_EVIDENCE_SCHEMA
    assert payload["sizing_decision_ledger"][0]["provider_translation"] is not None
    assert "NaN" not in encoded


def test_sizing_ledger_identity_is_deterministic_without_external_signal_id() -> None:
    bars = _real_bars()[:3]
    policy = PositionSizingPolicy.fixed_percent(
        policy_id="SIZE_CURRENT_BINDING_FIXED_EQUITY_1PCT",
        fixed_pct=0.01,
        max_position_pct=0.05,
        minimum_position_notional=1.0,
    )

    def _run() -> str:
        result = FillSimulator(
            initial_capital=100_000.0,
            cost_model=_zero_costs(),
            fill_policy=FillPolicy.reference_v2(),
            sizing_policy=policy,
            coinbase_sizing_rules=_coinbase_rules(),
        ).run(bars, [_signal(bars, 0, SignalAction.LONG, stop_loss=60_000.0)])
        return result.sizing_decisions[0].signal_id

    assert _run() == _run()


def test_evaluation_fails_when_non_trading_warmup_is_short() -> None:
    bars = _real_bars()[:4]
    config = BacktestConfig(
        symbol="BTCUSD",
        consolidation_minutes=0,
        evaluation_start=execution_bar_times(bars[1]).close_ts,
        fill_policy=FillPolicy.reference_v2(),
    )
    with pytest.raises(ValueError, match="required 2 bars, observed 1"):
        BacktestEngine().run_consolidated(_MetadataWarmupStrategy(), bars, config)


def test_required_flat_model_boundary_fails_closed_for_unaware_strategy() -> None:
    bars = _real_bars()[:6]
    config = BacktestConfig(
        symbol="BTCUSD",
        consolidation_minutes=0,
        evaluation_start=execution_bar_times(bars[2]).close_ts,
        require_flat_model_boundary=True,
    )

    with pytest.raises(ValueError, match="does not support a flat model boundary"):
        BacktestEngine().run_consolidated(_MetadataWarmupStrategy(), bars, config)

    assert BacktestEngine.validation_capabilities == {
        "flat_model_boundary": True,
        "purged_event_overlap": True,
        "embargoed_selection": True,
    }


def test_flat_model_boundary_runs_after_bootstrap_and_is_reported() -> None:
    bars = _real_bars()[:6]
    strategy = _FlatBoundaryWarmupStrategy()
    config = BacktestConfig(
        symbol="BTCUSD",
        consolidation_minutes=0,
        evaluation_start=execution_bar_times(bars[2]).close_ts,
        evaluation_end=execution_bar_times(bars[5]).close_ts,
        require_flat_model_boundary=True,
    )

    report = BacktestEngine().run_consolidated(strategy, bars, config)

    assert strategy.bootstrap_observations_at_boundary == 2
    assert strategy.boundary_symbols == ("BTCUSD",)
    assert report.flat_model_boundary_applied is True
    assert report.to_dict()["flat_model_boundary_applied"] is True


def test_bar_coverage_is_enforced() -> None:
    bars = _real_bars()[:3]
    bars[1].metadata["coverage"] = 0.5
    config = BacktestConfig(
        symbol="BTCUSD",
        consolidation_minutes=0,
        min_bar_coverage=0.95,
    )
    with pytest.raises(ValueError, match="below configured minimum"):
        BacktestEngine().run_consolidated(_MetadataWarmupStrategy(), bars, config)


def test_consolidation_preserves_fixed_product_lineage_metadata() -> None:
    bars = _real_bars()[:15]
    for bar in bars:
        bar.metadata.update(
            {
                "asset_class": "crypto",
                "requested_product": "BTC-USDC",
                "canonical_product": "BTC-USD",
            }
        )

    consolidated = BacktestEngine().consolidate(bars, period_minutes=15)

    assert len(consolidated) == 1
    assert consolidated[0].metadata["asset_class"] == "crypto"
    assert consolidated[0].metadata["requested_product"] == "BTC-USDC"
    assert consolidated[0].metadata["canonical_product"] == "BTC-USD"
    assert consolidated[0].metadata["coverage"] == 1.0


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda bars: [bars[0], replace(bars[1], timestamp=bars[0].timestamp)],
            "duplicate bar timestamp",
        ),
        (lambda bars: [bars[1], bars[0]], "strictly chronological"),
        (
            lambda bars: [bars[0], replace(bars[1], symbol="ETHUSD")],
            "inconsistent symbols",
        ),
        (
            lambda bars: [bars[0], replace(bars[1], source="other")],
            "inconsistent sources",
        ),
        (
            lambda bars: [bars[0], replace(bars[1], close=float("nan"))],
            "non-finite OHLCV",
        ),
        (
            lambda bars: [bars[0], replace(bars[1], high=bars[1].low - 1.0)],
            "violates OHLC bounds",
        ),
    ],
)
def test_bar_quality_validation_is_fail_closed(mutator, message: str) -> None:  # type: ignore[no-untyped-def]
    bars = mutator(_real_bars()[:2])
    with pytest.raises(ValueError, match=message):
        BacktestEngine().consolidate(bars, period_minutes=0)


def test_execution_value_objects_reject_ambiguous_or_non_finite_inputs() -> None:
    with pytest.raises(ValueError, match="commission_bps"):
        ExecutionCostModel(commission_bps=float("nan"))
    with pytest.raises(ValueError, match="latency_bars"):
        ExecutionCostModel(latency_bars=-1)
    with pytest.raises(ValueError, match="rejection_assumption is not implemented"):
        ExecutionCostModel(rejection_assumption="random_5pct")
    with pytest.raises(ValueError, match="partial_fill_assumption is not implemented"):
        ExecutionCostModel(partial_fill_assumption="half_fill")
    with pytest.raises(ValueError, match="stop-first"):
        FillPolicy(
            policy_id="unsafe",
            protective_orders=True,
            gap_through_at_open=True,
            ambiguity_policy="ignore",
            invalid_bracket_policy="reject_entry",
        )
    with pytest.raises(ValueError, match="execution_model is not implemented"):
        FillPolicy(execution_model="simultaneous_two_sided_quotes")
    with pytest.raises(ValueError, match="annualization_factor"):
        BacktestConfig(symbol="BTCUSD", annualization_factor=0.0)


def test_real_direct_daily_and_minute_consolidation_share_execution_interval() -> None:
    minute_payload = load_market_fixture(_FIXTURE)
    daily_payload = load_market_fixture(_DAILY_FIXTURE)
    minute_bars = bars_from_ohlcv(
        minute_payload["bars"],
        symbol="BTC-USDC",
        source=str(minute_payload["source"]),
    )
    consolidated = BacktestEngine().consolidate(
        minute_bars,
        period_minutes=1440,
        min_coverage=1.0,
    )[0]
    daily_row = daily_payload["bar"]
    direct_daily = Bar(
        symbol="BTC-USDC",
        timestamp=datetime.fromtimestamp(daily_row["start"], tz=UTC),
        open=float(daily_row["open"]),
        high=float(daily_row["high"]),
        low=float(daily_row["low"]),
        close=float(daily_row["close"]),
        volume=float(daily_row["volume"]),
        timeframe="1d",
        source=str(daily_payload["source"]),
        metadata={"timestamp_semantics": "period_start"},
    )

    direct = normalize_execution_bar(direct_daily)
    direct_times = execution_bar_times(direct)
    consolidated_times = execution_bar_times(consolidated)
    assert direct_times == consolidated_times
    assert direct.timestamp == consolidated.timestamp == datetime(2026, 6, 11, tzinfo=UTC)
    assert direct_times.open_ts == datetime(2026, 6, 10, tzinfo=UTC)
    assert direct_times.close_ts == datetime(2026, 6, 11, tzinfo=UTC)
    assert (direct.open, direct.high, direct.low, direct.close) == pytest.approx(
        (consolidated.open, consolidated.high, consolidated.low, consolidated.close)
    )


def test_reference_execution_rejects_ambiguous_timestamp_semantics() -> None:
    bar = replace(_real_bars()[0], metadata={})
    with pytest.raises(ValueError, match="timestamp_semantics"):
        _reference_simulator().run([bar], [])


def test_reference_execution_requires_authoritative_session_daily_boundaries() -> None:
    source = _real_bars()[0]
    inferred_daily = replace(
        source,
        timeframe="1d",
        metadata={"timestamp_semantics": "period_start", "asset_class": "equity"},
    )
    with pytest.raises(ValueError, match="authoritative bar open/close"):
        execution_bar_times(inferred_daily)

    explicit_daily = replace(
        inferred_daily,
        metadata={
            **inferred_daily.metadata,
            "open_timestamp": source.timestamp.isoformat(),
            "close_timestamp": (source.timestamp + timedelta(hours=6, minutes=30)).isoformat(),
        },
    )
    assert execution_bar_times(explicit_daily).close_ts == source.timestamp + timedelta(
        hours=6,
        minutes=30,
    )


def test_full_allocation_with_costs_fails_buying_power_closed() -> None:
    bars = _real_bars()[:3]
    costs = ExecutionCostModel(
        scenario_name="costly",
        commission_bps=10.0,
        half_spread_bps=2.0,
        historical_basis="backward-applied-scenario",
    )
    simulator = FillSimulator(
        initial_capital=100_000.0,
        size_pct=1.0,
        cost_model=costs,
        fill_policy=FillPolicy.reference_v2(),
    )
    result = simulator.run(bars, [_signal(bars, 0, SignalAction.LONG)])

    assert result.buying_power_rejections == 1
    assert result.rejected_entries == 1
    assert result.terminal_position is None
    assert result.turnover_notional == 0.0
    assert result.model_execution_divergences[0].cause == "insufficient_buying_power"


def test_policy_sizing_retains_post_cost_buying_power_rejection_evidence() -> None:
    bars = _real_bars()[:3]
    costs = ExecutionCostModel(
        scenario_name="costly-policy",
        commission_bps=10.0,
        half_spread_bps=2.0,
        historical_basis="backward-applied-scenario",
    )
    policy = PositionSizingPolicy.fixed_percent(
        policy_id="full-allocation-stress",
        fixed_pct=1.0,
        max_position_pct=1.0,
        minimum_position_notional=1.0,
    )
    result = FillSimulator(
        initial_capital=100_000.0,
        size_pct=0.01,
        cost_model=costs,
        fill_policy=FillPolicy.reference_v2(),
        sizing_policy=policy,
        coinbase_sizing_rules=_coinbase_rules(),
    ).run(bars, [_signal(bars, 0, SignalAction.LONG)])

    assert result.buying_power_rejections == 1
    assert result.rejected_entries == 1
    assert len(result.sizing_decisions) == 1
    assert result.sizing_decisions[0].execution_outcome == "rejected"
    assert (
        result.sizing_decisions[0].execution_rejection_reason
        == "insufficient_buying_power_after_costs"
    )


def test_real_adverse_short_path_marks_account_ruin() -> None:
    # This real Coinbase interval rises from 60,906.18 to 62,615.79.  An
    # intentionally extreme registered stress cost makes the short round trip
    # insolvent; the simulator must flag ruin instead of permitting a later
    # negative-quantity position.
    bars = _real_bars()[560:956]
    costs = ExecutionCostModel(
        scenario_name="account-ruin-stress",
        commission_bps=0.0,
        half_spread_bps=9_950.0,
        historical_basis="backward-applied-scenario",
    )
    simulator = FillSimulator(
        initial_capital=100_000.0,
        size_pct=0.50125,
        cost_model=costs,
        fill_policy=FillPolicy.reference_v2(),
    )
    result = simulator.run(
        bars,
        [
            _signal(bars, 0, SignalAction.SHORT),
            _signal(bars, 393, SignalAction.CLOSE),
        ],
    )

    assert len(result.trades) == 1
    assert result.trades[0].entry_reference_price == 60_906.18
    assert result.trades[0].exit_reference_price == 62_615.79
    assert result.equity_curve[-1][1] < 0.0
    assert result.account_ruined is True


def test_report_with_terminal_position_is_ineligible_for_parameter_selection() -> None:
    bars = _real_bars()[:5]
    report = BacktestEngine().run_consolidated(
        _MetadataWarmupStrategy(),
        bars,
        BacktestConfig(
            symbol="BTCUSD",
            consolidation_minutes=0,
            fee_bps=0.0,
            fill_policy=FillPolicy.reference_v2(),
        ),
    )

    assert report.terminal_position is not None
    assert report.selection_eligible is False
    assert report.selection_blockers == ("unresolved_terminal_position",)
    with pytest.raises(ValueError, match="unresolved_terminal_position"):
        report.require_selection_eligible()


def test_unresolved_execution_divergence_is_ineligible_for_selection() -> None:
    bars = _real_bars()[63:67]
    strategy = _MetadataWarmupStrategy()
    report = BacktestEngine().run_consolidated(
        strategy,
        bars,
        BacktestConfig(
            symbol="BTCUSD",
            consolidation_minutes=0,
            size_pct=1.0,
            execution_costs=ExecutionCostModel(
                scenario_name="costly",
                commission_bps=10.0,
                historical_basis="backward-applied-scenario",
            ),
            fill_policy=FillPolicy.reference_v2(),
        ),
    )

    assert report.terminal_position is None
    assert report.buying_power_rejections == 1
    assert report.selection_blockers == ("unresolved_model_execution_divergence",)
    with pytest.raises(ValueError, match="unresolved_model_execution_divergence"):
        report.require_selection_eligible()


def test_resolved_position_lineage_divergence_is_ineligible_for_selection() -> None:
    bars = _real_bars()[:5]
    report = BacktestEngine().run_consolidated(
        _MetadataWarmupStrategy(),
        bars,
        BacktestConfig(
            symbol="BTCUSD",
            consolidation_minutes=0,
            fee_bps=0.0,
            fill_policy=FillPolicy.reference_v2(),
        ),
    )
    timestamp = execution_bar_times(bars[2]).close_ts
    report = replace(
        report,
        terminal_position=None,
        model_execution_divergences=(
            ModelExecutionDivergence(
                start_ts=timestamp,
                end_ts=timestamp,
                model_position=1,
                execution_position=1,
                cause="position_lineage_mismatch",
            ),
        ),
    )

    assert report.selection_blockers == ("model_execution_position_lineage_divergence",)
    with pytest.raises(ValueError, match="model_execution_position_lineage_divergence"):
        report.require_selection_eligible()


def test_complete_lineage_is_required_and_partial_lineage_is_rejected() -> None:
    bars = _real_bars()[:3]
    config = BacktestConfig(
        symbol="BTCUSD",
        consolidation_minutes=0,
        asset_class="crypto",
        requested_product="BTC-USDC",
        canonical_product="BTC-USD",
    )
    with pytest.raises(ValueError, match="requires complete bar lineage"):
        BacktestEngine().run_consolidated(_MetadataWarmupStrategy(), bars, config)

    bars[0].metadata.update(
        {
            "asset_class": "crypto",
            "requested_product": "BTC-USDC",
            "canonical_product": "BTC-USD",
        }
    )
    with pytest.raises(ValueError, match="incomplete asset_class lineage"):
        BacktestEngine().run_consolidated(_MetadataWarmupStrategy(), bars, config)
