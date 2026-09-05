"""Reconstructable low-frequency transaction-cost estimates for US equities.

The estimator is intentionally explicit about what is observed and what is
modelled. Daily high, low, and close are observed raw source data. EODHD's
observed volume is split-adjusted, retained as such, and paired with OHLC in a
pinned matching split coordinate. The liquidity denominator removes one
reported adjusted share as an explicit conservative integer-quantization
haircut; it is not labelled exact raw tape volume. The spread is the
Corwin--Schultz two-session high/low estimate in the same split coordinate, so
a split between sessions cannot create a fictitious cross-day range. The non-spread component
combines a frozen implementation-shortfall floor with a square-root
participation term scaled by Parkinson daily volatility. No output from this
module is a quote, fill, or broker observation.

References:
Corwin and Schultz (2012), Journal of Finance 67(2), 719--760.
Parkinson (1980), Journal of Business 53(1), 61--65.
Toth et al. (2011), Physical Review X 1, 021006.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from itertools import pairwise
from typing import NoReturn

from lib_common.hashing import canonical_json_hash

from .equity_market_factors import (
    conservative_split_coordinate_notional,
    validate_split_price_contract,
)

__all__ = [
    "DAILY_BAR_COST_MODEL_VERSION",
    "MODELED_TRANSACTION_COST_SCHEMA",
    "DailyBarCostModelPolicy",
    "DailyBarCostObservation",
    "ModeledDailyTransactionCost",
    "estimate_daily_bar_transaction_costs",
]

DAILY_BAR_COST_MODEL_VERSION = "corwin-schultz-parkinson-sqrt-participation-v2"
MODELED_TRANSACTION_COST_SCHEMA = "vynmatrix.modeled-equity-transaction-cost-observation.v2"
_SPREAD_ESTIMATOR_VERSION = "corwin-schultz-two-session-high-low-v1"
_VOLATILITY_ESTIMATOR_VERSION = "parkinson-daily-range-v1"
_IMPACT_ESTIMATOR_VERSION = "volatility-square-root-participation-v1"
_SHA256_LENGTH = 64
_TWO_SESSION_INPUT_COUNT = 2


class DailyBarCostInputError(ValueError):
    """Observed bar evidence or the frozen modelling policy is invalid."""


def _invalid(message: str) -> NoReturn:
    raise DailyBarCostInputError(message)


def _text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _invalid(f"{field_name} must be canonical non-blank text")
    return value


def _digest(value: object, *, field_name: str) -> str:
    result = _text(value, field_name=field_name)
    if len(result) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in result
    ):
        _invalid(f"{field_name} must be a lowercase SHA-256 digest")
    return result


def _finite(
    value: object,
    *,
    field_name: str,
    minimum: float | None = None,
    strict: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid(f"{field_name} must be finite")
    result = float(value)
    if not math.isfinite(result):
        _invalid(f"{field_name} must be finite")
    if minimum is not None and (result <= minimum if strict else result < minimum):
        comparator = "greater than" if strict else "at least"
        _invalid(f"{field_name} must be {comparator} {minimum}")
    return 0.0 if result == 0.0 else result


def _utc(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DailyBarCostModelPolicy:
    """Frozen assumptions for one reconstructable cost-evidence campaign.

    ``reference_order_notional_usd`` is the order size at which the strategy's
    eligibility cost is evaluated. V1 uses USD 50,000: 0.1% of the strategy's
    USD 50 million liquidity floor and conservatively above an equal-weight
    slot in the registered USD 1 million / 30-holding validation portfolio.

    ``fixed_one_way_nonspread_bps`` covers the separately preregistered base
    slippage and non-participation impact assumptions.  The square-root term is
    added to it; spread and commission remain separate.
    """

    reference_order_notional_usd: float = 50_000.0
    impact_coefficient: float = 1.0
    fixed_one_way_nonspread_bps: float = 2.5
    calculation_version: str = DAILY_BAR_COST_MODEL_VERSION
    configuration_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        reference_notional = _finite(
            self.reference_order_notional_usd,
            field_name="reference_order_notional_usd",
            minimum=0.0,
            strict=True,
        )
        object.__setattr__(self, "reference_order_notional_usd", reference_notional)
        object.__setattr__(
            self,
            "impact_coefficient",
            _finite(
                self.impact_coefficient,
                field_name="impact_coefficient",
                minimum=0.0,
                strict=True,
            ),
        )
        object.__setattr__(
            self,
            "fixed_one_way_nonspread_bps",
            _finite(
                self.fixed_one_way_nonspread_bps,
                field_name="fixed_one_way_nonspread_bps",
                minimum=0.0,
            ),
        )
        if self.calculation_version != DAILY_BAR_COST_MODEL_VERSION:
            _invalid(f"calculation_version must be {DAILY_BAR_COST_MODEL_VERSION!r}")
        object.__setattr__(self, "configuration_sha256", canonical_json_hash(self.to_payload()))

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": "vynmatrix.daily-bar-cost-model-policy.v2",
            "calculation_version": self.calculation_version,
            "spread_estimator_version": _SPREAD_ESTIMATOR_VERSION,
            "volatility_estimator_version": _VOLATILITY_ESTIMATOR_VERSION,
            "impact_estimator_version": _IMPACT_ESTIMATOR_VERSION,
            "reference_order_notional_usd": self.reference_order_notional_usd,
            "impact_coefficient": self.impact_coefficient,
            "fixed_one_way_nonspread_bps": self.fixed_one_way_nonspread_bps,
            "evidence_kind": "modeled_from_observed_daily_bars",
        }


@dataclass(frozen=True, slots=True)
class DailyBarCostObservation:
    """One immutable observed daily bar used only as estimator input."""

    security_id: str
    symbol: str
    session_date: date
    observed_at: datetime
    available_at: datetime
    raw_high: float
    raw_low: float
    raw_close: float
    split_adjusted_high: float
    split_adjusted_low: float
    split_adjusted_close: float
    split_adjusted_volume: float
    split_adjustment_factor: float
    source_observation_id: str
    source_content_sha256: str

    def __post_init__(self) -> None:
        _text(self.security_id, field_name="security_id")
        symbol = _text(self.symbol, field_name="symbol")
        if symbol != symbol.upper() or any(character.isspace() for character in symbol):
            _invalid("symbol must be canonical uppercase text")
        if not isinstance(self.session_date, date):
            _invalid("session_date must be a date")
        observed = _utc(self.observed_at, field_name="observed_at")
        available = _utc(self.available_at, field_name="available_at")
        if available < observed:
            _invalid("available_at cannot precede observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)
        high = _finite(self.raw_high, field_name="raw_high", minimum=0.0, strict=True)
        low = _finite(self.raw_low, field_name="raw_low", minimum=0.0, strict=True)
        close = _finite(self.raw_close, field_name="raw_close", minimum=0.0, strict=True)
        if high < max(low, close) or low > close:
            _invalid("raw daily high/low do not contain the close")
        object.__setattr__(self, "raw_high", high)
        object.__setattr__(self, "raw_low", low)
        object.__setattr__(self, "raw_close", close)
        for raw_price, adjusted_price in (
            (high, self.split_adjusted_high),
            (low, self.split_adjusted_low),
            (close, self.split_adjusted_close),
        ):
            validate_split_price_contract(
                raw_close=raw_price,
                split_adjusted_close=adjusted_price,
                split_adjustment_factor=self.split_adjustment_factor,
            )
        object.__setattr__(
            self,
            "split_adjusted_volume",
            _finite(
                self.split_adjusted_volume,
                field_name="split_adjusted_volume",
                minimum=0.0,
            ),
        )
        _digest(self.source_observation_id, field_name="source_observation_id")
        _digest(self.source_content_sha256, field_name="source_content_sha256")


@dataclass(frozen=True, slots=True)
class ModeledDailyTransactionCost:
    """One explicitly modelled result backed by two observed daily bars."""

    security_id: str
    symbol: str
    session_date: date
    available_at: datetime
    estimated_round_trip_spread_bps: float
    estimated_one_way_nonspread_cost_bps: float
    assumed_order_notional_usd: float
    reference_participation_rate: float
    daily_range_volatility: float
    source_observation_ids: tuple[str, str]
    source_content_sha256s: tuple[str, str]
    cost_context_sha256: str
    observation_id: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.security_id, field_name="security_id")
        _text(self.symbol, field_name="symbol")
        if not isinstance(self.session_date, date):
            _invalid("session_date must be a date")
        object.__setattr__(self, "available_at", _utc(self.available_at, field_name="available_at"))
        for field_name in (
            "estimated_round_trip_spread_bps",
            "estimated_one_way_nonspread_cost_bps",
            "daily_range_volatility",
        ):
            object.__setattr__(
                self,
                field_name,
                _finite(getattr(self, field_name), field_name=field_name, minimum=0.0),
            )
        object.__setattr__(
            self,
            "assumed_order_notional_usd",
            _finite(
                self.assumed_order_notional_usd,
                field_name="assumed_order_notional_usd",
                minimum=0.0,
                strict=True,
            ),
        )
        participation = _finite(
            self.reference_participation_rate,
            field_name="reference_participation_rate",
            minimum=0.0,
            strict=True,
        )
        if participation > 1.0:
            _invalid("reference_participation_rate must not exceed one")
        object.__setattr__(self, "reference_participation_rate", participation)
        if len(set(self.source_observation_ids)) != _TWO_SESSION_INPUT_COUNT:
            _invalid("cost estimate requires two distinct source observations")
        for value in self.source_observation_ids:
            _digest(value, field_name="source_observation_id")
        if len(self.source_content_sha256s) != _TWO_SESSION_INPUT_COUNT:
            _invalid("cost estimate requires two source content digests")
        for value in self.source_content_sha256s:
            _digest(value, field_name="source_content_sha256")
        _digest(self.cost_context_sha256, field_name="cost_context_sha256")
        object.__setattr__(self, "observation_id", canonical_json_hash(self.to_payload()))

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": MODELED_TRANSACTION_COST_SCHEMA,
            "evidence_kind": "modeled_from_observed_daily_bars",
            "security_id": self.security_id,
            "symbol": self.symbol,
            "session": self.session_date.isoformat(),
            "available_at": self.available_at.isoformat(),
            "estimated_round_trip_spread_bps": self.estimated_round_trip_spread_bps,
            "estimated_one_way_nonspread_cost_bps": (self.estimated_one_way_nonspread_cost_bps),
            "assumed_order_notional_usd": self.assumed_order_notional_usd,
            "reference_participation_rate": self.reference_participation_rate,
            "daily_range_volatility": self.daily_range_volatility,
            "source_observation_ids": list(self.source_observation_ids),
            "source_content_sha256s": list(self.source_content_sha256s),
            "cost_context_sha256": self.cost_context_sha256,
        }


def estimate_daily_bar_transaction_costs(
    observations: tuple[DailyBarCostObservation, ...],
    policy: DailyBarCostModelPolicy,
    *,
    cost_context_sha256: str,
) -> tuple[ModeledDailyTransactionCost, ...]:
    """Estimate each session after the first, preserving exact bar lineage."""

    if not isinstance(observations, tuple) or len(observations) < _TWO_SESSION_INPUT_COUNT:
        _invalid("daily-bar cost estimation requires at least two immutable observations")
    if not isinstance(policy, DailyBarCostModelPolicy):
        message = "policy must be a DailyBarCostModelPolicy"
        raise TypeError(message)
    _digest(cost_context_sha256, field_name="cost_context_sha256")
    security_ids = {item.security_id for item in observations}
    symbols = {item.symbol for item in observations}
    if len(security_ids) != 1 or len(symbols) != 1:
        _invalid("daily-bar cost observations must cover one security")
    sessions = tuple(item.session_date for item in observations)
    if sessions != tuple(sorted(sessions)) or len(sessions) != len(set(sessions)):
        _invalid("daily-bar cost observations must be strictly session ordered")
    if any(left.available_at > right.available_at for left, right in pairwise(observations)):
        _invalid("daily-bar cost availability must not move backward")

    denominator = 3.0 - 2.0 * math.sqrt(2.0)
    results: list[ModeledDailyTransactionCost] = []
    for previous, current in pairwise(observations):
        previous_range = math.log(previous.split_adjusted_high / previous.split_adjusted_low)
        current_range = math.log(current.split_adjusted_high / current.split_adjusted_low)
        beta = previous_range**2 + current_range**2
        gamma = (
            math.log(
                max(previous.split_adjusted_high, current.split_adjusted_high)
                / min(previous.split_adjusted_low, current.split_adjusted_low)
            )
            ** 2
        )
        alpha = (math.sqrt(2.0 * beta) - math.sqrt(beta)) / denominator - math.sqrt(
            gamma / denominator
        )
        nonnegative_alpha = max(0.0, alpha)
        estimated_spread_bps = 20_000.0 * math.tanh(nonnegative_alpha / 2.0)
        range_volatility = abs(current_range) / math.sqrt(4.0 * math.log(2.0))
        daily_notional = conservative_split_coordinate_notional(
            current.split_adjusted_close,
            current.split_adjusted_volume,
        )
        reference_participation_rate = (
            1.0
            if daily_notional <= 0.0
            else min(1.0, policy.reference_order_notional_usd / daily_notional)
        )
        participation_impact_bps = (
            policy.impact_coefficient
            * range_volatility
            * math.sqrt(reference_participation_rate)
            * 10_000.0
        )
        results.append(
            ModeledDailyTransactionCost(
                security_id=current.security_id,
                symbol=current.symbol,
                session_date=current.session_date,
                available_at=current.available_at,
                estimated_round_trip_spread_bps=estimated_spread_bps,
                estimated_one_way_nonspread_cost_bps=(
                    policy.fixed_one_way_nonspread_bps + participation_impact_bps
                ),
                assumed_order_notional_usd=policy.reference_order_notional_usd,
                reference_participation_rate=reference_participation_rate,
                daily_range_volatility=range_volatility,
                source_observation_ids=(
                    previous.source_observation_id,
                    current.source_observation_id,
                ),
                source_content_sha256s=(
                    previous.source_content_sha256,
                    current.source_content_sha256,
                ),
                cost_context_sha256=cost_context_sha256,
            )
        )
    return tuple(results)
