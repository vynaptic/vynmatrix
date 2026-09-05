"""Deterministic, versioned directional validation fill simulation.

Every run uses an explicit bar-interval contract. A signal at a completed bar
fills at the next bar open, the configured cost model is applied, and the
current policy handles fixed entry brackets, gaps, same-bar ambiguity, latency,
terminal exposure, and model/execution divergence without adding a
research-only signal implementation.

The simulator maintains one net position per run. It is not an order-book,
queue-position, simultaneous-quote, or inventory-skew simulator and therefore
must not be used as evidence for market-making strategies.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from dev_cli.validation.position_sizing import (
    SIZING_EVIDENCE_SCHEMA,
    CoinbaseSizingRules,
    CoinbaseSizingTranslation,
    apply_coinbase_sizing_rules,
    validate_sizing_boundary,
)
from lib_data.bars import Bar
from lib_infrastructure.brokers.adapters.coinbase_order_sizing import MarketOrderSide
from lib_strategy.position_sizing import (
    PositionSizingDecision,
    PositionSizingPolicy,
    PositionSizingRequest,
)
from lib_strategy.signals.signal import Signal, SignalAction
from lib_strategy.signals.utils import compute_stable_signal_id

from .execution import (
    CostBreakdown,
    ExecutionCostModel,
    FillPolicy,
    adverse_execution_price,
    execution_bar_times,
    execution_fill_costs,
    normalize_execution_bar,
)

# Signal action -> desired net position. HOLD leaves the position unchanged.
_DESIRED_POSITION: dict[SignalAction, int | None] = {
    SignalAction.LONG: 1,
    SignalAction.SHORT: -1,
    SignalAction.CLOSE: 0,
    SignalAction.HOLD: None,
}


@dataclass(frozen=True)
class Trade:
    """A closed round-trip position."""

    symbol: str
    side: str  # "long" | "short"
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    quantity: float
    fees: float
    pnl: float  # net of all decomposed execution costs
    gross_pnl: float = 0.0
    exit_reason: str = "model_close"
    holding_bars: int = 0
    entry_reference_price: float | None = None
    exit_reference_price: float | None = None
    cost_breakdown: CostBreakdown = field(default_factory=CostBreakdown)


@dataclass(frozen=True)
class TerminalPosition:
    """Marked, deliberately unliquidated exposure at the dataset boundary."""

    symbol: str
    side: str
    quantity: float
    entry_ts: datetime
    entry_price: float
    mark_price: float
    gross_unrealized_pnl: float
    net_unrealized_pnl: float
    stop_loss: float | None
    take_profit: float | None
    holding_bars: int
    paid_costs: CostBreakdown = field(default_factory=CostBreakdown)

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "entry_ts": self.entry_ts.isoformat(),
            "entry_price": self.entry_price,
            "mark_price": self.mark_price,
            "gross_unrealized_pnl": self.gross_unrealized_pnl,
            "net_unrealized_pnl": self.net_unrealized_pnl,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "holding_bars": self.holding_bars,
            "paid_costs": self.paid_costs.to_dict(),
        }


@dataclass(frozen=True)
class ModelExecutionDivergence:
    """A persistent mismatch between model intent and executable position."""

    start_ts: datetime
    end_ts: datetime | None
    model_position: int
    execution_position: int
    cause: str

    def to_dict(self) -> dict[str, object]:
        return {
            "start_ts": self.start_ts.isoformat(),
            "end_ts": self.end_ts.isoformat() if self.end_ts is not None else None,
            "model_position": self.model_position,
            "execution_position": self.execution_position,
            "cause": self.cause,
        }


@dataclass(frozen=True)
class BacktestSizingDecision:
    """One fill-time sizing decision joined to its canonical strategy signal."""

    signal_id: str
    strategy_id: str
    symbol: str
    action: str
    fill_ts: datetime
    side: int
    reference_price: float
    execution_price: float
    sizing: PositionSizingDecision
    provider_translation: CoinbaseSizingTranslation | None
    filled_quantity: float | None
    fill_notional_value: float | None
    post_fill_initial_stop_risk_amount: float | None
    post_fill_initial_stop_risk_pct: float | None
    execution_outcome: str
    execution_rejection_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return deterministic JSON-safe sizing and execution evidence."""

        return {
            "sizing_evidence_schema": SIZING_EVIDENCE_SCHEMA,
            "signal_id": self.signal_id,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "action": self.action,
            "fill_ts": self.fill_ts.isoformat(),
            "side": self.side,
            "reference_price": self.reference_price,
            "execution_price": self.execution_price,
            "sizing": self.sizing.to_dict(),
            "provider_translation": (
                self.provider_translation.to_dict()
                if self.provider_translation is not None
                else None
            ),
            "filled_quantity": self.filled_quantity,
            "fill_notional_value": self.fill_notional_value,
            "post_fill_initial_stop_risk_amount": self.post_fill_initial_stop_risk_amount,
            "post_fill_initial_stop_risk_pct": self.post_fill_initial_stop_risk_pct,
            "execution_outcome": self.execution_outcome,
            "execution_rejection_reason": self.execution_rejection_reason,
        }


@dataclass(frozen=True)
class SimulationResult:
    trades: list[Trade]
    equity_curve: list[tuple[datetime, float]]
    unfilled_signals: int
    unmatched_signals: int = 0
    ignored_signals: int = 0
    cancelled_signals: int = 0
    rejected_entries: int = 0
    buying_power_rejections: int = 0
    account_ruined: bool = False
    ambiguous_bars: int = 0
    protective_stop_exits: int = 0
    model_execution_divergences: tuple[ModelExecutionDivergence, ...] = ()
    terminal_position: TerminalPosition | None = None
    gross_pnl: float = 0.0
    closed_gross_pnl: float = 0.0
    terminal_gross_pnl: float = 0.0
    cost_breakdown: CostBreakdown = field(default_factory=CostBreakdown)
    closed_cost_breakdown: CostBreakdown = field(default_factory=CostBreakdown)
    turnover_notional: float = 0.0
    closed_turnover_notional: float = 0.0
    exposure_seconds: float = 0.0
    holding_periods_bars: tuple[int, ...] = ()
    exit_reason_counts: dict[str, int] = field(default_factory=dict)
    execution_policy_id: str = "next_open_bracket_v2"
    cost_scenario: str = "configured_flat_fee"
    sizing_policy: PositionSizingPolicy | None = None
    sizing_decisions: tuple[BacktestSizingDecision, ...] = ()


@dataclass(frozen=True)
class _PendingDecision:
    signal: Signal
    desired: int
    fill_index: int


class FillSimulator:
    """Replay signals into one directional net position, trades, and equity."""

    def __init__(
        self,
        *,
        initial_capital: float = 100_000.0,
        size_pct: float = 0.95,
        fee_bps: float = 10.0,
        cost_model: ExecutionCostModel | None = None,
        fill_policy: FillPolicy | None = None,
        sizing_policy: PositionSizingPolicy | None = None,
        coinbase_sizing_rules: CoinbaseSizingRules | None = None,
    ) -> None:
        if not math.isfinite(initial_capital) or initial_capital <= 0.0:
            msg = "initial_capital must be finite and positive"
            raise ValueError(msg)
        if not math.isfinite(size_pct) or not 0.0 < size_pct <= 1.0:
            msg = "size_pct must be finite and in (0, 1]"
            raise ValueError(msg)
        if not math.isfinite(fee_bps) or fee_bps < 0.0:
            msg = "fee_bps must be finite and non-negative"
            raise ValueError(msg)
        self._initial = float(initial_capital)
        self._size_pct = float(size_pct)
        self._cost_model = cost_model or ExecutionCostModel.flat_fee(fee_bps)
        self._policy = fill_policy or FillPolicy.reference_v2()
        validate_sizing_boundary(sizing_policy, coinbase_sizing_rules)
        self._sizing_policy = sizing_policy
        self._coinbase_sizing_rules = coinbase_sizing_rules
        self._reset()

    def _reset(self) -> None:
        self._side = 0
        self._qty = 0.0
        self._entry_price = 0.0
        self._entry_reference_price = 0.0
        self._entry_ts: datetime | None = None
        self._entry_bar_index = 0
        self._entry_costs = CostBreakdown()
        self._stop_loss: float | None = None
        self._take_profit: float | None = None
        self._realized = 0.0
        self._trades: list[Trade] = []
        self._aggregate_costs = CostBreakdown()
        self._closed_costs = CostBreakdown()
        self._turnover_notional = 0.0
        self._closed_turnover_notional = 0.0
        self._ignored_signals = 0
        self._cancelled_signals = 0
        self._rejected_entries = 0
        self._ambiguous_bars = 0
        self._protective_stop_exits = 0
        self._buying_power_rejections = 0
        self._account_ruined = False
        self._model_side = 0
        self._model_entry_signal_id: str | None = None
        self._execution_entry_signal_id: str | None = None
        self._open_divergence: ModelExecutionDivergence | None = None
        self._divergences: list[ModelExecutionDivergence] = []
        self._sizing_decisions: list[BacktestSizingDecision] = []

    def run(self, bars: list[Bar], signals: list[Signal]) -> SimulationResult:
        """Fill signals chronologically; a close decision never fills same-bar."""
        self._reset()
        return self._run_reference(bars, signals)

    def _signals_by_timestamp(
        self, bars: list[Bar], signals: list[Signal]
    ) -> tuple[dict[datetime, list[Signal]], int]:
        bar_timestamps = {bar.timestamp for bar in bars}
        bar_symbols = {bar.symbol for bar in bars}
        grouped: dict[datetime, list[Signal]] = defaultdict(list)
        unmatched = 0
        for signal in signals:
            if signal.timestamp not in bar_timestamps or signal.symbol not in bar_symbols:
                unmatched += 1
                continue
            grouped[signal.timestamp].append(signal)
        return dict(grouped), unmatched

    def _run_reference(self, bars: list[Bar], signals: list[Signal]) -> SimulationResult:
        bars = [normalize_execution_bar(bar) for bar in bars]
        grouped, unmatched = self._signals_by_timestamp(bars, signals)
        equity_curve: list[tuple[datetime, float]] = []
        pending: _PendingDecision | None = None

        for i, bar in enumerate(bars):
            bar_times = execution_bar_times(bar)
            # A resting bracket remains active through the next open.  It wins a
            # gap race against a model CLOSE that becomes executable at that open.
            if self._policy.protective_orders and self._side != 0:
                self._apply_protective_exit(
                    bar,
                    i,
                    event_ts=bar_times.open_ts,
                    gap_only=True,
                )

            if pending is not None and pending.fill_index <= i:
                self._transition_reference(
                    pending,
                    bar,
                    i,
                    fill_ts=bar_times.open_ts,
                )
                pending = None

            # Brackets attached to an entry filled at this open are active for
            # the remainder of this bar.  Unknown stop/target order is stop-first.
            if self._policy.protective_orders and self._side != 0:
                self._apply_protective_exit(
                    bar,
                    i,
                    event_ts=bar_times.close_ts,
                    gap_only=False,
                )

            equity_curve.append((bar_times.close_ts, self._equity(bar.close)))

            same_bar = grouped.get(bar.timestamp, [])
            if not same_bar:
                continue
            self._ignored_signals += max(0, len(same_bar) - 1)
            signal = same_bar[-1]
            desired = _DESIRED_POSITION.get(signal.action)
            if desired is None:
                self._ignored_signals += 1
                continue
            previous_model_side = self._model_side
            if desired != previous_model_side:
                self._model_entry_signal_id = (
                    self._stable_signal_id(signal) if desired != 0 else None
                )
            self._model_side = desired

            if pending is not None:
                if desired == pending.desired:
                    self._ignored_signals += 1
                    continue
                # A later contradictory decision cancels an unfilled order.  A
                # CLOSE while still flat is a real cancellation, not a new fill.
                pending = None
                self._cancelled_signals += 1
                if desired == self._side:
                    self._reconcile_divergence(bar_times.close_ts)
                    continue

            if desired == self._side:
                self._ignored_signals += 1
                self._reconcile_divergence(bar_times.close_ts)
                continue

            # A same-side signal after an execution rejection or protective exit
            # is a retry with new entry lineage. Ordinary duplicates were handled
            # above while model and execution positions still agreed.
            if desired != 0 and desired == previous_model_side:
                self._model_entry_signal_id = self._stable_signal_id(signal)
            pending = _PendingDecision(
                signal=signal,
                desired=desired,
                fill_index=i + 1 + self._cost_model.latency_bars,
            )
            self._reconcile_divergence(bar_times.close_ts, pending=pending)

        unfilled = int(pending is not None)
        terminal = self._terminal_position(bars)
        self._finalize_divergence(None)
        return self._build_result(
            bars=bars,
            equity_curve=equity_curve,
            unfilled=unfilled,
            unmatched=unmatched,
            terminal=terminal,
        )

    def _transition_reference(
        self,
        pending: _PendingDecision,
        bar: Bar,
        bar_index: int,
        *,
        fill_ts: datetime,
    ) -> None:
        desired = pending.desired
        signal = pending.signal
        if desired == self._side:
            self._ignored_signals += 1
            return
        if self._side != 0:
            reason = (
                str(signal.metadata.get("exit_reason") or "model_close")
                if desired == 0
                else "model_reverse"
            )
            self._close_reference(
                reference_price=bar.open,
                ts=fill_ts,
                bar_index=bar_index,
                reason=reason,
            )
        if desired == 0:
            self._reconcile_divergence(fill_ts)
            return
        self._open_reference(
            side=desired,
            reference_price=bar.open,
            ts=fill_ts,
            bar_index=bar_index,
            signal=signal,
        )
        self._reconcile_divergence(fill_ts)

    def _open_reference(
        self,
        *,
        side: int,
        reference_price: float,
        ts: datetime,
        bar_index: int,
        signal: Signal,
    ) -> None:
        execution_price = self._execution_price(reference_price, transaction_side=side)
        if not self._bracket_valid(signal, execution_price, side):
            self._rejected_entries += 1
            self._open_divergence_event(ts, cause="invalid_entry_bracket")
            return
        equity_now = self._initial + self._realized
        if not math.isfinite(equity_now) or equity_now <= 0.0:
            self._account_ruined = True
            self._rejected_entries += 1
            self._open_divergence_event(ts, cause="account_ruined")
            return
        sizing_decision: PositionSizingDecision | None = None
        provider_translation: CoinbaseSizingTranslation | None = None
        if self._sizing_policy is None:
            intended_notional = equity_now * self._size_pct
            qty = intended_notional / reference_price
        else:
            sizing_decision = self._sizing_policy.calculate(
                PositionSizingRequest(
                    price=reference_price,
                    equity=equity_now,
                    buying_power=equity_now,
                    stop_loss=signal.stop_loss,
                )
            )
            if not sizing_decision.accepted:
                self._record_sizing_decision(
                    signal=signal,
                    fill_ts=ts,
                    side=side,
                    reference_price=reference_price,
                    execution_price=execution_price,
                    decision=sizing_decision,
                    provider_translation=None,
                    filled_quantity=None,
                    execution_outcome="rejected",
                    rejection_reason=sizing_decision.rejection_reason,
                )
                self._rejected_entries += 1
                cause = f"position_sizing_{sizing_decision.rejection_reason}"
                self._open_divergence_event(ts, cause=cause)
                return
            if self._coinbase_sizing_rules is not None:
                provider_translation = apply_coinbase_sizing_rules(
                    sizing_decision,
                    side=(MarketOrderSide.BUY if side > 0 else MarketOrderSide.SELL),
                    rules=self._coinbase_sizing_rules,
                )
                if not provider_translation.accepted:
                    self._record_sizing_decision(
                        signal=signal,
                        fill_ts=ts,
                        side=side,
                        reference_price=reference_price,
                        execution_price=execution_price,
                        decision=sizing_decision,
                        provider_translation=provider_translation,
                        filled_quantity=None,
                        execution_outcome="rejected",
                        rejection_reason=provider_translation.rejection_reason,
                    )
                    self._rejected_entries += 1
                    cause = f"position_sizing_{provider_translation.rejection_reason}"
                    self._open_divergence_event(ts, cause=cause)
                    return
            broker_size = (
                provider_translation.market_order_size if provider_translation is not None else None
            )
            qty = (
                broker_size.base_quantity_at_fill(execution_price)
                if broker_size is not None
                else sizing_decision.quantity
            )
            intended_notional = (
                broker_size.quote_notional_at_fill(execution_price)
                if broker_size is not None
                else qty * reference_price
            )
        if not math.isfinite(qty) or qty <= 0.0:
            self._account_ruined = True
            self._rejected_entries += 1
            self._open_divergence_event(ts, cause="invalid_position_size")
            return
        costs = self._fill_costs(
            reference_price=reference_price,
            execution_price=execution_price,
            quantity=qty,
        )
        required_buying_power = intended_notional + costs.total
        if required_buying_power > equity_now + max(1e-9, abs(equity_now) * 1e-12):
            if sizing_decision is not None:
                self._record_sizing_decision(
                    signal=signal,
                    fill_ts=ts,
                    side=side,
                    reference_price=reference_price,
                    execution_price=execution_price,
                    decision=sizing_decision,
                    provider_translation=provider_translation,
                    filled_quantity=None,
                    execution_outcome="rejected",
                    rejection_reason="insufficient_buying_power_after_costs",
                )
            self._buying_power_rejections += 1
            self._rejected_entries += 1
            self._open_divergence_event(ts, cause="insufficient_buying_power")
            return
        if sizing_decision is not None:
            self._record_sizing_decision(
                signal=signal,
                fill_ts=ts,
                side=side,
                reference_price=reference_price,
                execution_price=execution_price,
                decision=sizing_decision,
                provider_translation=provider_translation,
                filled_quantity=qty,
                execution_outcome="accepted",
                rejection_reason=None,
            )
        self._side = side
        self._qty = qty
        self._entry_price = execution_price
        self._entry_reference_price = reference_price
        self._entry_ts = ts
        self._entry_bar_index = bar_index
        self._entry_costs = costs
        self._stop_loss = signal.stop_loss
        self._take_profit = signal.take_profit
        self._execution_entry_signal_id = self._stable_signal_id(signal)
        self._aggregate_costs = self._aggregate_costs + costs
        self._turnover_notional += intended_notional

    def _record_sizing_decision(
        self,
        *,
        signal: Signal,
        fill_ts: datetime,
        side: int,
        reference_price: float,
        execution_price: float,
        decision: PositionSizingDecision,
        provider_translation: CoinbaseSizingTranslation | None,
        filled_quantity: float | None,
        execution_outcome: str,
        rejection_reason: str | None,
    ) -> None:
        stable_signal_id = self._stable_signal_id(signal)
        fill_notional = filled_quantity * execution_price if filled_quantity is not None else None
        initial_stop_risk = (
            abs(execution_price - signal.stop_loss) * filled_quantity
            if filled_quantity is not None and signal.stop_loss is not None
            else None
        )
        self._sizing_decisions.append(
            BacktestSizingDecision(
                signal_id=stable_signal_id,
                strategy_id=signal.strategy_id,
                symbol=signal.symbol,
                action=signal.action.value,
                fill_ts=fill_ts,
                side=side,
                reference_price=reference_price,
                execution_price=execution_price,
                sizing=decision,
                provider_translation=provider_translation,
                filled_quantity=filled_quantity,
                fill_notional_value=fill_notional,
                post_fill_initial_stop_risk_amount=initial_stop_risk,
                post_fill_initial_stop_risk_pct=(
                    initial_stop_risk / decision.equity if initial_stop_risk is not None else None
                ),
                execution_outcome=execution_outcome,
                execution_rejection_reason=rejection_reason,
            )
        )

    def _close_reference(
        self,
        *,
        reference_price: float,
        ts: datetime,
        bar_index: int,
        reason: str,
    ) -> None:
        transaction_side = -self._side
        execution_price = self._execution_price(reference_price, transaction_side=transaction_side)
        exit_costs = self._fill_costs(
            reference_price=reference_price,
            execution_price=execution_price,
            quantity=self._qty,
        )
        costs = self._entry_costs + exit_costs
        gross = (reference_price - self._entry_reference_price) * self._qty * self._side
        pnl = gross - costs.total
        self._trades.append(
            Trade(
                symbol="",
                side="long" if self._side > 0 else "short",
                entry_ts=self._entry_ts,  # type: ignore[arg-type]
                exit_ts=ts,
                entry_price=self._entry_price,
                exit_price=execution_price,
                quantity=self._qty,
                fees=costs.total,
                pnl=pnl,
                gross_pnl=gross,
                exit_reason=reason,
                holding_bars=bar_index - self._entry_bar_index,
                entry_reference_price=self._entry_reference_price,
                exit_reference_price=reference_price,
                cost_breakdown=costs,
            )
        )
        self._realized += pnl
        self._mark_account_ruin()
        self._aggregate_costs = self._aggregate_costs + exit_costs
        self._turnover_notional += self._qty * reference_price
        self._closed_costs = self._closed_costs + costs
        self._closed_turnover_notional += self._qty * (
            self._entry_reference_price + reference_price
        )
        self._clear_position()

    def _apply_protective_exit(
        self,
        bar: Bar,
        bar_index: int,
        *,
        event_ts: datetime,
        gap_only: bool,
    ) -> None:
        if self._side == 0:
            return
        stop = self._stop_loss
        target = self._take_profit
        if gap_only:
            stop_hit = stop is not None and (
                (self._side > 0 and bar.open <= stop) or (self._side < 0 and bar.open >= stop)
            )
            target_hit = target is not None and (
                (self._side > 0 and bar.open >= target) or (self._side < 0 and bar.open <= target)
            )
            if not stop_hit and not target_hit:
                return
            if stop_hit and target_hit:
                self._ambiguous_bars += 1
            reason = "stop_loss_gap" if stop_hit else "take_profit_gap"
            if stop_hit:
                self._protective_stop_exits += 1
            self._close_reference(
                reference_price=bar.open,
                ts=event_ts,
                bar_index=bar_index,
                reason=reason,
            )
            self._open_divergence_event(event_ts, cause="protective_exit")
            return

        stop_hit = stop is not None and (
            (self._side > 0 and bar.low <= stop) or (self._side < 0 and bar.high >= stop)
        )
        target_hit = target is not None and (
            (self._side > 0 and bar.high >= target) or (self._side < 0 and bar.low <= target)
        )
        if not stop_hit and not target_hit:
            return
        if stop_hit and target_hit:
            self._ambiguous_bars += 1
        if stop_hit:
            assert stop is not None
            reference_price = stop
            reason = "stop_loss"
            self._protective_stop_exits += 1
        else:
            assert target is not None
            reference_price = target
            reason = "take_profit"
        self._close_reference(
            reference_price=reference_price,
            ts=event_ts,
            bar_index=bar_index,
            reason=reason,
        )
        self._open_divergence_event(event_ts, cause="protective_exit")

    def _bracket_valid(self, signal: Signal, execution_price: float, side: int) -> bool:
        if self._policy.invalid_bracket_policy == "ignore":
            return True
        stop = signal.stop_loss
        target = signal.take_profit
        for level in (stop, target):
            if level is not None and (not math.isfinite(level) or level <= 0.0):
                return False
        if side > 0:
            return (stop is None or stop < execution_price) and (
                target is None or target > execution_price
            )
        return (stop is None or stop > execution_price) and (
            target is None or target < execution_price
        )

    def _execution_price(self, reference_price: float, *, transaction_side: int) -> float:
        return adverse_execution_price(
            reference_price,
            transaction_side=transaction_side,
            cost_model=self._cost_model,
        )

    def _fill_costs(
        self, *, reference_price: float, execution_price: float, quantity: float
    ) -> CostBreakdown:
        return execution_fill_costs(
            reference_price=reference_price,
            execution_price=execution_price,
            quantity=quantity,
            cost_model=self._cost_model,
        )

    def _clear_position(self) -> None:
        self._side = 0
        self._qty = 0.0
        self._entry_price = 0.0
        self._entry_reference_price = 0.0
        self._entry_ts = None
        self._entry_bar_index = 0
        self._entry_costs = CostBreakdown()
        self._stop_loss = None
        self._take_profit = None
        self._execution_entry_signal_id = None

    def _mark_account_ruin(self) -> None:
        available_equity = self._initial + self._realized
        if not math.isfinite(available_equity) or available_equity <= 0.0:
            self._account_ruined = True

    def _equity(self, mark_price: float) -> float:
        if self._side == 0:
            return self._initial + self._realized
        unrealized = (mark_price - self._entry_reference_price) * self._qty * self._side
        return self._initial + self._realized + unrealized - self._entry_costs.total

    def _terminal_position(self, bars: list[Bar]) -> TerminalPosition | None:
        if self._side == 0 or not bars or self._entry_ts is None:
            return None
        final_bar = bars[-1]
        gross = (final_bar.close - self._entry_reference_price) * self._qty * self._side
        return TerminalPosition(
            symbol=final_bar.symbol,
            side="long" if self._side > 0 else "short",
            quantity=self._qty,
            entry_ts=self._entry_ts,
            entry_price=self._entry_price,
            mark_price=final_bar.close,
            gross_unrealized_pnl=gross,
            net_unrealized_pnl=gross - self._entry_costs.total,
            stop_loss=self._stop_loss,
            take_profit=self._take_profit,
            holding_bars=(len(bars) - 1) - self._entry_bar_index,
            paid_costs=self._entry_costs,
        )

    def _open_divergence_event(self, timestamp: datetime, *, cause: str) -> None:
        if self._model_execution_reconciled():
            self._reconcile_divergence(timestamp)
            return
        current = self._open_divergence
        if (
            current is not None
            and current.model_position == self._model_side
            and current.execution_position == self._side
            and current.cause == cause
        ):
            return
        self._finalize_divergence(timestamp)
        self._open_divergence = ModelExecutionDivergence(
            start_ts=timestamp,
            end_ts=None,
            model_position=self._model_side,
            execution_position=self._side,
            cause=cause,
        )

    def _reconcile_divergence(
        self,
        timestamp: datetime,
        *,
        pending: _PendingDecision | None = None,
    ) -> None:
        expected_fill = pending is not None and pending.desired == self._model_side
        if self._model_execution_reconciled():
            self._finalize_divergence(timestamp)
        elif self._model_side == self._side:
            self._open_divergence_event(timestamp, cause="position_lineage_mismatch")
        elif self._open_divergence is not None:
            # A previously observed mismatch remains real while a retry waits
            # for its next-open fill.  Close it only after execution actually
            # catches up (or the model returns to the executable position).
            return
        elif expected_fill:
            # Ordinary next-open latency is part of the registered policy, not
            # a divergence, unless a prior rejection/stop already opened one.
            return
        elif self._open_divergence is None:
            self._open_divergence_event(timestamp, cause="unreconciled_model_state")

    def _model_execution_reconciled(self) -> bool:
        return self._model_side == self._side and (
            self._model_side == 0 or self._model_entry_signal_id == self._execution_entry_signal_id
        )

    @staticmethod
    def _stable_signal_id(signal: Signal) -> str:
        return signal.external_signal_id or compute_stable_signal_id(
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            action=signal.action,
            timestamp=signal.timestamp,
            run_id=signal.run_id,
            strategy_version=signal.strategy_version,
        )

    def _finalize_divergence(self, end_ts: datetime | None) -> None:
        current = self._open_divergence
        if current is None:
            return
        self._divergences.append(
            ModelExecutionDivergence(
                start_ts=current.start_ts,
                end_ts=end_ts,
                model_position=current.model_position,
                execution_position=current.execution_position,
                cause=current.cause,
            )
        )
        self._open_divergence = None

    def _build_result(
        self,
        *,
        bars: list[Bar],
        equity_curve: list[tuple[datetime, float]],
        unfilled: int,
        unmatched: int,
        terminal: TerminalPosition | None,
    ) -> SimulationResult:
        reasons = Counter(trade.exit_reason for trade in self._trades)
        exposure_seconds = sum(
            max(0.0, (trade.exit_ts - trade.entry_ts).total_seconds()) for trade in self._trades
        )
        if terminal is not None and bars:
            exposure_seconds += max(
                0.0,
                (bars[-1].timestamp - terminal.entry_ts).total_seconds(),
            )
        closed_gross = sum(trade.gross_pnl for trade in self._trades)
        terminal_gross = terminal.gross_unrealized_pnl if terminal is not None else 0.0
        gross_pnl = closed_gross + terminal_gross
        if equity_curve:
            reconciled_equity = self._initial + gross_pnl - self._aggregate_costs.total
            tolerance = max(1e-8, abs(reconciled_equity) * 1e-10)
            if not math.isclose(equity_curve[-1][1], reconciled_equity, abs_tol=tolerance):
                msg = "simulation P&L, costs, and terminal equity do not reconcile"
                raise RuntimeError(msg)
        return SimulationResult(
            trades=list(self._trades),
            equity_curve=equity_curve,
            unfilled_signals=unfilled,
            unmatched_signals=unmatched,
            ignored_signals=self._ignored_signals,
            cancelled_signals=self._cancelled_signals,
            rejected_entries=self._rejected_entries,
            ambiguous_bars=self._ambiguous_bars,
            buying_power_rejections=self._buying_power_rejections,
            account_ruined=self._account_ruined,
            protective_stop_exits=self._protective_stop_exits,
            model_execution_divergences=tuple(self._divergences),
            terminal_position=terminal,
            gross_pnl=gross_pnl,
            closed_gross_pnl=closed_gross,
            terminal_gross_pnl=terminal_gross,
            cost_breakdown=self._aggregate_costs,
            closed_cost_breakdown=self._closed_costs,
            turnover_notional=self._turnover_notional,
            closed_turnover_notional=self._closed_turnover_notional,
            exposure_seconds=exposure_seconds,
            holding_periods_bars=tuple(trade.holding_bars for trade in self._trades),
            exit_reason_counts=dict(reasons),
            execution_policy_id=self._policy.policy_id,
            cost_scenario=self._cost_model.scenario_name,
            sizing_policy=self._sizing_policy,
            sizing_decisions=tuple(self._sizing_decisions),
        )
