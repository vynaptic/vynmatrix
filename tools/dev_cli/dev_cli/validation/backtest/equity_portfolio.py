"""N-symbol portfolio backtest harness for US-equity PureSignalStrategy cores
(strategies/indicator/USQualityCompounder/README.md evidence path).

Drives the PRODUCTION strategy core (no rule re-implementation) over a
point-in-time universe with a shared cash account, next-open fills, and the
dated US cost model. All simulation prices are corporate-action-adjusted
(total-return space: dividends implicitly reinvested); dollar-ADV ranking
uses a conservatively haircutted price and volume in the provider's pinned
split-adjustment coordinate and never relabels provider volume as raw shares.

Conventions pre-registered here (referenced by the run report):
- Fills at the next session's adjusted open; one-way cost = base_bps, times
  a stress multiplier when the prior session's VIX percentile exceeds the
  stress threshold. The fresh institutional policy adds effective-date SEC
  Section 31 and FINRA Trading Activity Fees on sells. The 2020-01-01 onward
  schedules follow the published regulator notices exactly; older shared-
  harness runs retain their documented conservative SEC-only legacy rate.
- Targets are frozen as size_hint x prior-close equity. Target reductions run
  before increases, and increases are clipped to available cash (long-only, no
  margin, cash earns zero).
- Positions in names rotating out of the quarterly universe are force-closed
  at the first open of the new quarter (reason ``universe_rotation``).
  A held symbol with no further bar requires an independently verified
  terminal disposition; the simulator never invents a last-close delisting
  fill. Survivors remain open and are marked at the final close; a separately
  labelled terminal-liquidation sensitivity is non-actionable and never
  changes primary trades, costs, turnover, or NAV.
- A symbol entering the fed set is pre-loaded with its prior history (full
  historical metadata) via ``bootstrap_history``, which suppresses emission;
  the core's monotonic session guard keeps replayed dates off the shared
  benchmark gate.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from datetime import time as dt_time
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from dev_cli.validation.backtest.equity_portfolio_inputs import (
    _TARGET_NOTIONAL_REL_TOLERANCE,
    _finra_taf_bps,
    _section31_bps,
    _target_notional_tolerance,
    earnings_distances,
    is_member_asof,
    median_dollar_adv,
    rebalance_sessions,
    select_universe,
    vix_percentiles,
)
from dev_cli.validation.backtest.equity_portfolio_policies import (
    AdvUniverseSelectionPolicy,
    DefaultPanelContextPolicy,
    ExplicitRawCorporateActionPolicy,
    InstitutionalEquityExecutionCostPolicy,
    LegacyEquityExecutionCostPolicy,
    PanelExecutionUniversePolicy,
    PinnedUniverseSelectionPolicy,
    QuarterlyRebalanceCadencePolicy,
    SignalHintTargetAllocationPolicy,
    StaticExecutionUniverseCadencePolicy,
    SynchronizedStrategyPanelContextPolicy,
    TotalReturnAdjustedCorporateActionPolicy,
    institutional_equity_execution_cost_policy,
)
from dev_cli.validation.backtest.equity_portfolio_types import (
    DEFAULT_INSTITUTIONAL_EQUITY_EXECUTION_ASSUMPTIONS,
    EQUITY_TERMINAL_LIQUIDATION_SENSITIVITY_POLICY_ID,
    EQUITY_TERMINAL_TREATMENT_POLICY_ID,
    EQUITY_TURNOVER_POLICY_ID,
    BacktestResult,
    CorporateActionAccountingPolicy,
    CorporateActionApplication,
    CostConfig,
    DailyBar,
    DailyPortfolioExposure,
    EquityCorporateAction,
    EquityCorporateActionKind,
    EquityDataset,
    EquityExecutionCostPolicy,
    EquityPortfolioError,
    EquitySessionPanel,
    EquityTerminalDisposition,
    EquityTerminalDispositionKind,
    EquityTerminalPosition,
    InstitutionalEquityExecutionAssumptions,
    PanelContextPolicy,
    PortfolioConfig,
    RebalanceCadencePolicy,
    TargetAllocationPolicy,
    Trade,
    UniverseSelection,
    UniverseSelectionPolicy,
    two_way_traded_notional,
)
from dev_cli.validation.backtest.metrics import (
    annualized_volatility_pct,
    cagr_pct,
    daily_returns,
    max_drawdown_pct,
    recovery_duration_days,
    sharpe_ratio,
    sortino_ratio,
)
from lib_strategy.signals.emitter import BacktestSignalEmitter
from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy
from lib_strategy.signals.signal import SignalAction

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_COMPATIBILITY_REEXPORTS = (
    _TARGET_NOTIONAL_REL_TOLERANCE,
    _finra_taf_bps,
    _section31_bps,
    _target_notional_tolerance,
    AdvUniverseSelectionPolicy,
    BacktestResult,
    CorporateActionAccountingPolicy,
    CorporateActionApplication,
    CostConfig,
    DailyBar,
    DailyPortfolioExposure,
    DEFAULT_INSTITUTIONAL_EQUITY_EXECUTION_ASSUMPTIONS,
    DefaultPanelContextPolicy,
    earnings_distances,
    EquityCorporateAction,
    EquityCorporateActionKind,
    EquityDataset,
    EquityExecutionCostPolicy,
    EquityPortfolioError,
    EquitySessionPanel,
    EquityTerminalDisposition,
    EquityTerminalDispositionKind,
    EquityTerminalPosition,
    ExplicitRawCorporateActionPolicy,
    institutional_equity_execution_cost_policy,
    InstitutionalEquityExecutionAssumptions,
    InstitutionalEquityExecutionCostPolicy,
    is_member_asof,
    LegacyEquityExecutionCostPolicy,
    median_dollar_adv,
    PanelContextPolicy,
    PanelExecutionUniversePolicy,
    PinnedUniverseSelectionPolicy,
    PortfolioConfig,
    QuarterlyRebalanceCadencePolicy,
    rebalance_sessions,
    RebalanceCadencePolicy,
    select_universe,
    SignalHintTargetAllocationPolicy,
    StaticExecutionUniverseCadencePolicy,
    SynchronizedStrategyPanelContextPolicy,
    TargetAllocationPolicy,
    TotalReturnAdjustedCorporateActionPolicy,
    Trade,
    UniverseSelection,
    UniverseSelectionPolicy,
    vix_percentiles,
)

_ANNUALIZATION = 252.0
# Daily bars carry one canonical UTC decision stamp; strategy cores require
# timezone-aware timestamps and the platform is UTC-canonical throughout.
_BAR_STAMP = dt_time(21, 0, tzinfo=UTC)
_CASH_EPSILON = -1e-6
_MIN_BENCHMARK_POINTS = 3


# ---------------------------------------------------------------------------
# simulator
# ---------------------------------------------------------------------------


@dataclass
class _PendingEntry:
    signal: Any
    remaining_delay_sessions: int


@dataclass
class _PendingExit:
    reason: str
    remaining_delay_sessions: int


@dataclass
class _RunState:
    """Mutable book state threaded through the per-session steps."""

    cash: float
    gross_cash: float
    prev_equity: float
    dividend_receivable: float = 0.0
    gross_dividend_receivable: float = 0.0
    prev_vix_pct: float | None = None
    last_execution_stressed: bool = False
    universe: list[str] = field(default_factory=list)
    fed: set[str] = field(default_factory=set)
    open_trades: dict[str, Trade] = field(default_factory=dict)
    open_trade_lots: dict[str, list[Trade]] = field(default_factory=dict)
    pending_entries: list[_PendingEntry] = field(default_factory=list)
    pending_exits: dict[str, _PendingExit] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    gross_equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    daily_exposures: list[DailyPortfolioExposure] = field(default_factory=list)
    cost_ledger: list[dict[str, Any]] = field(default_factory=list)
    corporate_action_ledger: list[dict[str, Any]] = field(default_factory=list)
    terminal_disposition_ledger: list[dict[str, Any]] = field(default_factory=list)
    applied_terminal_dispositions: set[tuple[str, str]] = field(default_factory=set)
    rebalance_log: list[dict[str, Any]] = field(default_factory=list)
    skipped_entries: int = 0


def _cost_ledger_entry(
    *,
    session: date,
    symbol: str,
    side: str,
    reference_price: float,
    execution_price: float,
    quantity: float,
    components: Mapping[str, float],
    available_daily_notional: float | None = None,
    event_kind: str = "execution",
    turnover_eligible: bool = True,
) -> dict[str, Any]:
    required = (
        "commission",
        "half_spread",
        "impact",
        "regulatory_fee",
        "slippage",
        "total",
    )
    values = {name: float(components[name]) for name in required}
    if any(not math.isfinite(value) or value < 0.0 for value in values.values()):
        msg = f"execution cost components must be finite and non-negative for {symbol}"
        raise EquityPortfolioError(msg)
    component_sum = sum(values[name] for name in required if name != "total")
    if not math.isclose(component_sum, values["total"], rel_tol=1e-9, abs_tol=1e-6):
        msg = f"execution cost components do not reconcile for {symbol}"
        raise EquityPortfolioError(msg)
    if not event_kind.strip():
        msg = "cost-ledger event_kind must not be blank"
        raise EquityPortfolioError(msg)
    return {
        "available_daily_notional": available_daily_notional,
        "components": values,
        "event_kind": event_kind,
        "execution_price": execution_price,
        "quantity": quantity,
        "reference_price": reference_price,
        "session": session.isoformat(),
        "side": side,
        "symbol": symbol,
        "turnover_eligible": turnover_eligible,
    }


class EquityPortfolioBacktest:
    """Feeds the production core session by session and books fills next open."""

    def __init__(
        self,
        dataset: EquityDataset,
        strategy_factory: Callable[[], PureSignalStrategy],
        config: PortfolioConfig,
        *,
        universe_policy: UniverseSelectionPolicy | None = None,
        cadence_policy: RebalanceCadencePolicy | None = None,
        panel_context_policy: PanelContextPolicy | None = None,
        target_allocation_policy: TargetAllocationPolicy | None = None,
        execution_cost_policy: EquityExecutionCostPolicy | None = None,
        corporate_action_policy: CorporateActionAccountingPolicy | None = None,
    ) -> None:
        self._dataset = dataset
        self._config = config
        self._strategy = strategy_factory()
        self._emitter = BacktestSignalEmitter()
        self._strategy._emitter = self._emitter
        self._strategy.initialize()
        self._universe_policy = universe_policy or AdvUniverseSelectionPolicy()
        self._cadence_policy = cadence_policy or QuarterlyRebalanceCadencePolicy()
        self._panel_context_policy = panel_context_policy or DefaultPanelContextPolicy()
        self._target_allocation_policy = (
            target_allocation_policy or SignalHintTargetAllocationPolicy()
        )
        self._execution_cost_policy = execution_cost_policy or LegacyEquityExecutionCostPolicy(
            config.costs
        )
        self._corporate_action_policy = (
            corporate_action_policy or TotalReturnAdjustedCorporateActionPolicy()
        )
        if self._corporate_action_policy.price_basis not in {"raw", "total_return_adjusted"}:
            msg = "corporate-action policy price_basis must be raw or total_return_adjusted"
            raise EquityPortfolioError(msg)
        if (
            isinstance(self._execution_cost_policy.latency_sessions, bool)
            or not isinstance(self._execution_cost_policy.latency_sessions, int)
            or self._execution_cost_policy.latency_sessions < 0
        ):
            msg = "execution-cost policy latency_sessions must be a non-negative integer"
            raise EquityPortfolioError(msg)
        self._bars_by_symbol: dict[str, dict[date, DailyBar]] = {
            symbol: {bar.session: bar for bar in bars} for symbol, bars in dataset.bars.items()
        }
        self._vix_pct = vix_percentiles(dataset)
        self._earnings_at = {
            symbol: earnings_distances(dataset.earnings.get(symbol, ()), dataset.sessions)
            for symbol in dataset.bars
        }
        self._session_index = {session: i for i, session in enumerate(dataset.sessions)}
        self._actions_by_session: dict[date, list[EquityCorporateAction]] = defaultdict(list)
        for symbol, actions in dataset.corporate_actions.items():
            for action in actions:
                if action.symbol != symbol:
                    msg = f"corporate-action mapping key does not match {action.symbol}"
                    raise EquityPortfolioError(msg)
                self._actions_by_session[action.effective_session].append(action)
        self._terminal_dispositions: dict[str, tuple[EquityTerminalDisposition, ...]] = {}
        official_sessions = set(dataset.sessions)
        for symbol, dispositions in dataset.terminal_dispositions.items():
            ordered = tuple(
                sorted(
                    dispositions,
                    key=lambda item: (
                        item.effective_session,
                        item.kind.value,
                        item.source_observation_id,
                    ),
                )
            )
            seen_sessions: set[date] = set()
            for disposition in ordered:
                if disposition.symbol != symbol:
                    msg = f"terminal-disposition mapping key does not match {disposition.symbol}"
                    raise EquityPortfolioError(msg)
                if disposition.effective_session not in official_sessions:
                    msg = (
                        f"terminal disposition for {symbol} is not on an official "
                        f"dataset session: {disposition.effective_session}"
                    )
                    raise EquityPortfolioError(msg)
                if disposition.effective_session in seen_sessions:
                    msg = (
                        f"multiple terminal dispositions for {symbol} on "
                        f"{disposition.effective_session}"
                    )
                    raise EquityPortfolioError(msg)
                seen_sessions.add(disposition.effective_session)
            self._terminal_dispositions[symbol] = ordered

    def run(self) -> BacktestResult:
        cfg = self._config
        sessions = self._dataset.sessions
        eval_start_idx = bisect_left(sessions, cfg.eval_start)
        eval_end_idx = bisect_right(sessions, cfg.eval_end) - 1
        if eval_start_idx >= len(sessions) or eval_end_idx <= eval_start_idx:
            msg = "evaluation window is empty for the supplied sessions"
            raise EquityPortfolioError(msg)

        rebalances = set(
            self._cadence_policy.sessions(
                sessions,
                start=cfg.eval_start,
                end=cfg.eval_end,
            )
        )
        state = _RunState(
            cash=cfg.initial_capital,
            gross_cash=cfg.initial_capital,
            prev_equity=cfg.initial_capital,
        )
        initial_selection = self._universe_policy.select(
            self._dataset,
            asof=sessions[eval_start_idx],
            config=cfg,
        )
        state.universe = list(initial_selection.symbols)
        state.rebalance_log.append(
            {
                "session": sessions[eval_start_idx].isoformat(),
                "universe": state.universe,
                **initial_selection.detail,
            }
        )
        for symbol in state.universe:
            self._preload(symbol, before=cfg.eval_start)
            state.fed.add(symbol)

        for idx in range(eval_start_idx, eval_end_idx + 1):
            today = sessions[idx]
            stress = (
                state.prev_vix_pct is not None
                and state.prev_vix_pct > cfg.costs.stress_vix_percentile
            )
            state.last_execution_stressed = stress
            self._apply_corporate_actions(state, today)
            self._fill_exits(state, today, stressed=stress)
            self._fill_entries(state, today, stressed=stress)
            self._feed_session(state, today, idx)
            self._collect_signals(state)
            self._mark_session(state, today)
            if today in rebalances and idx != eval_start_idx:
                # Universe membership is decided from completed-session evidence.
                # Applying the change after this session's fills/feed prevents
                # same-open use of a membership decision only known at its close;
                # zero-latency exits therefore execute at the next official open,
                # symmetrically with entries emitted from the same decision.
                self._apply_rebalance(state, today)

        final_session = sessions[eval_end_idx]
        terminal_positions = self._terminal_positions(state, final_session)
        terminal_liquidation_sensitivity = self._terminal_liquidation_sensitivity(
            state,
            terminal_positions,
            stressed=state.last_execution_stressed,
        )
        result = BacktestResult(
            equity_curve=state.equity_curve,
            gross_equity_curve=state.gross_equity_curve,
            trades=state.trades,
            cost_ledger=state.cost_ledger,
            corporate_action_ledger=state.corporate_action_ledger,
            terminal_disposition_ledger=state.terminal_disposition_ledger,
            rebalance_log=state.rebalance_log,
            skipped_entries=state.skipped_entries,
            daily_exposures=state.daily_exposures,
            terminal_positions=terminal_positions,
            terminal_liquidation_sensitivity=terminal_liquidation_sensitivity,
            policies={
                "cadence": self._cadence_policy.policy_id,
                "corporate_actions": self._corporate_action_policy.policy_id,
                "execution_cost": self._execution_cost_policy.policy_id,
                "panel_context": self._panel_context_policy.policy_id,
                "target_allocation": self._target_allocation_policy.policy_id,
                "terminal_liquidation_sensitivity": (
                    EQUITY_TERMINAL_LIQUIDATION_SENSITIVITY_POLICY_ID
                ),
                "terminal_treatment": EQUITY_TERMINAL_TREATMENT_POLICY_ID,
                "turnover": EQUITY_TURNOVER_POLICY_ID,
                "universe": self._universe_policy.policy_id,
            },
            execution_assumptions=dict(self._execution_cost_policy.assumptions()),
        )
        _finalize_metrics(result, self._dataset, self._config)
        return result

    # -- per-session steps ----------------------------------------------------

    def _apply_rebalance(self, state: _RunState, today: date) -> None:
        selection = self._universe_policy.select(
            self._dataset,
            asof=today,
            config=self._config,
        )
        state.universe = list(selection.symbols)
        state.rebalance_log.append(
            {
                "session": today.isoformat(),
                "universe": state.universe,
                **selection.detail,
            }
        )
        for symbol in list(state.open_trades):
            if symbol not in state.universe:
                state.pending_exits.setdefault(
                    symbol,
                    _PendingExit(
                        reason="universe_rotation",
                        remaining_delay_sessions=self._execution_cost_policy.latency_sessions,
                    ),
                )
        for symbol in state.universe:
            if symbol not in state.fed:
                self._preload(symbol, before=today)
                state.fed.add(symbol)

    def _apply_corporate_actions(self, state: _RunState, today: date) -> None:
        for action in sorted(
            self._actions_by_session.get(today, ()),
            key=lambda item: (item.symbol, item.kind.value, item.source_observation_id),
        ):
            application = self._corporate_action_policy.apply(action)
            if (
                not math.isfinite(application.share_multiplier)
                or application.share_multiplier <= 0.0
                or not math.isfinite(application.cash_per_pre_action_share)
                or application.cash_per_pre_action_share < 0.0
                or not math.isfinite(application.receivable_per_pre_action_share)
                or application.receivable_per_pre_action_share < 0.0
            ):
                msg = f"invalid corporate-action application for {action.symbol}"
                raise EquityPortfolioError(msg)
            trade = state.open_trades.get(action.symbol)
            pre_action_shares = trade.shares if trade is not None else 0.0
            cash_payment = pre_action_shares * application.cash_per_pre_action_share
            receivable_credit = pre_action_shares * application.receivable_per_pre_action_share
            if trade is not None and not application.encoded_in_adjusted_prices:
                trade.shares *= application.share_multiplier
                trade.entry_fill /= application.share_multiplier
                if trade.entry_reference is not None:
                    trade.entry_reference /= application.share_multiplier
                for lot in state.open_trade_lots.get(action.symbol, ()):
                    lot.shares *= application.share_multiplier
                    lot.entry_fill /= application.share_multiplier
                    if lot.entry_reference is not None:
                        lot.entry_reference /= application.share_multiplier
                state.cash += cash_payment
                state.gross_cash += cash_payment
                state.dividend_receivable += receivable_credit
                state.gross_dividend_receivable += receivable_credit
            state.corporate_action_ledger.append(
                {
                    "cash_available_session": None,
                    "cash_payment": cash_payment,
                    "currency": action.currency,
                    "encoded_in_adjusted_prices": application.encoded_in_adjusted_prices,
                    "kind": action.kind.value,
                    "position_affected": trade is not None,
                    "receivable_credit": receivable_credit,
                    "session": today.isoformat(),
                    "share_multiplier": application.share_multiplier,
                    "source_observation_id": action.source_observation_id,
                    "symbol": action.symbol,
                    "value": action.value,
                }
            )

    def _fill_exits(
        self,
        state: _RunState,
        today: date,
        *,
        stressed: bool,
    ) -> None:
        still_pending: dict[str, _PendingExit] = {}
        for symbol, pending in sorted(state.pending_exits.items()):
            if pending.remaining_delay_sessions > 0:
                still_pending[symbol] = _PendingExit(
                    reason=pending.reason,
                    remaining_delay_sessions=pending.remaining_delay_sessions - 1,
                )
                continue
            trade = state.open_trades.get(symbol)
            if trade is None:
                continue
            bar = self._bars_by_symbol[symbol].get(today)
            if bar is None:
                if self._has_future_bar(symbol, today):
                    still_pending[symbol] = pending
                continue
            state.open_trades.pop(symbol)
            available_daily_notional = self._available_daily_notional(
                symbol,
                before=today,
            )
            reference = self._account_open(bar)
            fill = reference
            fill = self._execution_cost_policy.exit_fill(
                reference_price=fill,
                reference_notional=fill * trade.shares,
                available_daily_notional=available_daily_notional,
                session=today,
                stressed=stressed,
            )
            costs = self._execution_cost_policy.cost_components(
                reference_price=reference,
                execution_price=fill,
                quantity=trade.shares,
                reference_notional=reference * trade.shares,
                available_daily_notional=available_daily_notional,
                session=today,
                side="sell",
                stressed=stressed,
            )
            self._book_exit(
                state,
                trade,
                today,
                fill,
                reference=reference,
                costs=costs,
                reason=pending.reason,
                available_daily_notional=available_daily_notional,
            )
        state.pending_exits = still_pending

    def _fill_entries(  # noqa: PLR0912, PLR0915 - explicit reduction-first account ledger
        self,
        state: _RunState,
        today: date,
        *,
        stressed: bool,
    ) -> None:
        still_pending: list[_PendingEntry] = []
        actionable: list[tuple[_PendingEntry, DailyBar]] = []
        for pending in sorted(
            state.pending_entries,
            key=lambda item: (-item.signal.confidence, item.signal.symbol),
        ):
            if pending.remaining_delay_sessions > 0:
                still_pending.append(
                    _PendingEntry(
                        signal=pending.signal,
                        remaining_delay_sessions=pending.remaining_delay_sessions - 1,
                    )
                )
                continue
            signal = pending.signal
            symbol = signal.symbol
            bar = self._bars_by_symbol[symbol].get(today)
            if symbol not in state.universe:
                state.skipped_entries += 1
                continue
            if bar is None:
                if self._has_future_bar(symbol, today):
                    still_pending.append(pending)
                else:
                    state.skipped_entries += 1
                continue
            actionable.append((pending, bar))

        increases: list[tuple[_PendingEntry, DailyBar]] = []
        for pending, bar in actionable:
            signal = pending.signal
            symbol = signal.symbol
            existing = state.open_trades.get(symbol)
            reference_open = self._account_open(bar)
            existing_notional = existing.shares * reference_open if existing is not None else 0.0
            target_notional = self._target_allocation_policy.entry_notional(
                signal=signal,
                previous_equity=state.prev_equity,
                available_cash=state.cash + existing_notional,
            )
            if not math.isfinite(target_notional) or target_notional < 0.0:
                msg = f"target-allocation policy returned invalid notional for {symbol}"
                raise EquityPortfolioError(msg)
            delta_notional = target_notional - existing_notional
            if delta_notional < 0.0:
                tolerance = _target_notional_tolerance(
                    signal,
                    target_notional=target_notional,
                )
                if -delta_notional <= tolerance:
                    state.skipped_entries += 1
                    continue
                if existing is None:
                    state.skipped_entries += 1
                    continue
                self._reduce_to_target(
                    state,
                    existing,
                    bar,
                    today,
                    reduction_reference_notional=-delta_notional,
                    target_notional=target_notional,
                    stressed=stressed,
                )
                continue
            increases.append((pending, bar))

        for pending, bar in increases:
            signal = pending.signal
            symbol = signal.symbol
            existing = state.open_trades.get(symbol)
            reference_open = self._account_open(bar)
            existing_notional = existing.shares * reference_open if existing is not None else 0.0
            target_notional = self._target_allocation_policy.entry_notional(
                signal=signal,
                previous_equity=state.prev_equity,
                available_cash=state.cash + existing_notional,
            )
            if not math.isfinite(target_notional) or target_notional < 0.0:
                msg = f"target-allocation policy returned invalid notional for {symbol}"
                raise EquityPortfolioError(msg)
            delta_notional = target_notional - existing_notional
            tolerance = _target_notional_tolerance(
                signal,
                target_notional=target_notional,
            )
            notional = min(delta_notional, state.cash)
            if notional <= tolerance:
                state.skipped_entries += 1
                continue
            available_daily_notional = self._available_daily_notional(
                symbol,
                before=today,
            )
            fill = self._execution_cost_policy.entry_fill(
                reference_open=reference_open,
                reference_notional=notional,
                available_daily_notional=available_daily_notional,
                session=today,
                stressed=stressed,
            )
            state.cash -= notional
            shares = notional / fill
            state.gross_cash -= shares * reference_open
            costs = self._execution_cost_policy.cost_components(
                reference_price=reference_open,
                execution_price=fill,
                quantity=shares,
                reference_notional=notional,
                available_daily_notional=available_daily_notional,
                session=today,
                side="buy",
                stressed=stressed,
            )
            state.cost_ledger.append(
                _cost_ledger_entry(
                    session=today,
                    symbol=symbol,
                    side="buy",
                    reference_price=reference_open,
                    execution_price=fill,
                    quantity=shares,
                    components=costs,
                    available_daily_notional=available_daily_notional,
                )
            )
            entry_lot = Trade(
                symbol=symbol,
                entry_session=today,
                entry_fill=fill,
                shares=shares,
                entry_session_ordinal=self._session_index[today],
                entry_reference=reference_open,
                entry_cost=float(costs["total"]),
            )
            state.open_trade_lots.setdefault(symbol, []).append(entry_lot)
            if existing is None:
                state.open_trades[symbol] = Trade(
                    symbol=entry_lot.symbol,
                    entry_session=entry_lot.entry_session,
                    entry_fill=entry_lot.entry_fill,
                    shares=entry_lot.shares,
                    entry_session_ordinal=entry_lot.entry_session_ordinal,
                    entry_reference=entry_lot.entry_reference,
                    entry_cost=entry_lot.entry_cost,
                )
            else:
                previous_shares = existing.shares
                combined_shares = previous_shares + shares
                previous_reference = existing.entry_reference
                existing.entry_fill = (
                    existing.entry_fill * previous_shares + fill * shares
                ) / combined_shares
                existing.entry_reference = (
                    (previous_reference or existing.entry_fill) * previous_shares
                    + reference_open * shares
                ) / combined_shares
                existing.entry_cost += float(costs["total"])
                existing.shares = combined_shares
            state.rebalance_log.append(
                {
                    "event": "target_buy",
                    "execution_notional": notional,
                    "session": today.isoformat(),
                    "symbol": symbol,
                    "target_notional": target_notional,
                }
            )
        state.pending_entries = still_pending

    def _reduce_to_target(
        self,
        state: _RunState,
        trade: Trade,
        bar: DailyBar,
        session: date,
        *,
        reduction_reference_notional: float,
        target_notional: float,
        stressed: bool,
    ) -> None:
        reference_open = self._account_open(bar)
        quantity = min(trade.shares, reduction_reference_notional / reference_open)
        if not math.isfinite(quantity) or quantity <= 0.0:
            msg = f"target reduction quantity is invalid for {trade.symbol}"
            raise EquityPortfolioError(msg)
        reference_notional = reference_open * quantity
        available_daily_notional = self._available_daily_notional(
            trade.symbol,
            before=session,
        )
        fill = self._execution_cost_policy.exit_fill(
            reference_price=reference_open,
            reference_notional=reference_notional,
            available_daily_notional=available_daily_notional,
            session=session,
            stressed=stressed,
        )
        costs = self._execution_cost_policy.cost_components(
            reference_price=reference_open,
            execution_price=fill,
            quantity=quantity,
            reference_notional=reference_notional,
            available_daily_notional=available_daily_notional,
            session=session,
            side="sell",
            stressed=stressed,
        )
        original_shares = trade.shares
        fraction = quantity / original_shares
        closed = Trade(
            symbol=trade.symbol,
            entry_session=trade.entry_session,
            entry_fill=trade.entry_fill,
            shares=quantity,
            entry_session_ordinal=trade.entry_session_ordinal,
            entry_reference=trade.entry_reference,
            entry_cost=trade.entry_cost * fraction,
        )
        trade.shares = original_shares - quantity
        trade.entry_cost *= 1.0 - fraction
        self._book_exit(
            state,
            closed,
            session,
            fill,
            reference=reference_open,
            costs=costs,
            reason="target_rebalance",
            available_daily_notional=available_daily_notional,
        )
        if trade.shares <= _TARGET_NOTIONAL_REL_TOLERANCE:
            state.open_trades.pop(trade.symbol, None)
        state.rebalance_log.append(
            {
                "event": "target_reduce",
                "execution_notional": reference_notional,
                "session": session.isoformat(),
                "symbol": trade.symbol,
                "target_notional": target_notional,
            }
        )

    def _feed_session(
        self,
        state: _RunState,
        today: date,
        idx: int,
    ) -> None:
        effective_symbols = tuple(sorted(set(state.universe) | set(state.open_trades)))
        panel_bars: dict[str, DailyBar] = {}
        for symbol in effective_symbols:
            # A membership-derived universe symbol can carry no bars at all
            # when the loader dropped it as an owner-registered panel
            # exclusion. It is unpriceable, so it is never fed and never
            # selectable, exactly like a symbol without a bar today.
            bar = self._bars_by_symbol.get(symbol, {}).get(today)
            if bar is not None:
                panel_bars[symbol] = bar
                self._feed(symbol, bar, idx)
                continue
            if symbol in state.open_trades and not self._has_future_bar(symbol, today):
                self._apply_terminal_disposition(state, symbol, today)
        self._panel_context_policy.on_panel(
            strategy=self._strategy,
            panel=EquitySessionPanel(
                session=today,
                session_index=idx,
                effective_symbols=effective_symbols,
                bars=panel_bars,
                panel_input=self._dataset.panel_inputs.get(today),
            ),
        )

    def _collect_signals(self, state: _RunState) -> None:
        for signal in self._emitter.get_signals():
            if signal.action in {SignalAction.LONG, SignalAction.HOLD}:
                state.pending_entries.append(
                    _PendingEntry(
                        signal=signal,
                        remaining_delay_sessions=self._execution_cost_policy.latency_sessions,
                    )
                )
            elif signal.action == SignalAction.CLOSE and signal.symbol in state.open_trades:
                reason = (signal.metadata or {}).get("exit_reason", "strategy_close")
                state.pending_exits.setdefault(
                    signal.symbol,
                    _PendingExit(
                        reason=reason,
                        remaining_delay_sessions=self._execution_cost_policy.latency_sessions,
                    ),
                )
        self._emitter.clear()

    def _mark_session(self, state: _RunState, today: date) -> None:
        marked_values = {
            symbol: trade.shares * self._mark(symbol, today)
            for symbol, trade in state.open_trades.items()
        }
        gross_invested_notional = sum(abs(value) for value in marked_values.values())
        net_invested_notional = sum(marked_values.values())
        equity = state.cash + state.dividend_receivable + net_invested_notional
        gross_equity = state.gross_cash + state.gross_dividend_receivable + net_invested_notional
        if state.cash < _CASH_EPSILON:
            msg = f"cash went negative on {today}: {state.cash}"
            raise EquityPortfolioError(msg)
        if state.gross_cash < _CASH_EPSILON:
            msg = f"gross cash went negative on {today}: {state.gross_cash}"
            raise EquityPortfolioError(msg)
        if not math.isfinite(equity) or equity <= 0.0:
            msg = f"marked net equity is invalid on {today}: {equity}"
            raise EquityPortfolioError(msg)
        if not math.isfinite(gross_equity) or gross_equity <= 0.0:
            msg = f"marked gross equity is invalid on {today}: {gross_equity}"
            raise EquityPortfolioError(msg)
        timestamp = datetime.combine(today, _BAR_STAMP)
        state.equity_curve.append((timestamp, equity))
        state.gross_equity_curve.append((timestamp, gross_equity))
        state.daily_exposures.append(
            DailyPortfolioExposure(
                timestamp=timestamp,
                gross_invested_notional=gross_invested_notional,
                net_invested_notional=net_invested_notional,
                gross_exposure_ratio=gross_invested_notional / equity,
                net_exposure_ratio=net_invested_notional / equity,
            )
        )
        state.prev_equity = equity
        state.prev_vix_pct = self._vix_pct.get(today)

    def _terminal_positions(
        self,
        state: _RunState,
        final_session: date,
    ) -> list[EquityTerminalPosition]:
        positions: list[EquityTerminalPosition] = []
        for symbol, trade in sorted(state.open_trades.items()):
            lots = state.open_trade_lots.get(symbol, ())
            lot_quantity = sum(lot.shares for lot in lots)
            tolerance = max(_TARGET_NOTIONAL_REL_TOLERANCE, trade.shares * 1e-10)
            if not math.isclose(lot_quantity, trade.shares, rel_tol=1e-10, abs_tol=tolerance):
                msg = f"terminal FIFO quantity does not reconcile for {symbol}"
                raise EquityPortfolioError(msg)
            if not lots:
                msg = f"terminal gross entry reference is missing for {symbol}"
                raise EquityPortfolioError(msg)
            entry_fill = sum(lot.entry_fill * lot.shares for lot in lots) / lot_quantity
            entry_reference_numerator = 0.0
            for lot in lots:
                if lot.entry_reference is None:
                    msg = f"terminal gross entry reference is missing for {symbol}"
                    raise EquityPortfolioError(msg)
                entry_reference_numerator += lot.entry_reference * lot.shares
            entry_reference = entry_reference_numerator / lot_quantity
            entry_cost = sum(lot.entry_cost for lot in lots)
            mark = self._mark(symbol, final_session)
            positions.append(
                EquityTerminalPosition(
                    symbol=symbol,
                    entry_session=min(lot.entry_session for lot in lots),
                    quantity=lot_quantity,
                    entry_fill=entry_fill,
                    entry_reference=entry_reference,
                    entry_cost=entry_cost,
                    mark_session=final_session,
                    mark_price=mark,
                    market_value=mark * lot_quantity,
                    gross_unrealized_pnl=(mark - entry_reference) * lot_quantity,
                    net_unrealized_pnl=(mark - entry_fill) * lot_quantity,
                )
            )
        return positions

    def _terminal_liquidation_sensitivity(
        self,
        state: _RunState,
        positions: Sequence[EquityTerminalPosition],
        *,
        stressed: bool,
    ) -> dict[str, Any] | None:
        if not positions:
            return None
        estimates: list[dict[str, Any]] = []
        total_cost = 0.0
        for position in positions:
            available_daily_notional = self._available_daily_notional(
                position.symbol,
                before=position.mark_session,
            )
            fill = self._execution_cost_policy.exit_fill(
                reference_price=position.mark_price,
                reference_notional=position.market_value,
                available_daily_notional=available_daily_notional,
                session=position.mark_session,
                stressed=stressed,
            )
            costs = self._execution_cost_policy.cost_components(
                reference_price=position.mark_price,
                execution_price=fill,
                quantity=position.quantity,
                reference_notional=position.market_value,
                available_daily_notional=available_daily_notional,
                session=position.mark_session,
                side="sell",
                stressed=stressed,
            )
            estimate = _cost_ledger_entry(
                session=position.mark_session,
                symbol=position.symbol,
                side="sell",
                reference_price=position.mark_price,
                execution_price=fill,
                quantity=position.quantity,
                components=costs,
                available_daily_notional=available_daily_notional,
                event_kind="hypothetical_terminal_liquidation",
                turnover_eligible=False,
            )
            estimates.append(estimate)
            total_cost += float(costs["total"])
        marked_terminal_nav = state.equity_curve[-1][1]
        return {
            "actionable": False,
            "estimated_liquidated_nav": marked_terminal_nav - total_cost,
            "estimated_total_cost": total_cost,
            "marked_terminal_nav": marked_terminal_nav,
            "policy_id": EQUITY_TERMINAL_LIQUIDATION_SENSITIVITY_POLICY_ID,
            "positions": estimates,
            "primary_metrics_affected": False,
            "terminal_session": positions[0].mark_session.isoformat(),
            "turnover_affected": False,
        }

    def _apply_terminal_disposition(
        self,
        state: _RunState,
        symbol: str,
        today: date,
    ) -> None:
        available = [
            disposition
            for disposition in self._terminal_dispositions.get(symbol, ())
            if (symbol, disposition.source_observation_id)
            not in state.applied_terminal_dispositions
            and disposition.effective_session <= today
        ]
        if len(available) > 1:
            msg = f"ambiguous terminal disposition history for held symbol {symbol}"
            raise EquityPortfolioError(msg)
        if not available:
            future = [
                disposition
                for disposition in self._terminal_dispositions.get(symbol, ())
                if (symbol, disposition.source_observation_id)
                not in state.applied_terminal_dispositions
                and today < disposition.effective_session <= self._config.eval_end
            ]
            if future:
                return
            msg = (
                f"held symbol {symbol} has no future bar or verified terminal "
                f"disposition as of {today}"
            )
            raise EquityPortfolioError(msg)

        disposition = available[0]
        trade = state.open_trades.pop(symbol)
        settlement_price = disposition.settlement_price
        zero_costs = {
            "commission": 0.0,
            "half_spread": 0.0,
            "impact": 0.0,
            "regulatory_fee": 0.0,
            "slippage": 0.0,
            "total": 0.0,
        }
        self._book_exit(
            state,
            trade,
            today,
            settlement_price,
            reference=settlement_price,
            costs=zero_costs,
            reason=f"terminal_{disposition.kind.value}",
            event_kind="verified_terminal_disposition",
            turnover_eligible=False,
        )
        state.pending_exits.pop(symbol, None)
        state.pending_entries = [
            pending for pending in state.pending_entries if pending.signal.symbol != symbol
        ]
        state.applied_terminal_dispositions.add((symbol, disposition.source_observation_id))
        state.terminal_disposition_ledger.append(
            {
                "effective_session": disposition.effective_session.isoformat(),
                "kind": disposition.kind.value,
                "position_affected": True,
                "quantity": trade.shares,
                "settlement_cash": settlement_price * trade.shares,
                "settlement_price": settlement_price,
                "source_observation_id": disposition.source_observation_id,
                "symbol": symbol,
            }
        )

    def _book_exit(
        self,
        state: _RunState,
        trade: Trade,
        session: date,
        fill: float,
        *,
        reference: float,
        costs: Mapping[str, float],
        reason: str,
        available_daily_notional: float | None = None,
        event_kind: str = "execution",
        turnover_eligible: bool = True,
    ) -> None:
        closed_lots = EquityPortfolioBacktest._close_fifo_lots(
            state,
            symbol=trade.symbol,
            quantity=trade.shares,
            session=session,
            session_ordinal=self._session_index[session],
            fill=fill,
            reference=reference,
            exit_cost=float(costs["total"]),
            reason=reason,
        )
        state.cash += fill * trade.shares
        state.gross_cash += reference * trade.shares
        state.cost_ledger.append(
            _cost_ledger_entry(
                session=session,
                symbol=trade.symbol,
                side="sell",
                reference_price=reference,
                execution_price=fill,
                quantity=trade.shares,
                components=costs,
                available_daily_notional=available_daily_notional,
                event_kind=event_kind,
                turnover_eligible=turnover_eligible,
            )
        )
        state.trades.extend(closed_lots)

    @staticmethod
    def _close_fifo_lots(
        state: _RunState,
        *,
        symbol: str,
        quantity: float,
        session: date,
        session_ordinal: int,
        fill: float,
        reference: float,
        exit_cost: float,
        reason: str,
    ) -> list[Trade]:
        """Close exact entry lots so diagnostic statistics match production FIFO."""

        if not math.isfinite(quantity) or quantity <= 0.0:
            msg = f"FIFO exit quantity is invalid for {symbol}"
            raise EquityPortfolioError(msg)
        lots = state.open_trade_lots.get(symbol)
        if not lots:
            msg = f"FIFO entry lineage is missing for held symbol {symbol}"
            raise EquityPortfolioError(msg)
        available = sum(lot.shares for lot in lots)
        tolerance = max(_TARGET_NOTIONAL_REL_TOLERANCE, quantity * 1e-10)
        if available + tolerance < quantity:
            msg = f"FIFO entry quantity is insufficient for {symbol}"
            raise EquityPortfolioError(msg)

        remaining = quantity
        allocated_exit_cost = 0.0
        closed: list[Trade] = []
        while remaining > tolerance and lots:
            lot = lots[0]
            take = min(lot.shares, remaining)
            fraction = take / lot.shares
            entry_cost = lot.entry_cost * fraction
            is_final = remaining - take <= tolerance
            lot_exit_cost = (
                exit_cost - allocated_exit_cost if is_final else exit_cost * take / quantity
            )
            allocated_exit_cost += lot_exit_cost
            closed.append(
                Trade(
                    symbol=symbol,
                    entry_session=lot.entry_session,
                    entry_fill=lot.entry_fill,
                    shares=take,
                    entry_session_ordinal=lot.entry_session_ordinal,
                    entry_reference=lot.entry_reference,
                    entry_cost=entry_cost,
                    exit_session=session,
                    exit_session_ordinal=session_ordinal,
                    exit_fill=fill,
                    exit_reference=reference,
                    exit_cost=lot_exit_cost,
                    exit_reason=reason,
                )
            )
            lot.shares -= take
            lot.entry_cost -= entry_cost
            remaining -= take
            if lot.shares <= tolerance:
                lots.pop(0)
        if remaining > tolerance:
            msg = f"FIFO exit could not reconcile the requested quantity for {symbol}"
            raise EquityPortfolioError(msg)
        if not lots:
            state.open_trade_lots.pop(symbol, None)
        return closed

    # -- feeding helpers ----------------------------------------------------

    def _feed(self, symbol: str, bar: DailyBar, session_index: int) -> None:
        metadata = dict(
            self._panel_context_policy.metadata(
                dataset=self._dataset,
                symbol=symbol,
                bar=bar,
                session_index=session_index,
                vix_percentile=self._vix_pct.get(bar.session),
                earnings_distance=self._earnings_at[symbol](session_index),
            )
        )
        self._strategy.record_bar(symbol)
        self._strategy.on_data(self._market_state(symbol, bar, metadata))

    def _preload(self, symbol: str, *, before: date) -> None:
        """Warm a symbol's indicators over its prior history, emission-suppressed.

        Full metadata is supplied (it is all historical fact at ``before``);
        ``bootstrap_history`` suppresses entries and the core's monotonic
        session guard keeps replayed old dates from touching the shared
        benchmark gate on mid-run preloads.
        """
        bars = [b for b in self._dataset.bars.get(symbol, ()) if b.session < before]
        history = bars[-self._config.warmup_sessions :]
        states = []
        for bar in history:
            session_index = self._session_index.get(bar.session)
            if session_index is None:
                continue
            metadata = dict(
                self._panel_context_policy.metadata(
                    dataset=self._dataset,
                    symbol=symbol,
                    bar=bar,
                    session_index=session_index,
                    vix_percentile=self._vix_pct.get(bar.session),
                    earnings_distance=self._earnings_at[symbol](session_index),
                )
            )
            states.append(self._market_state(symbol, bar, metadata))
        self._strategy.bootstrap_history(states)
        self._emitter.clear()

    @staticmethod
    def _market_state(symbol: str, bar: DailyBar, metadata: dict[str, Any]) -> MarketState:
        return MarketState(
            symbol=symbol,
            timestamp=datetime.combine(bar.session, _BAR_STAMP),
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.split_adjusted_volume or 0.0,
            metadata=metadata,
        )

    def _has_future_bar(self, symbol: str, today: date) -> bool:
        return any(bar.session > today for bar in self._dataset.bars.get(symbol, ()))

    def _account_open(self, bar: DailyBar) -> float:
        value = bar.raw_open if self._corporate_action_policy.price_basis == "raw" else bar.open
        if not math.isfinite(value) or value <= 0.0:
            msg = f"invalid {self._corporate_action_policy.price_basis} account open"
            raise EquityPortfolioError(msg)
        return value

    def _account_close(self, bar: DailyBar) -> float:
        value = bar.raw_close if self._corporate_action_policy.price_basis == "raw" else bar.close
        if not math.isfinite(value) or value <= 0.0:
            msg = f"invalid {self._corporate_action_policy.price_basis} account close"
            raise EquityPortfolioError(msg)
        return value

    def _last_close(self, symbol: str, today: date) -> float:
        closes = [
            self._account_close(bar)
            for bar in self._dataset.bars.get(symbol, ())
            if bar.session <= today
        ]
        if not closes:
            msg = f"no close history for held symbol {symbol}"
            raise EquityPortfolioError(msg)
        return closes[-1]

    def _mark(self, symbol: str, today: date) -> float:
        return self._last_close(symbol, today)

    def _available_daily_notional(
        self,
        symbol: str,
        *,
        before: date,
    ) -> float | None:
        value, _observations = median_dollar_adv(
            self._dataset.bars.get(symbol, ()),
            before=before,
            window=self._config.adv_window,
        )
        return value


# ---------------------------------------------------------------------------
# metrics + period tables (§10 report contract)
# ---------------------------------------------------------------------------


def _period_tables(
    equity_curve: Sequence[tuple[datetime, float]],
) -> dict[str, dict[str, float]]:
    monthly: dict[str, float] = {}
    quarterly: dict[str, float] = {}
    yearly: dict[str, float] = {}
    period_start: dict[str, float] = {}
    prev = equity_curve[0][1]
    for stamp, value in equity_curve:
        month = stamp.strftime("%Y-%m")
        quarter = f"{stamp.year}-Q{(stamp.month - 1) // 3 + 1}"
        year = str(stamp.year)
        for key, bucket in ((month, monthly), (quarter, quarterly), (year, yearly)):
            if key not in period_start:
                period_start[key] = prev
            bucket[key] = (value / period_start[key] - 1.0) * 100.0
        prev = value
    return {"monthly": monthly, "quarterly": quarterly, "yearly": yearly}


def _benchmark_stats(
    dataset: EquityDataset,
    equity_curve: Sequence[tuple[datetime, float]],
    *,
    initial_capital: float,
) -> dict[str, float | str]:
    missing_sessions = [
        stamp.date()
        for stamp, _value in equity_curve
        if stamp.date() not in dataset.benchmark_total_return
    ]
    if missing_sessions:
        msg = (
            "benchmark total-return series is incomplete for evaluated sessions: "
            f"{[session.isoformat() for session in missing_sessions]}"
        )
        raise EquityPortfolioError(msg)
    first_session = equity_curve[0][0].date()
    try:
        first_session_index = dataset.sessions.index(first_session)
    except ValueError as exc:
        msg = "first evaluated session is absent from the official session axis"
        raise EquityPortfolioError(msg) from exc
    if first_session_index == 0:
        msg = (
            "benchmark comparison requires the official session immediately before "
            "the first evaluated session"
        )
        raise EquityPortfolioError(msg)
    benchmark_base_session = dataset.sessions[first_session_index - 1]
    try:
        benchmark_base_value = dataset.benchmark_total_return[benchmark_base_session]
    except KeyError as exc:
        msg = (
            "benchmark total-return series lacks the official pre-window base session: "
            f"{benchmark_base_session.isoformat()}"
        )
        raise EquityPortfolioError(msg) from exc
    pairs = [
        (initial_capital, benchmark_base_value),
        *[
            (strategy, dataset.benchmark_total_return[stamp.date()])
            for stamp, strategy in equity_curve
        ],
    ]
    if len(pairs) < _MIN_BENCHMARK_POINTS:
        return {}
    strat_returns = [b[0] / a[0] - 1.0 for a, b in pairwise(pairs)]
    bench_returns = [b[1] / a[1] - 1.0 for a, b in pairwise(pairs)]
    bench_total = (pairs[-1][1] / pairs[0][1] - 1.0) * 100.0
    mean_s = sum(strat_returns) / len(strat_returns)
    mean_b = sum(bench_returns) / len(bench_returns)
    cov = sum(
        (s - mean_s) * (b - mean_b) for s, b in zip(strat_returns, bench_returns, strict=True)
    ) / len(strat_returns)
    var_b = sum((b - mean_b) ** 2 for b in bench_returns) / len(bench_returns)
    beta = cov / var_b if var_b > 0 else 0.0
    alpha_annual = (mean_s - beta * mean_b) * _ANNUALIZATION * 100.0
    bench_values = [b for _, b in pairs]
    return {
        "alpha_annualized_pct": alpha_annual,
        "benchmark_base_session": benchmark_base_session.isoformat(),
        "benchmark_kind": dataset.benchmark_total_return_kind,
        "benchmark_max_drawdown_pct": max_drawdown_pct(bench_values),
        "benchmark_symbol": dataset.benchmark_total_return_symbol,
        "benchmark_total_return_pct": bench_total,
        "beta_vs_benchmark": beta,
    }


def _finalize_metrics(
    result: BacktestResult, dataset: EquityDataset, config: PortfolioConfig
) -> None:
    curve = result.equity_curve
    values = [value for _, value in curve]
    returns = daily_returns(curve)
    closed = [t for t in result.trades if t.exit_fill is not None]
    wins = [t for t in closed if t.pnl > 0]
    losses = [t for t in closed if t.pnl <= 0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    span_days = (curve[-1][0] - curve[0][0]).days or 1
    if len(result.daily_exposures) != len(curve) or any(
        exposure.timestamp != curve_observation[0]
        for exposure, curve_observation in zip(result.daily_exposures, curve, strict=True)
    ):
        msg = "daily exposure ledger does not align with the primary equity curve"
        raise EquityPortfolioError(msg)
    exposure_sessions = sum(
        observation.gross_invested_notional > _TARGET_NOTIONAL_REL_TOLERANCE
        for observation in result.daily_exposures
    )
    gross_exposure_ratios = [
        observation.gross_exposure_ratio for observation in result.daily_exposures
    ]
    net_exposure_ratios = [observation.net_exposure_ratio for observation in result.daily_exposures]
    try:
        traded_notional = two_way_traded_notional(result.cost_ledger)
    except (TypeError, ValueError) as exc:
        raise EquityPortfolioError(str(exc)) from exc
    avg_equity = sum(values) / len(values)
    years = span_days / 365.25
    two_way_turnover = traded_notional / avg_equity / years if years > 0 else 0.0
    time_in_market_pct = 100.0 * exposure_sessions / len(curve)
    result.metrics = {
        "initial_capital": config.initial_capital,
        "final_equity": values[-1],
        "total_return_pct": (values[-1] / values[0] - 1.0) * 100.0,
        "cagr_pct": cagr_pct(values[0], values[-1], days=float(span_days)),
        "sharpe_ratio": sharpe_ratio(returns, annualization_factor=_ANNUALIZATION),
        "sortino_ratio": sortino_ratio(returns, annualization_factor=_ANNUALIZATION),
        "annualized_volatility_pct": annualized_volatility_pct(
            returns,
            annualization_factor=_ANNUALIZATION,
        ),
        "max_drawdown_pct": max_drawdown_pct(values),
        "drawdown_recovery_days": recovery_duration_days(curve),
        "trade_count": float(len(closed)),
        "hit_rate_pct": 100.0 * len(wins) / len(closed) if closed else 0.0,
        "avg_win_pct": sum(t.return_pct for t in wins) / len(wins) if wins else 0.0,
        "avg_loss_pct": sum(t.return_pct for t in losses) / len(losses) if losses else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else float("inf"),
        "average_gross_exposure_pct": (
            100.0 * sum(gross_exposure_ratios) / len(gross_exposure_ratios)
        ),
        "peak_gross_exposure_pct": 100.0 * max(gross_exposure_ratios),
        "average_net_exposure_pct": (100.0 * sum(net_exposure_ratios) / len(net_exposure_ratios)),
        "peak_net_exposure_pct": 100.0 * max(net_exposure_ratios),
        "time_in_market_pct": time_in_market_pct,
        "exposure_pct": time_in_market_pct,
        "two_way_traded_notional": traded_notional,
        "two_way_turnover_x_per_year": two_way_turnover,
        "turnover_x_per_year": two_way_turnover,
        "avg_holding_sessions": (
            sum(t.holding_sessions for t in closed) / len(closed) if closed else 0.0
        ),
        "terminal_position_count": float(len(result.terminal_positions)),
        "terminal_market_value": sum(
            position.market_value for position in result.terminal_positions
        ),
        "terminal_net_unrealized_pnl": sum(
            position.net_unrealized_pnl for position in result.terminal_positions
        ),
        "terminal_dividend_receivable": sum(
            float(row.get("receivable_credit", 0.0)) for row in result.corporate_action_ledger
        ),
        "skipped_entries": float(result.skipped_entries),
    }
    calmar_denominator = result.metrics["max_drawdown_pct"]
    result.metrics["calmar_ratio"] = (
        result.metrics["cagr_pct"] / calmar_denominator if calmar_denominator > 0 else 0.0
    )
    total_costs = sum(float(row["components"]["total"]) for row in result.cost_ledger)
    result.metrics["total_execution_cost"] = total_costs
    result.metrics["execution_cost_pct_initial_capital"] = (
        total_costs / config.initial_capital * 100.0
    )
    result.period_returns = _period_tables(curve)
    result.benchmark = _benchmark_stats(
        dataset,
        curve,
        initial_capital=config.initial_capital,
    )
    result.gross_metrics = _curve_metrics(result.gross_equity_curve)
    result.metrics["gross_to_net_total_return_drag_pct"] = (
        result.gross_metrics["total_return_pct"] - result.metrics["total_return_pct"]
    )


def _curve_metrics(curve: Sequence[tuple[datetime, float]]) -> dict[str, float]:
    values = [value for _, value in curve]
    returns = daily_returns(curve)
    span_days = (curve[-1][0] - curve[0][0]).days or 1
    drawdown = max_drawdown_pct(values)
    cagr = cagr_pct(values[0], values[-1], days=float(span_days))
    return {
        "annualized_volatility_pct": annualized_volatility_pct(
            returns,
            annualization_factor=_ANNUALIZATION,
        ),
        "cagr_pct": cagr,
        "calmar_ratio": cagr / drawdown if drawdown > 0 else 0.0,
        "drawdown_recovery_days": recovery_duration_days(curve),
        "final_equity": values[-1],
        "max_drawdown_pct": drawdown,
        "sharpe_ratio": sharpe_ratio(returns, annualization_factor=_ANNUALIZATION),
        "sortino_ratio": sortino_ratio(returns, annualization_factor=_ANNUALIZATION),
        "total_return_pct": (values[-1] / values[0] - 1.0) * 100.0,
    }


def worst_trades(result: BacktestResult, count: int = 10) -> list[Trade]:
    closed = [t for t in result.trades if t.exit_fill is not None]
    return sorted(closed, key=lambda t: t.pnl)[:count]
