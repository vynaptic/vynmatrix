"""Reusable portfolio-selection, context, action, and execution-cost policies."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from dev_cli.validation.backtest.equity_portfolio_inputs import (
    _finra_taf_bps,
    _section31_bps,
    is_member_asof,
    rebalance_sessions,
    select_universe,
)
from dev_cli.validation.backtest.equity_portfolio_types import (
    DEFAULT_INSTITUTIONAL_EQUITY_EXECUTION_ASSUMPTIONS,
    CorporateActionApplication,
    CostConfig,
    DailyBar,
    EquityCorporateAction,
    EquityCorporateActionKind,
    EquityDataset,
    EquityPortfolioError,
    EquitySessionPanel,
    InstitutionalEquityExecutionAssumptions,
    PortfolioConfig,
    UniverseSelection,
)
from dev_cli.validation.backtest.execution import ExecutionCostModel
from lib_strategy.panels import SynchronizedPanelStrategy
from lib_strategy.signals.pure_strategy import PureSignalStrategy


@dataclass(frozen=True)
class AdvUniverseSelectionPolicy:
    """Existing PIT median-dollar-ADV universe selection."""

    policy_id: str = "pit_median_dollar_adv_v1"

    def select(
        self,
        dataset: EquityDataset,
        *,
        asof: date,
        config: PortfolioConfig,
    ) -> UniverseSelection:
        symbols, detail = select_universe(dataset, asof=asof, config=config)
        return UniverseSelection(symbols=tuple(symbols), detail=detail)


@dataclass(frozen=True)
class PinnedUniverseSelectionPolicy:
    """A universe pinned from present-day knowledge; contamination control only.

    Selecting today's liquidity leaders and replaying them through history is
    look-ahead by construction: the list encodes which companies survived and
    grew. This policy exists solely to quantify that contamination against the
    point-in-time selection, and its results are never decision-grade.
    """

    symbols: tuple[str, ...]
    policy_id: str = "pinned_present_day_universe_selection_contaminated_v1"

    def select(
        self,
        dataset: EquityDataset,
        *,
        asof: date,
        config: PortfolioConfig,
    ) -> UniverseSelection:
        eligible = tuple(
            symbol
            for symbol in self.symbols
            if symbol in dataset.bars and is_member_asof(dataset.membership, symbol, asof)
        )
        selected = eligible[: config.universe_size]
        return UniverseSelection(
            symbols=selected,
            detail={
                "asof": asof.isoformat(),
                "contamination": "present_day_selection_replayed_through_history",
                "eligible_count": len(eligible),
                "pinned_count": len(self.symbols),
                "policy_id": self.policy_id,
                "selected_count": len(selected),
            },
        )


@dataclass(frozen=True)
class QuarterlyRebalanceCadencePolicy:
    """Existing first-session Jan/Apr/Jul/Oct schedule."""

    policy_id: str = "quarterly_first_session_v1"

    def sessions(
        self,
        available_sessions: Sequence[date],
        *,
        start: date,
        end: date,
    ) -> tuple[date, ...]:
        return tuple(rebalance_sessions(available_sessions, start, end))


@dataclass(frozen=True)
class PanelExecutionUniversePolicy:
    """Feed every acquired security while the panel owns PIT eligibility.

    A synchronized panel already contains the exact effective membership and
    explicit exclusions at each cutoff. Filtering that set a second time in
    the simulator would create a competing universe decision and could
    silently suppress a required close after an index removal.
    """

    policy_id: str = "panel_supplied_pit_universe_v1"

    def select(
        self,
        dataset: EquityDataset,
        *,
        asof: date,
        config: PortfolioConfig,
    ) -> UniverseSelection:
        del config
        if not dataset.panel_inputs:
            msg = "panel-supplied universe requires immutable panel inputs"
            raise EquityPortfolioError(msg)
        symbols = tuple(sorted(dataset.bars))
        if not symbols:
            msg = "panel-supplied universe requires acquired security bars"
            raise EquityPortfolioError(msg)
        return UniverseSelection(
            symbols=symbols,
            detail={
                "asof": asof.isoformat(),
                "eligibility_owner": "revision_pinned_strategy_panel",
            },
        )


@dataclass(frozen=True)
class StaticExecutionUniverseCadencePolicy:
    """Keep the execution feed stable; explicit decisions come from panels."""

    policy_id: str = "static_panel_execution_feed_v1"

    def sessions(
        self,
        available_sessions: Sequence[date],
        *,
        start: date,
        end: date,
    ) -> tuple[date, ...]:
        del available_sessions, start, end
        return ()


@dataclass(frozen=True)
class DefaultPanelContextPolicy:
    """Existing benchmark/VIX/earnings context with no panel callback."""

    policy_id: str = "per_symbol_equity_context_v1"

    def metadata(
        self,
        *,
        dataset: EquityDataset,
        symbol: str,
        bar: DailyBar,
        session_index: int,
        vix_percentile: float | None,
        earnings_distance: tuple[int | None, int | None],
    ) -> Mapping[str, Any]:
        del symbol, session_index
        days_until, days_since = earnings_distance
        metadata: dict[str, Any] = {
            "benchmark_close": dataset.benchmark_price.get(bar.session),
            "days_since_earnings": days_since,
            "days_until_earnings": days_until,
            "vix_percentile": vix_percentile,
        }
        if metadata["benchmark_close"] is None:
            metadata.pop("benchmark_close")
        return metadata

    def on_panel(
        self,
        *,
        strategy: PureSignalStrategy,
        panel: EquitySessionPanel,
    ) -> None:
        del strategy, panel


@dataclass(frozen=True)
class SynchronizedStrategyPanelContextPolicy:
    """Evaluate only explicit immutable panels through the production core."""

    policy_id: str = "synchronized_strategy_panel_v1"
    _metadata_policy: DefaultPanelContextPolicy = field(
        default_factory=DefaultPanelContextPolicy,
        repr=False,
    )

    def metadata(
        self,
        *,
        dataset: EquityDataset,
        symbol: str,
        bar: DailyBar,
        session_index: int,
        vix_percentile: float | None,
        earnings_distance: tuple[int | None, int | None],
    ) -> Mapping[str, Any]:
        return self._metadata_policy.metadata(
            dataset=dataset,
            symbol=symbol,
            bar=bar,
            session_index=session_index,
            vix_percentile=vix_percentile,
            earnings_distance=earnings_distance,
        )

    def on_panel(
        self,
        *,
        strategy: PureSignalStrategy,
        panel: EquitySessionPanel,
    ) -> None:
        if panel.panel_input is None:
            return
        if not isinstance(strategy, SynchronizedPanelStrategy):
            msg = "panel input supplied to a strategy without synchronized-panel support"
            raise EquityPortfolioError(msg)
        strategy.evaluate_panel(panel.panel_input)


@dataclass(frozen=True)
class SignalHintTargetAllocationPolicy:
    """Long-only size-hint target; the execution phase owns cash clipping."""

    policy_id: str = "signal_size_hint_frozen_equity_target_v2"

    def entry_notional(
        self,
        *,
        signal: Any,
        previous_equity: float,
        available_cash: float,
    ) -> float:
        del available_cash
        weight = float(signal.size_hint or 0.0)
        return weight * previous_equity


@dataclass(frozen=True)
class TotalReturnAdjustedCorporateActionPolicy:
    """Audit actions while leaving split/dividend effects in adjusted prices."""

    policy_id: str = "total_return_adjusted_actions_v1"
    price_basis: str = "total_return_adjusted"

    def apply(self, action: EquityCorporateAction) -> CorporateActionApplication:
        del action
        return CorporateActionApplication(
            share_multiplier=1.0,
            cash_per_pre_action_share=0.0,
            receivable_per_pre_action_share=0.0,
            encoded_in_adjusted_prices=True,
        )


@dataclass(frozen=True)
class ExplicitRawCorporateActionPolicy:
    """Apply splits and recognize non-spendable dividend receivables.

    EODHD's dividend history provides ex-dates but not authoritative payment
    dates.  The receivable preserves ex-date total-return accounting without
    making the distribution available to fund another trade prematurely.
    """

    policy_id: str = "explicit_raw_actions_ex_date_receivable_v2"
    price_basis: str = "raw"

    def apply(self, action: EquityCorporateAction) -> CorporateActionApplication:
        if action.kind is EquityCorporateActionKind.SPLIT:
            return CorporateActionApplication(
                share_multiplier=action.value,
                cash_per_pre_action_share=0.0,
                receivable_per_pre_action_share=0.0,
                encoded_in_adjusted_prices=False,
            )
        return CorporateActionApplication(
            share_multiplier=1.0,
            cash_per_pre_action_share=0.0,
            receivable_per_pre_action_share=action.value,
            encoded_in_adjusted_prices=False,
        )


@dataclass(frozen=True)
class LegacyEquityExecutionCostPolicy:
    """Compatibility policy for the existing dated flat-bps simulator."""

    config: CostConfig
    policy_id: str = "legacy_equity_flat_bps_v1"
    latency_sessions: int = 0

    def assumptions(self) -> Mapping[str, Any]:
        return {
            "base_bps": self.config.base_bps,
            "latency_sessions": self.latency_sessions,
            "regulatory_fee_schedule": "sec_section_31_dated_v2",
            "stress_multiplier": self.config.stress_multiplier,
            "stress_vix_percentile": self.config.stress_vix_percentile,
        }

    def entry_fill(
        self,
        *,
        reference_open: float,
        reference_notional: float,
        available_daily_notional: float | None,
        session: date,
        stressed: bool,
    ) -> float:
        del available_daily_notional, reference_notional, session
        multiplier = self.config.stress_multiplier if stressed else 1.0
        return reference_open * (1.0 + self.config.base_bps * multiplier / 10_000.0)

    def exit_fill(
        self,
        *,
        reference_price: float,
        reference_notional: float,
        available_daily_notional: float | None,
        session: date,
        stressed: bool,
    ) -> float:
        del available_daily_notional, reference_notional
        multiplier = self.config.stress_multiplier if stressed else 1.0
        sell_bps = self.config.base_bps * multiplier + _section31_bps(session)
        return reference_price * (1.0 - sell_bps / 10_000.0)

    def cost_components(
        self,
        *,
        reference_price: float,
        execution_price: float,
        quantity: float,
        reference_notional: float,
        available_daily_notional: float | None,
        session: date,
        side: str,
        stressed: bool,
    ) -> Mapping[str, float]:
        del available_daily_notional, reference_notional
        multiplier = self.config.stress_multiplier if stressed else 1.0
        regulatory_fee = (
            reference_price * quantity * _section31_bps(session) / 10_000.0
            if side == "sell"
            else 0.0
        )
        total = abs(execution_price - reference_price) * quantity
        return {
            "commission": 0.0,
            "half_spread": 0.0,
            "impact": 0.0,
            "regulatory_fee": regulatory_fee,
            "slippage": max(
                0.0,
                reference_price * quantity * self.config.base_bps * multiplier / 10_000.0,
            ),
            "total": total,
        }


@dataclass(frozen=True)
class InstitutionalEquityExecutionCostPolicy:
    """Commission, spread, slippage, impact, and dated US sell-side fees."""

    cost_model: ExecutionCostModel
    participation_impact_bps_at_full_daily_volume: float
    stress_multiplier: float
    policy_id: str = "institutional_equity_cost_v1"

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.participation_impact_bps_at_full_daily_volume)
            or self.participation_impact_bps_at_full_daily_volume < 0.0
        ):
            msg = "participation impact coefficient must be finite and non-negative"
            raise ValueError(msg)
        if not math.isfinite(self.stress_multiplier) or self.stress_multiplier < 1.0:
            msg = "institutional cost stress_multiplier must be finite and at least one"
            raise ValueError(msg)

    @property
    def latency_sessions(self) -> int:
        return self.cost_model.latency_bars

    def assumptions(self) -> Mapping[str, Any]:
        return {
            "commission_bps": self.cost_model.commission_bps,
            "half_spread_bps": self.cost_model.half_spread_bps,
            "historical_basis": self.cost_model.historical_basis,
            "impact_bps": self.cost_model.impact_bps,
            "latency_sessions": self.latency_sessions,
            "partial_fill_assumption": self.cost_model.partial_fill_assumption,
            "participation_impact_bps_at_full_daily_volume": (
                self.participation_impact_bps_at_full_daily_volume
            ),
            "regulatory_fee_schedule": "sec_section_31_v2_finra_taf_v1",
            "rejection_assumption": self.cost_model.rejection_assumption,
            "scenario_name": self.cost_model.scenario_name,
            "slippage_bps": self.cost_model.slippage_bps,
            "stress_multiplier": self.stress_multiplier,
            "version": self.cost_model.version,
        }

    def entry_fill(
        self,
        *,
        reference_open: float,
        reference_notional: float,
        available_daily_notional: float | None,
        session: date,
        stressed: bool,
    ) -> float:
        del session
        components = self._component_bps(
            reference_price=reference_open,
            reference_notional=reference_notional,
            available_daily_notional=available_daily_notional,
            stressed=stressed,
            include_regulatory=False,
            session_date=None,
        )
        return reference_open * (1.0 + sum(components.values()) / 10_000.0)

    def exit_fill(
        self,
        *,
        reference_price: float,
        reference_notional: float,
        available_daily_notional: float | None,
        session: date,
        stressed: bool,
    ) -> float:
        components = self._component_bps(
            reference_price=reference_price,
            reference_notional=reference_notional,
            available_daily_notional=available_daily_notional,
            stressed=stressed,
            include_regulatory=True,
            session_date=session,
        )
        return reference_price * (1.0 - sum(components.values()) / 10_000.0)

    def cost_components(
        self,
        *,
        reference_price: float,
        execution_price: float,
        quantity: float,
        reference_notional: float,
        available_daily_notional: float | None,
        session: date,
        side: str,
        stressed: bool,
    ) -> Mapping[str, float]:
        bps = self._component_bps(
            reference_price=reference_price,
            reference_notional=reference_notional,
            available_daily_notional=available_daily_notional,
            stressed=stressed,
            include_regulatory=side == "sell",
            session_date=session,
        )
        notional = reference_price * quantity
        components = {name: notional * rate / 10_000.0 for name, rate in bps.items()}
        total = abs(execution_price - reference_price) * quantity
        return {**components, "total": total}

    def _component_bps(
        self,
        *,
        reference_price: float,
        reference_notional: float,
        available_daily_notional: float | None,
        stressed: bool,
        include_regulatory: bool,
        session_date: date | None,
    ) -> dict[str, float]:
        if (
            not math.isfinite(reference_price)
            or reference_price <= 0.0
            or not math.isfinite(reference_notional)
            or reference_notional < 0.0
        ):
            msg = "institutional execution reference price/notional is invalid"
            raise EquityPortfolioError(msg)
        if (
            available_daily_notional is None
            or not math.isfinite(available_daily_notional)
            or available_daily_notional <= 0.0
        ):
            msg = "institutional execution requires positive cutoff-known trailing daily notional"
            raise EquityPortfolioError(msg)
        participation = reference_notional / available_daily_notional
        participation_impact = self.participation_impact_bps_at_full_daily_volume * math.sqrt(
            min(1.0, max(0.0, participation))
        )
        stress = self.stress_multiplier if stressed else 1.0
        regulatory_bps = 0.0
        if include_regulatory and session_date is not None:
            regulatory_bps = _section31_bps(session_date) + _finra_taf_bps(
                session_date,
                reference_price=reference_price,
                reference_notional=reference_notional,
            )
        return {
            "commission": self.cost_model.commission_bps,
            "half_spread": self.cost_model.half_spread_bps * stress,
            "impact": (self.cost_model.impact_bps + participation_impact) * stress,
            "regulatory_fee": regulatory_bps,
            "slippage": self.cost_model.slippage_bps * stress,
        }


def institutional_equity_execution_cost_policy(
    *,
    assumptions: InstitutionalEquityExecutionAssumptions = (
        DEFAULT_INSTITUTIONAL_EQUITY_EXECUTION_ASSUMPTIONS
    ),
    scenario_name: str,
    market_cost_multiplier: float = 1.0,
    latency_sessions: int = 0,
) -> InstitutionalEquityExecutionCostPolicy:
    """Build one validated policy from the shared preregistered contract."""

    if (
        isinstance(market_cost_multiplier, bool)
        or not isinstance(market_cost_multiplier, (int, float))
        or not math.isfinite(float(market_cost_multiplier))
        or float(market_cost_multiplier) < 1.0
    ):
        msg = "market_cost_multiplier must be finite and at least one"
        raise ValueError(msg)
    if (
        isinstance(latency_sessions, bool)
        or not isinstance(latency_sessions, int)
        or latency_sessions < 0
    ):
        msg = "latency_sessions must be a non-negative integer"
        raise ValueError(msg)
    if (
        not isinstance(scenario_name, str)
        or not scenario_name
        or scenario_name != scenario_name.strip()
    ):
        msg = "scenario_name must be a canonical string"
        raise ValueError(msg)
    multiplier = float(market_cost_multiplier)
    return InstitutionalEquityExecutionCostPolicy(
        cost_model=ExecutionCostModel(
            scenario_name=scenario_name,
            version=assumptions.version,
            commission_bps=assumptions.commission_bps * multiplier,
            half_spread_bps=assumptions.half_spread_bps * multiplier,
            slippage_bps=assumptions.slippage_bps * multiplier,
            impact_bps=assumptions.impact_bps * multiplier,
            latency_bars=latency_sessions,
            historical_basis=assumptions.historical_basis,
            rejection_assumption=assumptions.rejection_assumption,
            partial_fill_assumption=assumptions.partial_fill_assumption,
        ),
        participation_impact_bps_at_full_daily_volume=(
            assumptions.participation_impact_bps_at_full_daily_volume * multiplier
        ),
        stress_multiplier=assumptions.stress_multiplier,
    )
