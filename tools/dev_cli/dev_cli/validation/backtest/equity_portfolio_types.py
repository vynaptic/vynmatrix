"""Shared data and policy contracts for US-equity portfolio validation."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Final, Protocol

from lib_strategy.equity_market_factors import validate_split_price_contract
from lib_strategy.panels import OfficialSessionCutoff
from lib_strategy.signals.pure_strategy import PureSignalStrategy

EQUITY_TERMINAL_TREATMENT_POLICY_ID: Final = "mark_to_market_unliquidated"
EQUITY_TERMINAL_LIQUIDATION_SENSITIVITY_POLICY_ID: Final = (
    "non_actionable_hypothetical_immediate_exit_at_terminal_mark_v1"
)
EQUITY_TURNOVER_POLICY_ID: Final = (
    "two_way_execution_notional_over_average_daily_net_nav_annualized_v1"
)
_CURRENCY_CODE_LENGTH: Final = 3


def two_way_traded_notional(cost_ledger: Sequence[Mapping[str, Any]]) -> float:
    """Return eligible buy-plus-sell execution notional from the order ledger."""

    total = 0.0
    for index, row in enumerate(cost_ledger):
        turnover_eligible = row.get("turnover_eligible", True)
        if not isinstance(turnover_eligible, bool):
            msg = f"cost-ledger turnover_eligible must be boolean at row {index}"
            raise TypeError(msg)
        if not turnover_eligible:
            continue
        execution_price = row.get("execution_price")
        quantity = row.get("quantity")
        if (
            isinstance(execution_price, bool)
            or not isinstance(execution_price, (int, float))
            or not math.isfinite(float(execution_price))
            or float(execution_price) <= 0.0
            or isinstance(quantity, bool)
            or not isinstance(quantity, (int, float))
            or not math.isfinite(float(quantity))
            or float(quantity) <= 0.0
        ):
            msg = f"turnover-eligible execution notional is invalid at row {index}"
            raise ValueError(msg)
        total += abs(float(execution_price) * float(quantity))
    return total


class EquityPortfolioError(RuntimeError):
    """Raised on dataset/accounting inconsistencies (fail loud, never patch)."""


@dataclass(frozen=True)
class InstitutionalEquityExecutionAssumptions:
    """Versioned reusable cost assumptions for US-equity validation."""

    commission_bps: float = 0.5
    half_spread_bps: float = 1.0
    slippage_bps: float = 1.5
    impact_bps: float = 1.0
    participation_impact_bps_at_full_daily_volume: float = 20.0
    stress_multiplier: float = 1.5
    version: str = "sp500-rotation-execution-cost-v1"
    historical_basis: str = "configured-flat-fee"
    rejection_assumption: str = "none"
    partial_fill_assumption: str = "full_fill"

    def __post_init__(self) -> None:
        rates = {
            "commission_bps": self.commission_bps,
            "half_spread_bps": self.half_spread_bps,
            "impact_bps": self.impact_bps,
            "participation_impact_bps_at_full_daily_volume": (
                self.participation_impact_bps_at_full_daily_volume
            ),
            "slippage_bps": self.slippage_bps,
        }
        if any(not math.isfinite(value) or value < 0.0 for value in rates.values()):
            msg = "institutional equity cost rates must be finite and non-negative"
            raise ValueError(msg)
        if not math.isfinite(self.stress_multiplier) or self.stress_multiplier < 1.0:
            msg = "institutional equity stress_multiplier must be finite and at least one"
            raise ValueError(msg)
        for field_name in (
            "historical_basis",
            "partial_fill_assumption",
            "rejection_assumption",
            "version",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or value != value.strip():
                msg = f"institutional equity {field_name} must be a canonical string"
                raise ValueError(msg)

    def to_dict(self) -> dict[str, float | str]:
        """Return the exact preregisterable assumption contract."""

        return {
            "commission_bps": self.commission_bps,
            "half_spread_bps": self.half_spread_bps,
            "historical_basis": self.historical_basis,
            "impact_bps": self.impact_bps,
            "partial_fill_assumption": self.partial_fill_assumption,
            "participation_impact_bps_at_full_daily_volume": (
                self.participation_impact_bps_at_full_daily_volume
            ),
            "rejection_assumption": self.rejection_assumption,
            "slippage_bps": self.slippage_bps,
            "stress_multiplier": self.stress_multiplier,
            "version": self.version,
        }


DEFAULT_INSTITUTIONAL_EQUITY_EXECUTION_ASSUMPTIONS = InstitutionalEquityExecutionAssumptions()


@dataclass(frozen=True)
class DailyBar:
    """One session with raw, total-return, and provider split coordinates.

    ``split_adjusted_volume`` is retained exactly as the provider reports it;
    no exact raw-share volume is inferred from its undocumented integer
    rounding convention.
    """

    session: date
    open: float
    high: float
    low: float
    close: float
    raw_open: float
    raw_high: float
    raw_low: float
    raw_close: float
    split_adjusted_open: float
    split_adjusted_high: float
    split_adjusted_low: float
    split_adjusted_close: float
    split_adjusted_volume: float | None
    split_adjustment_factor: float

    def __post_init__(self) -> None:
        if self.split_adjusted_volume is not None and (
            isinstance(self.split_adjusted_volume, bool)
            or not isinstance(self.split_adjusted_volume, (int, float))
            or not math.isfinite(float(self.split_adjusted_volume))
            or float(self.split_adjusted_volume) < 0.0
        ):
            msg = "daily bar split_adjusted_volume must be finite and non-negative"
            raise ValueError(msg)
        for raw_price, adjusted_price in (
            (self.raw_open, self.split_adjusted_open),
            (self.raw_high, self.split_adjusted_high),
            (self.raw_low, self.split_adjusted_low),
            (self.raw_close, self.split_adjusted_close),
        ):
            validate_split_price_contract(
                raw_close=raw_price,
                split_adjusted_close=adjusted_price,
                split_adjustment_factor=self.split_adjustment_factor,
            )


class EquityCorporateActionKind(StrEnum):
    """Supported split/dividend accounting events."""

    CASH_DIVIDEND = "cash_dividend"
    SPLIT = "split"


@dataclass(frozen=True)
class EquityCorporateAction:
    """One content-addressed action effective on an official session."""

    symbol: str
    effective_session: date
    kind: EquityCorporateActionKind
    value: float
    source_observation_id: str
    currency: str | None = None

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            msg = "corporate-action symbol must not be blank"
            raise ValueError(msg)
        if not isinstance(self.kind, EquityCorporateActionKind):
            msg = "corporate-action kind must be an EquityCorporateActionKind"
            raise TypeError(msg)
        if not math.isfinite(self.value) or self.value <= 0.0:
            msg = "corporate-action value must be finite and positive"
            raise ValueError(msg)
        if not self.source_observation_id.strip():
            msg = "corporate-action source_observation_id must not be blank"
            raise ValueError(msg)
        if self.kind is EquityCorporateActionKind.CASH_DIVIDEND:
            if (
                self.currency is None
                or len(self.currency) != _CURRENCY_CODE_LENGTH
                or not self.currency.isalpha()
                or self.currency != self.currency.upper()
            ):
                msg = "cash-dividend currency must be a canonical ISO-style code"
                raise ValueError(msg)
        elif self.currency is not None:
            msg = "split actions must not carry a currency"
            raise ValueError(msg)


class EquityTerminalDispositionKind(StrEnum):
    """Supported verified outcomes after a security stops trading."""

    CASH_SETTLEMENT = "cash_settlement"
    ZERO_RECOVERY = "zero_recovery"


@dataclass(frozen=True)
class EquityTerminalDisposition:
    """Content-addressed terminal value effective on an official session."""

    symbol: str
    effective_session: date
    kind: EquityTerminalDispositionKind
    settlement_price: float
    source_observation_id: str

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            msg = "terminal-disposition symbol must not be blank"
            raise ValueError(msg)
        if not isinstance(self.kind, EquityTerminalDispositionKind):
            msg = "terminal-disposition kind must be an EquityTerminalDispositionKind"
            raise TypeError(msg)
        if not math.isfinite(self.settlement_price) or self.settlement_price < 0.0:
            msg = "terminal-disposition settlement_price must be finite and non-negative"
            raise ValueError(msg)
        if (
            self.kind is EquityTerminalDispositionKind.ZERO_RECOVERY
            and self.settlement_price != 0.0
        ):
            msg = "zero-recovery disposition requires a zero settlement_price"
            raise ValueError(msg)
        if (
            self.kind is EquityTerminalDispositionKind.CASH_SETTLEMENT
            and self.settlement_price <= 0.0
        ):
            msg = "cash-settlement disposition requires a positive settlement_price"
            raise ValueError(msg)
        if not self.source_observation_id.strip():
            msg = "terminal-disposition source_observation_id must not be blank"
            raise ValueError(msg)


@dataclass(frozen=True)
class EquityDataset:
    """Pure input container built from immutable validation snapshots."""

    sessions: list[date]
    bars: Mapping[str, Sequence[DailyBar]]
    benchmark_price: Mapping[date, float]
    benchmark_total_return: Mapping[date, float]
    vix: Mapping[date, float]
    earnings: Mapping[str, Sequence[date]]
    membership: Mapping[str, Sequence[tuple[date, date | None]]]
    benchmark_total_return_kind: str = "unspecified"
    benchmark_total_return_symbol: str = "unspecified"
    official_sessions: Mapping[date, OfficialSessionCutoff] = field(default_factory=dict)
    panel_inputs: Mapping[date, Any] = field(default_factory=dict)
    panel_manifest_sha256: str | None = None
    corporate_actions: Mapping[str, Sequence[EquityCorporateAction]] = field(default_factory=dict)
    terminal_dispositions: Mapping[str, Sequence[EquityTerminalDisposition]] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class CostConfig:
    base_bps: float = 5.0
    stress_multiplier: float = 1.75
    stress_vix_percentile: float = 0.90


@dataclass(frozen=True)
class PortfolioConfig:
    eval_start: date
    eval_end: date
    initial_capital: float = 1_000_000.0
    universe_size: int = 10
    adv_window: int = 126
    warmup_sessions: int = 262
    costs: CostConfig = field(default_factory=CostConfig)


@dataclass(frozen=True)
class UniverseSelection:
    """One effective-universe decision plus its auditable policy detail."""

    symbols: tuple[str, ...]
    detail: Mapping[str, Any]


class UniverseSelectionPolicy(Protocol):
    """Select an effective point-in-time universe without simulator coupling."""

    @property
    def policy_id(self) -> str: ...

    def select(
        self,
        dataset: EquityDataset,
        *,
        asof: date,
        config: PortfolioConfig,
    ) -> UniverseSelection: ...


class RebalanceCadencePolicy(Protocol):
    """Return the registered model-decision sessions."""

    @property
    def policy_id(self) -> str: ...

    def sessions(
        self,
        available_sessions: Sequence[date],
        *,
        start: date,
        end: date,
    ) -> tuple[date, ...]: ...


@dataclass(frozen=True)
class EquitySessionPanel:
    """One complete simulator session offered to a panel-aware adapter."""

    session: date
    session_index: int
    effective_symbols: tuple[str, ...]
    bars: Mapping[str, DailyBar]
    panel_input: Any | None


class PanelContextPolicy(Protocol):
    """Own per-bar context and optional synchronized panel evaluation."""

    @property
    def policy_id(self) -> str: ...

    def metadata(
        self,
        *,
        dataset: EquityDataset,
        symbol: str,
        bar: DailyBar,
        session_index: int,
        vix_percentile: float | None,
        earnings_distance: tuple[int | None, int | None],
    ) -> Mapping[str, Any]: ...

    def on_panel(
        self,
        *,
        strategy: PureSignalStrategy,
        panel: EquitySessionPanel,
    ) -> None: ...


class TargetAllocationPolicy(Protocol):
    """Translate a model target hint into a cash-account notional."""

    @property
    def policy_id(self) -> str: ...

    def entry_notional(
        self,
        *,
        signal: Any,
        previous_equity: float,
        available_cash: float,
    ) -> float: ...


@dataclass(frozen=True)
class CorporateActionApplication:
    """Book adjustment for one action under one declared price policy."""

    share_multiplier: float
    cash_per_pre_action_share: float
    receivable_per_pre_action_share: float
    encoded_in_adjusted_prices: bool


class CorporateActionAccountingPolicy(Protocol):
    """Prevent double counting between raw and total-return-adjusted prices."""

    @property
    def policy_id(self) -> str: ...

    @property
    def price_basis(self) -> str: ...

    def apply(self, action: EquityCorporateAction) -> CorporateActionApplication: ...


class EquityExecutionCostPolicy(Protocol):
    """Apply explicit adverse entry and exit price assumptions."""

    @property
    def policy_id(self) -> str: ...

    @property
    def latency_sessions(self) -> int: ...

    def assumptions(self) -> Mapping[str, Any]: ...

    def entry_fill(
        self,
        *,
        reference_open: float,
        reference_notional: float,
        available_daily_notional: float | None,
        session: date,
        stressed: bool,
    ) -> float: ...

    def exit_fill(
        self,
        *,
        reference_price: float,
        reference_notional: float,
        available_daily_notional: float | None,
        session: date,
        stressed: bool,
    ) -> float: ...

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
    ) -> Mapping[str, float]: ...


@dataclass
class Trade:
    symbol: str
    entry_session: date
    entry_fill: float
    shares: float
    entry_session_ordinal: int | None = None
    entry_reference: float | None = None
    entry_cost: float = 0.0
    exit_session: date | None = None
    exit_session_ordinal: int | None = None
    exit_fill: float | None = None
    exit_reference: float | None = None
    exit_cost: float = 0.0
    exit_reason: str | None = None

    @property
    def pnl(self) -> float:
        if self.exit_fill is None:
            return 0.0
        return (self.exit_fill - self.entry_fill) * self.shares

    @property
    def return_pct(self) -> float:
        if self.exit_fill is None:
            return 0.0
        return (self.exit_fill / self.entry_fill - 1.0) * 100.0

    @property
    def holding_sessions(self) -> int:
        if self.exit_session is None:
            return 0
        if self.entry_session_ordinal is not None and self.exit_session_ordinal is not None:
            return self.exit_session_ordinal - self.entry_session_ordinal
        return (self.exit_session - self.entry_session).days


@dataclass(frozen=True, slots=True)
class DailyPortfolioExposure:
    """Marked directional exposure for one official portfolio session.

    Gross and net invested notionals use the marked position values.  Their
    ratios use the primary net NAV, so the ledger can reproduce average/peak
    exposure and time in market without inferring holdings from closed trades.
    """

    timestamp: datetime
    gross_invested_notional: float
    net_invested_notional: float
    gross_exposure_ratio: float
    net_exposure_ratio: float

    def __post_init__(self) -> None:
        values = {
            "gross_invested_notional": self.gross_invested_notional,
            "net_invested_notional": self.net_invested_notional,
            "gross_exposure_ratio": self.gross_exposure_ratio,
            "net_exposure_ratio": self.net_exposure_ratio,
        }
        if any(not math.isfinite(value) for value in values.values()):
            msg = "daily portfolio exposure values must be finite"
            raise ValueError(msg)
        if self.gross_invested_notional < 0.0 or self.gross_exposure_ratio < 0.0:
            msg = "daily gross exposure values must be non-negative"
            raise ValueError(msg)
        if abs(self.net_invested_notional) > self.gross_invested_notional + 1e-8:
            msg = "daily net invested notional cannot exceed gross invested notional"
            raise ValueError(msg)
        if abs(self.net_exposure_ratio) > self.gross_exposure_ratio + 1e-12:
            msg = "daily net exposure ratio cannot exceed gross exposure ratio"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, float | str]:
        """Return a deterministic JSON-safe exposure observation."""

        return {
            "timestamp": self.timestamp.isoformat(),
            "gross_invested_notional": self.gross_invested_notional,
            "net_invested_notional": self.net_invested_notional,
            "gross_exposure_ratio": self.gross_exposure_ratio,
            "net_exposure_ratio": self.net_exposure_ratio,
        }


@dataclass(frozen=True, slots=True)
class EquityTerminalPosition:
    """Marked, deliberately unliquidated equity position at the run boundary."""

    symbol: str
    entry_session: date
    quantity: float
    entry_fill: float
    entry_reference: float
    entry_cost: float
    mark_session: date
    mark_price: float
    market_value: float
    gross_unrealized_pnl: float
    net_unrealized_pnl: float

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            msg = "terminal-position symbol must not be blank"
            raise ValueError(msg)
        positive = {
            "entry_fill": self.entry_fill,
            "entry_reference": self.entry_reference,
            "mark_price": self.mark_price,
            "market_value": self.market_value,
            "quantity": self.quantity,
        }
        if any(not math.isfinite(value) or value <= 0.0 for value in positive.values()):
            msg = "terminal-position quantities and prices must be finite and positive"
            raise ValueError(msg)
        if not math.isfinite(self.entry_cost) or self.entry_cost < 0.0:
            msg = "terminal-position entry_cost must be finite and non-negative"
            raise ValueError(msg)
        if not math.isfinite(self.gross_unrealized_pnl) or not math.isfinite(
            self.net_unrealized_pnl
        ):
            msg = "terminal-position unrealized PnL must be finite"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, float | str]:
        """Return deterministic terminal mark evidence."""

        return {
            "symbol": self.symbol,
            "entry_session": self.entry_session.isoformat(),
            "quantity": self.quantity,
            "entry_fill": self.entry_fill,
            "entry_reference": self.entry_reference,
            "entry_cost": self.entry_cost,
            "mark_session": self.mark_session.isoformat(),
            "mark_price": self.mark_price,
            "market_value": self.market_value,
            "gross_unrealized_pnl": self.gross_unrealized_pnl,
            "net_unrealized_pnl": self.net_unrealized_pnl,
        }


@dataclass
class BacktestResult:
    equity_curve: list[tuple[datetime, float]]
    gross_equity_curve: list[tuple[datetime, float]]
    trades: list[Trade]
    cost_ledger: list[dict[str, Any]]
    corporate_action_ledger: list[dict[str, Any]]
    terminal_disposition_ledger: list[dict[str, Any]]
    rebalance_log: list[dict[str, Any]]
    skipped_entries: int
    daily_exposures: list[DailyPortfolioExposure] = field(default_factory=list)
    terminal_positions: list[EquityTerminalPosition] = field(default_factory=list)
    terminal_liquidation_sensitivity: dict[str, Any] | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    period_returns: dict[str, dict[str, float]] = field(default_factory=dict)
    benchmark: dict[str, float | str] = field(default_factory=dict)
    gross_metrics: dict[str, float] = field(default_factory=dict)
    policies: dict[str, str] = field(default_factory=dict)
    execution_assumptions: dict[str, Any] = field(default_factory=dict)
