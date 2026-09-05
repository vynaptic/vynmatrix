"""Versioned execution assumptions for deterministic strategy validation.

Every run uses an explicit, auditable cost scenario and the conservative
next-open protective-order policy. These value objects contain no clocks or
randomness, so a frozen configuration always produces the same result.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime

from lib_common.execution_costs import (
    BASIS_POINTS_PER_UNIT,
    ExecutionCostRates,
    basis_points_to_fraction,
)
from lib_data.bars import Bar
from lib_data.dataset import fixed_duration_interval

_COST_BASES = {
    "configured-flat-fee",
    "backward-applied-scenario",
    "historically-measured",
}
_AMBIGUITY_POLICIES = {"ignore", "stop_first"}
_INVALID_BRACKET_POLICIES = {"ignore", "reject_entry"}
_TIMESTAMP_CONTRACTS = {"explicit_interval"}
_SUPPORTED_REJECTION_ASSUMPTIONS = {"none"}
_SUPPORTED_PARTIAL_FILL_ASSUMPTIONS = {"full_fill"}
_TIMESTAMP_SEMANTICS = {"period_start", "period_close"}
_SUPPORTED_EXECUTION_MODELS = {"directional_single_net_position"}


def _finite_non_negative(name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0.0:
        msg = f"{name} must be finite and non-negative, got {value!r}"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ExecutionCostModel:
    """Named, versioned adverse execution-cost assumptions.

    Price costs are applied adversely to each reference fill.  Commission is
    charged on the resulting execution notional.  ``latency_bars`` delays a
    market decision by complete tradable bars; it never permits a same-bar fill.
    Rejection and partial-fill assumptions are recorded explicitly even when a
    deterministic scenario assumes no rejection and full fills.
    """

    scenario_name: str = "configured_flat_fee"
    version: str = "execution-cost-v1"
    commission_bps: float = 10.0
    half_spread_bps: float = 0.0
    slippage_bps: float = 0.0
    impact_bps: float = 0.0
    latency_bars: int = 0
    historical_basis: str = "configured-flat-fee"
    rejection_assumption: str = "none"
    partial_fill_assumption: str = "full_fill"

    def __post_init__(self) -> None:
        if not self.scenario_name.strip():
            msg = "scenario_name must not be blank"
            raise ValueError(msg)
        if not self.version.strip():
            msg = "version must not be blank"
            raise ValueError(msg)
        for name in ("commission_bps", "half_spread_bps", "slippage_bps", "impact_bps"):
            _finite_non_negative(name, float(getattr(self, name)))
        if self.half_spread_bps + self.slippage_bps + self.impact_bps >= float(
            BASIS_POINTS_PER_UNIT
        ):
            msg = "aggregate adverse price costs must be below 10,000 bps"
            raise ValueError(msg)
        if isinstance(self.latency_bars, bool) or not isinstance(self.latency_bars, int):
            msg = "latency_bars must be an integer"
            raise TypeError(msg)
        if self.latency_bars < 0:
            msg = "latency_bars must be non-negative"
            raise ValueError(msg)
        if self.historical_basis not in _COST_BASES:
            msg = f"historical_basis must be one of {sorted(_COST_BASES)}"
            raise ValueError(msg)
        if self.rejection_assumption not in _SUPPORTED_REJECTION_ASSUMPTIONS:
            msg = (
                "rejection_assumption is not implemented; supported values are "
                f"{sorted(_SUPPORTED_REJECTION_ASSUMPTIONS)}"
            )
            raise ValueError(msg)
        if self.partial_fill_assumption not in _SUPPORTED_PARTIAL_FILL_ASSUMPTIONS:
            msg = (
                "partial_fill_assumption is not implemented; supported values are "
                f"{sorted(_SUPPORTED_PARTIAL_FILL_ASSUMPTIONS)}"
            )
            raise ValueError(msg)

    @property
    def adverse_price_bps(self) -> float:
        """Total non-commission price penalty per fill, in basis points."""
        return self.half_spread_bps + self.slippage_bps + self.impact_bps

    @classmethod
    def flat_fee(cls, fee_bps: float) -> ExecutionCostModel:
        """Build a deterministic configured flat-commission assumption."""
        return cls(commission_bps=float(fee_bps))

    def to_paper_rates(self) -> ExecutionCostRates:
        """Convert the representable subset to the runtime paper-broker contract."""
        if self.half_spread_bps != 0.0 or self.impact_bps != 0.0:
            msg = "PaperBroker cannot represent separate half-spread or impact costs"
            raise ValueError(msg)
        return ExecutionCostRates.from_basis_points(
            commission_bps=self.commission_bps,
            slippage_bps=self.slippage_bps,
        )


@dataclass(frozen=True, slots=True)
class FillPolicy:
    """Versioned deterministic fill and protective-order rules.

    The reference executor models one directional net position. It does not
    validate simultaneous quotes, queue priority, inventory skew, or other
    market-making microstructure.
    """

    policy_id: str = "next_open_bracket_v2"
    protective_orders: bool = True
    gap_through_at_open: bool = True
    ambiguity_policy: str = "stop_first"
    invalid_bracket_policy: str = "reject_entry"
    timestamp_contract: str = "explicit_interval"
    execution_model: str = "directional_single_net_position"

    def __post_init__(self) -> None:
        if not self.policy_id.strip():
            msg = "policy_id must not be blank"
            raise ValueError(msg)
        if self.ambiguity_policy not in _AMBIGUITY_POLICIES:
            msg = f"ambiguity_policy must be one of {sorted(_AMBIGUITY_POLICIES)}"
            raise ValueError(msg)
        if self.invalid_bracket_policy not in _INVALID_BRACKET_POLICIES:
            msg = f"invalid_bracket_policy must be one of {sorted(_INVALID_BRACKET_POLICIES)}"
            raise ValueError(msg)
        if self.timestamp_contract not in _TIMESTAMP_CONTRACTS:
            msg = f"timestamp_contract must be one of {sorted(_TIMESTAMP_CONTRACTS)}"
            raise ValueError(msg)
        if self.execution_model not in _SUPPORTED_EXECUTION_MODELS:
            msg = (
                "execution_model is not implemented; supported values are "
                f"{sorted(_SUPPORTED_EXECUTION_MODELS)}"
            )
            raise ValueError(msg)
        if self.protective_orders:
            if not self.gap_through_at_open:
                msg = "protective order policies must model gap-through at the open"
                raise ValueError(msg)
            if self.ambiguity_policy != "stop_first":
                msg = "protective order policies must resolve ambiguity stop-first"
                raise ValueError(msg)
            if self.invalid_bracket_policy != "reject_entry":
                msg = "protective order policies must reject invalid entry brackets"
                raise ValueError(msg)
            if self.timestamp_contract != "explicit_interval":
                msg = "protective order policies require explicit bar intervals"
                raise ValueError(msg)

    @classmethod
    def reference_v2(cls) -> FillPolicy:
        """Conservative next-open policy used by validation campaigns."""
        return cls()


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Execution costs paid across one fill, trade, or simulation."""

    commission: float = 0.0
    half_spread: float = 0.0
    slippage: float = 0.0
    impact: float = 0.0

    def __post_init__(self) -> None:
        for name in ("commission", "half_spread", "slippage", "impact"):
            _finite_non_negative(name, float(getattr(self, name)))

    @property
    def total(self) -> float:
        return self.commission + self.half_spread + self.slippage + self.impact

    def to_dict(self) -> dict[str, float]:
        return {
            "commission": self.commission,
            "half_spread": self.half_spread,
            "slippage": self.slippage,
            "impact": self.impact,
            "total": self.total,
        }

    def __add__(self, other: CostBreakdown) -> CostBreakdown:
        if not isinstance(other, CostBreakdown):
            return NotImplemented
        return CostBreakdown(
            commission=self.commission + other.commission,
            half_spread=self.half_spread + other.half_spread,
            slippage=self.slippage + other.slippage,
            impact=self.impact + other.impact,
        )


def adverse_execution_price(
    reference_price: float,
    *,
    transaction_side: int,
    cost_model: ExecutionCostModel,
) -> float:
    """Apply the simulator's adverse non-commission price movement.

    ``transaction_side`` is ``1`` for a buy and ``-1`` for a sell.  The helper
    is the single implementation used by both fill simulation and terminal
    liquidation evidence.
    """

    if not math.isfinite(reference_price) or reference_price <= 0.0:
        msg = "reference_price must be finite and positive"
        raise ValueError(msg)
    if isinstance(transaction_side, bool) or transaction_side not in {-1, 1}:
        msg = "transaction_side must be -1 or 1"
        raise ValueError(msg)
    adverse_rate = float(basis_points_to_fraction(cost_model.adverse_price_bps))
    execution_price = reference_price * (1.0 + transaction_side * adverse_rate)
    if not math.isfinite(execution_price) or execution_price <= 0.0:
        msg = "adverse execution price must be finite and positive"
        raise ValueError(msg)
    return execution_price


def execution_fill_costs(
    *,
    reference_price: float,
    execution_price: float,
    quantity: float,
    cost_model: ExecutionCostModel,
) -> CostBreakdown:
    """Return the simulator's commission and adverse-price decomposition."""

    for name, value in (
        ("reference_price", reference_price),
        ("execution_price", execution_price),
        ("quantity", quantity),
    ):
        if not math.isfinite(value) or value <= 0.0:
            msg = f"{name} must be finite and positive"
            raise ValueError(msg)
    reference_notional = quantity * reference_price
    commission_rate = float(basis_points_to_fraction(cost_model.commission_bps))
    half_spread_rate = float(basis_points_to_fraction(cost_model.half_spread_bps))
    slippage_rate = float(basis_points_to_fraction(cost_model.slippage_bps))
    impact_rate = float(basis_points_to_fraction(cost_model.impact_bps))
    return CostBreakdown(
        commission=quantity * execution_price * commission_rate,
        half_spread=reference_notional * half_spread_rate,
        slippage=reference_notional * slippage_rate,
        impact=reference_notional * impact_rate,
    )


@dataclass(frozen=True, slots=True)
class ExecutionBarTimes:
    """Explicit tradable interval for one OHLCV bar."""

    open_ts: datetime
    close_ts: datetime

    def __post_init__(self) -> None:
        for name, value in (("open_ts", self.open_ts), ("close_ts", self.close_ts)):
            if value.tzinfo is None or value.utcoffset() is None:
                msg = f"{name} must be timezone-aware"
                raise ValueError(msg)
        if self.open_ts >= self.close_ts:
            msg = "open_ts must be earlier than close_ts"
            raise ValueError(msg)


def _parse_timestamp(value: object, *, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    else:
        msg = f"{name} must be an ISO-8601 string or datetime"
        raise TypeError(msg)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        msg = f"{name} must be timezone-aware"
        raise ValueError(msg)
    return parsed


def execution_bar_times(bar: Bar) -> ExecutionBarTimes:
    """Resolve a bar's explicit tradable open and completed close timestamps.

    Provider rows may be stamped at period start while production consolidated
    bars are stamped at period close.  The distinction is mandatory for the
    reference execution policy so next-open fills cannot be recorded one full
    bar late.  Explicit metadata wins; otherwise the declared semantics and
    strict timeframe determine the interval.
    """

    metadata = bar.metadata
    explicit_open = metadata.get("open_timestamp")
    explicit_close = metadata.get("close_timestamp")
    if (explicit_open is None) != (explicit_close is None):
        msg = "open_timestamp and close_timestamp must be supplied together"
        raise ValueError(msg)
    if explicit_open is not None:
        times = ExecutionBarTimes(
            open_ts=_parse_timestamp(explicit_open, name="open_timestamp"),
            close_ts=_parse_timestamp(explicit_close, name="close_timestamp"),
        )
    else:
        semantics = metadata.get("timestamp_semantics")
        if semantics not in _TIMESTAMP_SEMANTICS:
            msg = (
                "reference execution requires timestamp_semantics to be "
                "period_start or period_close"
            )
            raise ValueError(msg)
        duration = fixed_duration_interval(
            bar.timeframe,
            asset_class=metadata.get("asset_class"),
        )
        times = (
            ExecutionBarTimes(open_ts=bar.timestamp, close_ts=bar.timestamp + duration)
            if semantics == "period_start"
            else ExecutionBarTimes(open_ts=bar.timestamp - duration, close_ts=bar.timestamp)
        )

    semantics = metadata.get("timestamp_semantics")
    expected_stamp = times.open_ts if semantics == "period_start" else times.close_ts
    if semantics in _TIMESTAMP_SEMANTICS and bar.timestamp != expected_stamp:
        msg = f"bar timestamp does not match declared {semantics} timestamp"
        raise ValueError(msg)
    return times


def normalize_execution_bar(bar: Bar) -> Bar:
    """Return an equivalent period-close-stamped bar with explicit interval metadata."""

    times = execution_bar_times(bar)
    metadata = dict(bar.metadata)
    metadata.update(
        {
            "timestamp_semantics": "period_close",
            "open_timestamp": times.open_ts.isoformat(),
            "close_timestamp": times.close_ts.isoformat(),
        }
    )
    return replace(bar, timestamp=times.close_ts, metadata=metadata)
