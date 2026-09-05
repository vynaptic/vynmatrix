"""Deterministic point-in-time daily market factors for US equities.

Acquisition, database access, provider symbols, and portfolio thresholds remain
outside this domain module.  Callers must supply immutable observations aligned
to an authoritative official-session sequence. Missing spread/non-spread
evidence is preserved as missing. Callers must label and content-bind whether
those inputs are observed or modelled upstream.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from itertools import pairwise
from typing import NoReturn

from lib_common.hashing import canonical_json_hash

from .panels import OfficialSessionCutoff

__all__ = [
    "DailyEquityMarketObservation",
    "EquityMarketFactorInput",
    "EquityMarketFactorPolicy",
    "EquityMarketFactorSnapshot",
    "EquityMarketInputError",
    "InstrumentMarketFactorCalculation",
    "MarketRegimeCalculation",
    "PointInTimeEquitySecurity",
    "StructuralBreadthExclusion",
    "calculate_equity_market_factors",
    "conservative_split_coordinate_notional",
    "validate_split_price_contract",
]

MOMENTUM_FACTOR_VERSION = "intermediate-price-momentum-126d-252d-skip21-current-trend-v2"
MARKET_FACTOR_CALCULATION_VERSION = "us-equity-market-panel-v3"
RISK_FACTOR_VERSION = "split-adjusted-gap-and-downside-semideviation-126d-v1"
LIQUIDITY_FACTOR_VERSION = "median-provider-split-coordinate-notional-126d-v2"
COST_FACTOR_VERSION = "explicit-spread-nonspread-median20-v2"
REGIME_FACTOR_VERSION = "benchmark-trend252-breadth252-vol20-listing-bounds-v2"
STRUCTURAL_BREADTH_EXCLUSION_REASON = "listing_history_warmup"

_SHORT_MOMENTUM_SESSIONS = 126
_LONG_MOMENTUM_SESSIONS = 252
_MOMENTUM_SKIP_SESSIONS = 21
_TREND_SESSIONS = 252
_LIQUIDITY_SESSIONS = 126
_RISK_SESSIONS = 126
_COST_SESSIONS = 20
_VOLATILITY_SESSIONS = 20
_ANNUALIZATION_SESSIONS = 252
_MAXIMUM_STRUCTURAL_BREADTH_EXCLUSION_FRACTION = 0.01
_SHA256_LENGTH = 64
_SPLIT_PRICE_REL_TOLERANCE = 1e-12
_SPLIT_PRICE_ABS_TOLERANCE = 1e-10
_PROVIDER_INTEGER_VOLUME_HAIRCUT = 1.0


class EquityMarketInputError(ValueError):
    """Market evidence is incomplete, future-dated, or internally divergent."""


def _invalid(message: str) -> NoReturn:
    raise EquityMarketInputError(message)


def _text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _invalid(f"{field_name} must be canonical non-blank text")
    return value


def _upper(value: object, *, field_name: str) -> str:
    result = _text(value, field_name=field_name)
    if result != result.upper():
        _invalid(f"{field_name} must be uppercase")
    return result


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _invalid(f"{field_name} must be a positive integer")
    return value


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


def _optional_nonnegative(value: object, *, field_name: str) -> float | None:
    if value is None:
        return None
    return _finite(value, field_name=field_name, minimum=0.0)


def _utc(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _digest(value: object, *, field_name: str) -> str:
    result = _text(value, field_name=field_name)
    if len(result) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in result
    ):
        _invalid(f"{field_name} must be a lowercase SHA-256 digest")
    return result


def _float_token(value: float | None) -> str | None:
    return None if value is None else value.hex()


def validate_split_price_contract(
    *,
    raw_close: object,
    split_adjusted_close: object,
    split_adjustment_factor: object,
) -> None:
    """Fail closed unless a split-adjusted price uses the pinned factor."""

    raw_price = _finite(raw_close, field_name="raw_close", minimum=0.0, strict=True)
    adjusted_price = _finite(
        split_adjusted_close,
        field_name="split_adjusted_close",
        minimum=0.0,
        strict=True,
    )
    factor = _finite(
        split_adjustment_factor,
        field_name="split_adjustment_factor",
        minimum=0.0,
        strict=True,
    )
    expected = raw_price * factor
    if not math.isfinite(expected) or not math.isclose(
        adjusted_price,
        expected,
        rel_tol=_SPLIT_PRICE_REL_TOLERANCE,
        abs_tol=_SPLIT_PRICE_ABS_TOLERANCE,
    ):
        _invalid("split-adjusted close violates the cumulative split-factor contract")


def conservative_split_coordinate_notional(
    split_adjusted_price: object,
    split_adjusted_volume: object,
) -> float:
    """Return a conservative notional in the provider's pinned split basis.

    EODHD documents an integer split-adjusted volume but not its rounding
    convention.  The calculation therefore removes one reported adjusted
    share (one integer quantization unit) before multiplying by a price in the
    exact same split basis.  This is not represented as observed raw volume or
    exact tape notional.
    """

    price = _finite(
        split_adjusted_price,
        field_name="split_adjusted_price",
        minimum=0.0,
        strict=True,
    )
    volume = _finite(
        split_adjusted_volume,
        field_name="split_adjusted_volume",
        minimum=0.0,
    )
    notional = price * max(0.0, volume - _PROVIDER_INTEGER_VOLUME_HAIRCUT)
    if not math.isfinite(notional):
        _invalid("provider split-coordinate notional must be finite")
    return 0.0 if notional == 0.0 else notional


@dataclass(frozen=True, slots=True)
class EquityMarketFactorPolicy:
    """Frozen, economically interpretable market-factor calculation policy.

    ``cost_context_sha256`` identifies the upstream evidence and reference-order
    policy used to produce each immutable spread/non-spread observation. It is
    deliberately required rather than inferred from portfolio AUM.
    """

    round_trip_commission_bps: float
    cost_context_sha256: str
    required_adjustment_policy: str
    calculation_version: str = MARKET_FACTOR_CALCULATION_VERSION
    short_momentum_sessions: int = _SHORT_MOMENTUM_SESSIONS
    long_momentum_sessions: int = _LONG_MOMENTUM_SESSIONS
    momentum_skip_sessions: int = _MOMENTUM_SKIP_SESSIONS
    trend_sessions: int = _TREND_SESSIONS
    liquidity_sessions: int = _LIQUIDITY_SESSIONS
    risk_sessions: int = _RISK_SESSIONS
    cost_sessions: int = _COST_SESSIONS
    volatility_sessions: int = _VOLATILITY_SESSIONS
    annualization_sessions: int = _ANNUALIZATION_SESSIONS
    maximum_structural_breadth_exclusion_fraction: float = (
        _MAXIMUM_STRUCTURAL_BREADTH_EXCLUSION_FRACTION
    )
    configuration_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "round_trip_commission_bps",
            _finite(
                self.round_trip_commission_bps,
                field_name="round_trip_commission_bps",
                minimum=0.0,
            ),
        )
        _digest(self.cost_context_sha256, field_name="cost_context_sha256")
        _text(self.required_adjustment_policy, field_name="required_adjustment_policy")
        if self.calculation_version != MARKET_FACTOR_CALCULATION_VERSION:
            _invalid(f"calculation_version must be {MARKET_FACTOR_CALCULATION_VERSION!r}")
        registered = (
            self.short_momentum_sessions,
            self.long_momentum_sessions,
            self.momentum_skip_sessions,
            self.trend_sessions,
            self.liquidity_sessions,
            self.risk_sessions,
            self.cost_sessions,
            self.volatility_sessions,
            self.annualization_sessions,
        )
        expected = (
            _SHORT_MOMENTUM_SESSIONS,
            _LONG_MOMENTUM_SESSIONS,
            _MOMENTUM_SKIP_SESSIONS,
            _TREND_SESSIONS,
            _LIQUIDITY_SESSIONS,
            _RISK_SESSIONS,
            _COST_SESSIONS,
            _VOLATILITY_SESSIONS,
            _ANNUALIZATION_SESSIONS,
        )
        if registered != expected:
            _invalid("registered daily-market windows cannot be changed within v3")
        structural_limit = _finite(
            self.maximum_structural_breadth_exclusion_fraction,
            field_name="maximum_structural_breadth_exclusion_fraction",
            minimum=0.0,
        )
        if structural_limit != _MAXIMUM_STRUCTURAL_BREADTH_EXCLUSION_FRACTION:
            _invalid(
                "maximum_structural_breadth_exclusion_fraction must remain "
                f"{_MAXIMUM_STRUCTURAL_BREADTH_EXCLUSION_FRACTION!r} within v3"
            )
        object.__setattr__(
            self,
            "maximum_structural_breadth_exclusion_fraction",
            structural_limit,
        )
        object.__setattr__(
            self,
            "configuration_sha256",
            canonical_json_hash(self.to_payload()),
        )

    @property
    def required_history_sessions(self) -> int:
        return max(
            self.long_momentum_sessions + self.momentum_skip_sessions + 1,
            self.trend_sessions + 1,
            self.liquidity_sessions,
            self.risk_sessions + 1,
            self.cost_sessions,
            self.volatility_sessions + 1,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": "equity-market-factor-policy-v3",
            "calculation_version": self.calculation_version,
            "momentum_factor_version": MOMENTUM_FACTOR_VERSION,
            "risk_factor_version": RISK_FACTOR_VERSION,
            "liquidity_factor_version": LIQUIDITY_FACTOR_VERSION,
            "cost_factor_version": COST_FACTOR_VERSION,
            "regime_factor_version": REGIME_FACTOR_VERSION,
            "short_momentum_sessions": self.short_momentum_sessions,
            "long_momentum_sessions": self.long_momentum_sessions,
            "momentum_skip_sessions": self.momentum_skip_sessions,
            "trend_sessions": self.trend_sessions,
            "liquidity_sessions": self.liquidity_sessions,
            "risk_sessions": self.risk_sessions,
            "cost_sessions": self.cost_sessions,
            "volatility_sessions": self.volatility_sessions,
            "annualization_sessions": self.annualization_sessions,
            "maximum_structural_breadth_exclusion_fraction": (
                self.maximum_structural_breadth_exclusion_fraction
            ),
            "round_trip_commission_bps": self.round_trip_commission_bps,
            "cost_context_sha256": self.cost_context_sha256,
            "required_adjustment_policy": self.required_adjustment_policy,
        }


@dataclass(frozen=True, slots=True)
class PointInTimeEquitySecurity:
    """Effective share-class, issuer, taxonomy, and tradability evidence."""

    instrument_id: int
    security_id: str
    issuer_id: str
    symbol: str
    sector: str
    industry: str
    quote_currency: str
    tradable: bool
    observation_id: str
    observation_sha256: str

    def __post_init__(self) -> None:
        _positive_int(self.instrument_id, field_name="instrument_id")
        _text(self.security_id, field_name="security_id")
        _text(self.issuer_id, field_name="issuer_id")
        _upper(self.symbol, field_name="symbol")
        _text(self.sector, field_name="sector")
        _text(self.industry, field_name="industry")
        _upper(self.quote_currency, field_name="quote_currency")
        if not isinstance(self.tradable, bool):
            _invalid("tradable must be boolean")
        _digest(self.observation_id, field_name="security observation_id")
        _digest(self.observation_sha256, field_name="security observation_sha256")

    @property
    def peer_groups(self) -> tuple[str, str]:
        return (f"industry:{self.industry}", f"sector:{self.sector}")


@dataclass(frozen=True, slots=True)
class DailyEquityMarketObservation:
    """One immutable daily bar plus explicit cost/action attestations.

    ``total_return_close`` is dividend/split aware for momentum and volatility.
    ``split_adjusted_*`` avoids split artefacts in overnight-gap calculations.
    The source's integer split-adjusted volume and exact split-price factor are
    retained without claiming exact raw-share reconstruction. Liquidity and
    impact use a conservative notional in this pinned provider coordinate.
    """

    instrument_id: int
    symbol: str
    session_date: date
    observed_at: datetime
    available_at: datetime
    observation_id: str
    observation_sha256: str
    provider: str
    timeframe: str
    entitlement_scope: str
    entitlement_owner_user_id: str | None
    total_return_close: float
    split_adjusted_open: float
    split_adjusted_close: float
    split_adjusted_volume: float
    split_adjustment_factor: float
    raw_close: float
    round_trip_spread_bps: float | None
    one_way_nonspread_cost_bps: float | None
    cost_context_sha256: str | None
    corporate_action_clear: bool

    def __post_init__(self) -> None:
        _positive_int(self.instrument_id, field_name="price instrument_id")
        _upper(self.symbol, field_name="price symbol")
        if not isinstance(self.session_date, date):
            _invalid("session_date must be a date")
        observed = _utc(self.observed_at, field_name="price observed_at")
        available = _utc(self.available_at, field_name="price available_at")
        if available < observed:
            _invalid("price available_at cannot precede observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)
        _digest(self.observation_id, field_name="price observation_id")
        _digest(self.observation_sha256, field_name="price observation_sha256")
        provider = _text(self.provider, field_name="price provider")
        if provider != provider.lower():
            _invalid("price provider must be a canonical lowercase identifier")
        if _text(self.timeframe, field_name="price timeframe") != "1d":
            _invalid("daily equity market observations require timeframe '1d'")
        _text(self.entitlement_scope, field_name="price entitlement_scope")
        if self.entitlement_owner_user_id is not None:
            _text(
                self.entitlement_owner_user_id,
                field_name="price entitlement_owner_user_id",
            )
        for field_name in (
            "total_return_close",
            "split_adjusted_open",
            "split_adjusted_close",
            "raw_close",
        ):
            object.__setattr__(
                self,
                field_name,
                _finite(
                    getattr(self, field_name),
                    field_name=field_name,
                    minimum=0.0,
                    strict=True,
                ),
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
        object.__setattr__(
            self,
            "split_adjustment_factor",
            _finite(
                self.split_adjustment_factor,
                field_name="split_adjustment_factor",
                minimum=0.0,
                strict=True,
            ),
        )
        validate_split_price_contract(
            raw_close=self.raw_close,
            split_adjusted_close=self.split_adjusted_close,
            split_adjustment_factor=self.split_adjustment_factor,
        )
        spread = _optional_nonnegative(
            self.round_trip_spread_bps,
            field_name="round_trip_spread_bps",
        )
        impact = _optional_nonnegative(
            self.one_way_nonspread_cost_bps,
            field_name="one_way_nonspread_cost_bps",
        )
        object.__setattr__(self, "round_trip_spread_bps", spread)
        object.__setattr__(self, "one_way_nonspread_cost_bps", impact)
        if (spread is None) != (impact is None):
            _invalid("spread and non-spread evidence must be present or missing together")
        if spread is None:
            if self.cost_context_sha256 is not None:
                _invalid("missing costs cannot carry a cost context")
        else:
            _digest(self.cost_context_sha256, field_name="cost_context_sha256")
        if not isinstance(self.corporate_action_clear, bool):
            _invalid("corporate_action_clear must be boolean")


@dataclass(frozen=True, slots=True)
class StructuralBreadthExclusion:
    """Bounded listing warm-up with complete post-listing market evidence.

    This is not a generic missing-data waiver. ``missing_session_dates`` must
    be exactly the official sessions preceding the first listing session, and
    every official session from listing through the panel cutoff must be bound
    to immutable source-observation identities.
    """

    security: PointInTimeEquitySecurity
    reason_code: str
    listing_date: date
    listing_session: date
    observed_history_sessions: int
    required_history_sessions: int
    missing_session_dates: tuple[date, ...]
    observed_session_dates: tuple[date, ...]
    source_observation_ids: tuple[str, ...]
    source_observation_sha256s: tuple[str, ...]
    membership_interval_id: str
    evidence_id: str
    evidence_provider: str
    evidence_provider_symbol: str
    evidence_artifact_role: str
    evidence_source_ref: str
    evidence_retrieved_at: datetime
    evidence_sha256: str
    identity_binding: str

    def __post_init__(self) -> None:
        if not isinstance(self.security, PointInTimeEquitySecurity):
            _invalid("structural breadth exclusion requires security evidence")
        if self.reason_code != STRUCTURAL_BREADTH_EXCLUSION_REASON:
            _invalid(
                "structural breadth exclusion reason must be "
                f"{STRUCTURAL_BREADTH_EXCLUSION_REASON!r}"
            )
        if not isinstance(self.listing_date, date) or not isinstance(self.listing_session, date):
            _invalid("listing date and session must be dates")
        if self.listing_session < self.listing_date:
            _invalid("listing session cannot precede the vendor-reported listing date")
        observed = _positive_int(
            self.observed_history_sessions,
            field_name="observed_history_sessions",
        )
        required = _positive_int(
            self.required_history_sessions,
            field_name="required_history_sessions",
        )
        if observed >= required:
            _invalid("a complete history cannot be a structural breadth exclusion")
        for field_name, values in (
            ("missing_session_dates", self.missing_session_dates),
            ("observed_session_dates", self.observed_session_dates),
        ):
            if (
                not isinstance(values, tuple)
                or not values
                or any(not isinstance(value, date) for value in values)
                or values != tuple(sorted(values))
                or len(values) != len(set(values))
            ):
                _invalid(f"{field_name} must be non-empty, unique, and canonical")
        if len(self.observed_session_dates) != observed:
            _invalid("observed session count differs from its session evidence")
        if len(self.missing_session_dates) + observed != required:
            _invalid("structural breadth history does not cover the required window")
        if any(value >= self.listing_session for value in self.missing_session_dates):
            _invalid("structural breadth exclusion contains a post-listing gap")
        if self.observed_session_dates[0] != self.listing_session:
            _invalid("structural breadth evidence does not start on the listing session")
        if (
            len(self.source_observation_ids) != observed
            or len(self.source_observation_sha256s) != observed
            or len(set(self.source_observation_ids)) != observed
        ):
            _invalid("structural breadth observation lineage is incomplete")
        for observation_id in self.source_observation_ids:
            _digest(observation_id, field_name="structural source observation_id")
        for observation_sha256 in self.source_observation_sha256s:
            _digest(
                observation_sha256,
                field_name="structural source observation_sha256",
            )
        _digest(self.membership_interval_id, field_name="membership_interval_id")
        _digest(self.evidence_id, field_name="listing evidence_id")
        _text(self.evidence_provider, field_name="listing evidence_provider")
        _text(
            self.evidence_provider_symbol,
            field_name="listing evidence_provider_symbol",
        )
        _text(self.evidence_artifact_role, field_name="listing evidence_artifact_role")
        _text(self.evidence_source_ref, field_name="listing evidence_source_ref")
        object.__setattr__(
            self,
            "evidence_retrieved_at",
            _utc(
                self.evidence_retrieved_at,
                field_name="listing evidence_retrieved_at",
            ),
        )
        _digest(self.evidence_sha256, field_name="listing evidence_sha256")
        _text(self.identity_binding, field_name="listing identity_binding")


@dataclass(frozen=True, slots=True)
class EquityMarketFactorInput:
    """Complete candidate cross-section at one actionable cutoff."""

    effective_session: date
    cutoff: datetime
    official_sessions: tuple[OfficialSessionCutoff, ...]
    members: tuple[PointInTimeEquitySecurity, ...]
    benchmark: PointInTimeEquitySecurity
    prices: tuple[DailyEquityMarketObservation, ...]
    structural_breadth_exclusions: tuple[StructuralBreadthExclusion, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.effective_session, date):
            _invalid("effective_session must be a date")
        object.__setattr__(self, "cutoff", _utc(self.cutoff, field_name="cutoff"))
        for field_name, values, expected_type in (
            ("official_sessions", self.official_sessions, OfficialSessionCutoff),
            ("members", self.members, PointInTimeEquitySecurity),
            ("prices", self.prices, DailyEquityMarketObservation),
        ):
            if not isinstance(values, tuple) or not values:
                _invalid(f"{field_name} must be a non-empty immutable tuple")
            if any(not isinstance(value, expected_type) for value in values):
                _invalid(f"{field_name} contains an incompatible value")
        if not isinstance(self.benchmark, PointInTimeEquitySecurity):
            _invalid("benchmark must be point-in-time security evidence")
        if not isinstance(self.structural_breadth_exclusions, tuple) or any(
            not isinstance(value, StructuralBreadthExclusion)
            for value in self.structural_breadth_exclusions
        ):
            _invalid("structural_breadth_exclusions must be an immutable typed tuple")


@dataclass(frozen=True, slots=True)
class InstrumentMarketFactorCalculation:
    """Exact market sleeve and hard-gate inputs for one member."""

    security: PointInTimeEquitySecurity
    reference_price: float
    momentum_6_1: float
    momentum_12_1: float
    price_momentum: float
    absolute_momentum: float
    trend_return: float
    median_dollar_volume: float
    expected_round_trip_cost_bps: float | None
    worst_gap_return: float
    downside_volatility: float
    corporate_action_clear: bool
    data_quality_passed: bool
    momentum_end_session: date
    latest_observation_id: str
    source_observation_ids: tuple[str, ...]
    source_observation_sha256s: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.security, PointInTimeEquitySecurity):
            _invalid("instrument calculation requires security evidence")
        for field_name in (
            "reference_price",
            "momentum_6_1",
            "momentum_12_1",
            "price_momentum",
            "absolute_momentum",
            "trend_return",
            "median_dollar_volume",
            "worst_gap_return",
            "downside_volatility",
        ):
            minimum = (
                0.0
                if field_name
                in {
                    "reference_price",
                    "median_dollar_volume",
                    "downside_volatility",
                }
                else None
            )
            strict = field_name == "reference_price"
            _finite(
                getattr(self, field_name),
                field_name=field_name,
                minimum=minimum,
                strict=strict,
            )
        if self.expected_round_trip_cost_bps is not None:
            _finite(
                self.expected_round_trip_cost_bps,
                field_name="expected_round_trip_cost_bps",
                minimum=0.0,
            )
        if not isinstance(self.corporate_action_clear, bool) or not isinstance(
            self.data_quality_passed, bool
        ):
            _invalid("calculation quality and action gates must be boolean")
        _digest(self.latest_observation_id, field_name="latest_observation_id")
        if (
            not self.source_observation_ids
            or len(self.source_observation_ids) != len(self.source_observation_sha256s)
            or len(set(self.source_observation_ids)) != len(self.source_observation_ids)
        ):
            _invalid("source observation identities must be complete and unique")


@dataclass(frozen=True, slots=True)
class MarketRegimeCalculation:
    """Benchmark trend/volatility and full-member breadth."""

    benchmark_trend_return: float
    benchmark_trend_score: float
    breadth_score: float
    breadth_upper_bound: float
    breadth_uncertainty: float
    breadth_coverage_ratio: float
    breadth_positive_members: int
    breadth_observed_members: int
    breadth_structural_excluded_members: int
    breadth_total_members: int
    realized_volatility: float
    structural_breadth_exclusions: tuple[StructuralBreadthExclusion, ...]
    source_observation_ids: tuple[str, ...]
    source_observation_sha256s: tuple[str, ...]

    def __post_init__(self) -> None:
        _finite(self.benchmark_trend_return, field_name="benchmark_trend_return")
        for field_name in (
            "benchmark_trend_score",
            "breadth_score",
            "breadth_upper_bound",
            "breadth_uncertainty",
            "breadth_coverage_ratio",
        ):
            value = _finite(getattr(self, field_name), field_name=field_name)
            if not 0.0 <= value <= 1.0:
                _invalid(f"{field_name} must be in [0, 1]")
        if not 0 <= self.breadth_positive_members <= self.breadth_observed_members:
            _invalid("breadth counts are inconsistent")
        if (
            self.breadth_observed_members < 1
            or self.breadth_structural_excluded_members < 0
            or self.breadth_total_members
            != self.breadth_observed_members + self.breadth_structural_excluded_members
        ):
            _invalid("breadth population counts are inconsistent")
        if self.breadth_total_members < 1:
            _invalid("breadth requires at least one member")
        exclusions = self.structural_breadth_exclusions
        if (
            not isinstance(exclusions, tuple)
            or len(exclusions) != self.breadth_structural_excluded_members
            or exclusions != tuple(sorted(exclusions, key=lambda item: item.security.security_id))
            or len({item.security.security_id for item in exclusions}) != len(exclusions)
        ):
            _invalid("structural breadth exclusions must be complete and canonical")
        expected_lower = self.breadth_positive_members / self.breadth_total_members
        expected_upper = (
            self.breadth_positive_members + self.breadth_structural_excluded_members
        ) / self.breadth_total_members
        expected_uncertainty = self.breadth_structural_excluded_members / self.breadth_total_members
        expected_coverage = self.breadth_observed_members / self.breadth_total_members
        for field_name, actual, expected in (
            ("breadth_score", self.breadth_score, expected_lower),
            ("breadth_upper_bound", self.breadth_upper_bound, expected_upper),
            ("breadth_uncertainty", self.breadth_uncertainty, expected_uncertainty),
            ("breadth_coverage_ratio", self.breadth_coverage_ratio, expected_coverage),
        ):
            if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-15):
                _invalid(f"{field_name} differs from its population counts")
        _finite(self.realized_volatility, field_name="realized_volatility", minimum=0.0)
        if not self.source_observation_ids or len(self.source_observation_ids) != len(
            self.source_observation_sha256s
        ):
            _invalid("benchmark observation lineage is incomplete")


@dataclass(frozen=True, slots=True)
class EquityMarketFactorSnapshot:
    """Content-addressed complete market calculation."""

    policy_sha256: str
    effective_session: date
    cutoff: datetime
    session_sha256s: tuple[str, ...]
    instruments: tuple[InstrumentMarketFactorCalculation, ...]
    regime: MarketRegimeCalculation
    content_sha256: str

    def __post_init__(self) -> None:
        _digest(self.policy_sha256, field_name="policy_sha256")
        object.__setattr__(self, "cutoff", _utc(self.cutoff, field_name="snapshot cutoff"))
        if not self.session_sha256s or any(
            _digest(value, field_name="session_sha256") != value for value in self.session_sha256s
        ):
            _invalid("session evidence is incomplete")
        symbols = tuple(item.security.symbol for item in self.instruments)
        if symbols != tuple(sorted(symbols)) or len(symbols) != len(set(symbols)):
            _invalid("instrument calculations must be canonical and unique")
        _digest(self.content_sha256, field_name="market snapshot content_sha256")
        if self.content_sha256 != _snapshot_sha256(
            policy_sha256=self.policy_sha256,
            effective_session=self.effective_session,
            cutoff=self.cutoff,
            session_sha256s=self.session_sha256s,
            instruments=self.instruments,
            regime=self.regime,
        ):
            _invalid("market snapshot content hash differs from its calculations")


def calculate_equity_market_factors(
    panel: EquityMarketFactorInput,
    policy: EquityMarketFactorPolicy,
) -> EquityMarketFactorSnapshot:
    """Calculate one complete panel or fail closed on any required evidence."""

    if not isinstance(panel, EquityMarketFactorInput):
        message = "panel must be an EquityMarketFactorInput"
        raise TypeError(message)
    if not isinstance(policy, EquityMarketFactorPolicy):
        message = "policy must be an EquityMarketFactorPolicy"
        raise TypeError(message)
    sessions = _validated_sessions(panel, policy)
    securities = _validated_securities(panel)
    structural_exclusions = _validated_structural_exclusions(panel, sessions, policy)
    histories = _validated_histories(panel, securities, sessions, policy)
    momentum_end_session = sessions[-policy.momentum_skip_sessions - 1].session_date
    instruments = tuple(
        _calculate_instrument(
            security,
            histories[security.instrument_id],
            policy,
            momentum_end_session=momentum_end_session,
        )
        for security in sorted(panel.members, key=lambda item: item.symbol)
    )
    regime = _calculate_regime(
        histories[panel.benchmark.instrument_id],
        instruments,
        structural_exclusions,
        policy,
    )
    session_hashes = tuple(session.content_sha256 for session in sessions)
    content_sha256 = _snapshot_sha256(
        policy_sha256=policy.configuration_sha256,
        effective_session=panel.effective_session,
        cutoff=panel.cutoff,
        session_sha256s=session_hashes,
        instruments=instruments,
        regime=regime,
    )
    return EquityMarketFactorSnapshot(
        policy_sha256=policy.configuration_sha256,
        effective_session=panel.effective_session,
        cutoff=panel.cutoff,
        session_sha256s=session_hashes,
        instruments=instruments,
        regime=regime,
        content_sha256=content_sha256,
    )


def _validated_sessions(
    panel: EquityMarketFactorInput,
    policy: EquityMarketFactorPolicy,
) -> tuple[OfficialSessionCutoff, ...]:
    sessions = tuple(sorted(panel.official_sessions, key=lambda item: item.session_date))
    dates = tuple(item.session_date for item in sessions)
    if len(dates) != len(set(dates)):
        _invalid("official-session history contains duplicate dates")
    if sessions[-1].session_date != panel.effective_session:
        _invalid("official-session history does not end at the effective session")
    if sessions[-1].closes_at > panel.cutoff:
        _invalid("cutoff precedes the effective official-session close")
    if len(sessions) < policy.required_history_sessions:
        _invalid(
            "official-session lookback is incomplete: "
            f"{len(sessions)}/{policy.required_history_sessions}"
        )
    required = sessions[-policy.required_history_sessions :]
    if len({item.mic for item in required}) != 1:
        _invalid("official-session history must use one venue calendar")
    if any(left.closes_at >= right.closes_at for left, right in pairwise(required)):
        _invalid("official-session closes must strictly increase")
    return required


def _validated_securities(
    panel: EquityMarketFactorInput,
) -> dict[int, PointInTimeEquitySecurity]:
    structural_securities = tuple(item.security for item in panel.structural_breadth_exclusions)
    complete_securities = (*panel.members, panel.benchmark)
    all_securities = (*complete_securities, *structural_securities)
    ids = tuple(item.instrument_id for item in all_securities)
    symbols = tuple(item.symbol for item in all_securities)
    security_ids = tuple(item.security_id for item in all_securities)
    if any(len(values) != len(set(values)) for values in (ids, symbols, security_ids)):
        _invalid("member, structural-exclusion, and benchmark identities must be unique")
    if any(item.quote_currency != "USD" for item in all_securities):
        _invalid("v2 US-equity market factors require explicit USD quote currency")
    return {item.instrument_id: item for item in complete_securities}


def _validated_structural_exclusions(
    panel: EquityMarketFactorInput,
    sessions: tuple[OfficialSessionCutoff, ...],
    policy: EquityMarketFactorPolicy,
) -> tuple[StructuralBreadthExclusion, ...]:
    exclusions = tuple(
        sorted(
            panel.structural_breadth_exclusions,
            key=lambda item: item.security.security_id,
        )
    )
    required_dates = tuple(item.session_date for item in sessions)
    for exclusion in exclusions:
        if exclusion.required_history_sessions != policy.required_history_sessions:
            _invalid("structural exclusion uses a different required-history policy")
        if exclusion.listing_date > panel.effective_session:
            _invalid("structural exclusion listing date is after the effective session")
        first_listing_session = next(
            (value for value in required_dates if value >= exclusion.listing_date),
            None,
        )
        if first_listing_session is None or exclusion.listing_session != first_listing_session:
            _invalid("structural exclusion does not use the first official listing session")
        listing_index = required_dates.index(exclusion.listing_session)
        expected_missing = required_dates[:listing_index]
        expected_observed = required_dates[listing_index:]
        if exclusion.missing_session_dates != expected_missing:
            _invalid("structural exclusion does not bind every pre-listing official session")
        if exclusion.observed_session_dates != expected_observed:
            _invalid("structural exclusion has an unexplained post-listing session gap")
    total_members = len(panel.members) + len(exclusions)
    structural_fraction = len(exclusions) / total_members
    if structural_fraction > policy.maximum_structural_breadth_exclusion_fraction:
        _invalid(
            "structural breadth exclusion fraction exceeds the frozen policy: "
            f"{len(exclusions)}/{total_members}"
        )
    return exclusions


def _validated_histories(
    panel: EquityMarketFactorInput,
    securities: dict[int, PointInTimeEquitySecurity],
    sessions: tuple[OfficialSessionCutoff, ...],
    policy: EquityMarketFactorPolicy,
) -> dict[int, tuple[DailyEquityMarketObservation, ...]]:
    session_by_date = {item.session_date: item for item in sessions}
    by_instrument: dict[int, dict[date, DailyEquityMarketObservation]] = {
        instrument_id: {} for instrument_id in securities
    }
    observation_ids: set[str] = set()
    for observation in panel.prices:
        security = securities.get(observation.instrument_id)
        if security is None or observation.symbol != security.symbol:
            _invalid("price observation security identity is inconsistent")
        session = session_by_date.get(observation.session_date)
        if session is None:
            _invalid("price observation is outside the required official sessions")
        if observation.observed_at != session.closes_at:
            _invalid("price observation is not stamped at the official close")
        if observation.available_at > panel.cutoff:
            _invalid("price observation is unavailable at the decision cutoff")
        if observation.observation_id in observation_ids:
            _invalid("price observation identities must be globally unique")
        observation_ids.add(observation.observation_id)
        history = by_instrument[observation.instrument_id]
        if observation.session_date in history:
            _invalid("multiple price observations cover one instrument/session")
        history[observation.session_date] = observation
    required_dates = tuple(item.session_date for item in sessions)
    result: dict[int, tuple[DailyEquityMarketObservation, ...]] = {}
    for instrument_id, security in securities.items():
        history = by_instrument[instrument_id]
        missing = tuple(item for item in required_dates if item not in history)
        if missing:
            _invalid(
                f"price history for {security.symbol} is missing official sessions: "
                f"{tuple(item.isoformat() for item in missing)}"
            )
        ordered = tuple(history[item] for item in required_dates)
        for observation in ordered[-policy.cost_sessions :]:
            if (
                observation.cost_context_sha256 is not None
                and observation.cost_context_sha256 != policy.cost_context_sha256
            ):
                _invalid("price cost evidence uses a different reference-order policy")
        result[instrument_id] = ordered
    return result


def _calculate_instrument(
    security: PointInTimeEquitySecurity,
    history: tuple[DailyEquityMarketObservation, ...],
    policy: EquityMarketFactorPolicy,
    *,
    momentum_end_session: date,
) -> InstrumentMarketFactorCalculation:
    end_index = len(history) - policy.momentum_skip_sessions - 1
    momentum_end = history[end_index].total_return_close
    short_start = history[end_index - policy.short_momentum_sessions].total_return_close
    long_start = history[end_index - policy.long_momentum_sessions].total_return_close
    momentum_6_1 = momentum_end / short_start - 1.0
    momentum_12_1 = momentum_end / long_start - 1.0
    price_momentum = math.fsum((momentum_6_1, momentum_12_1)) / 2.0
    trend_return = (
        history[-1].total_return_close / history[-policy.trend_sessions - 1].total_return_close
        - 1.0
    )
    liquidity_history = history[-policy.liquidity_sessions :]
    median_dollar_volume = float(
        statistics.median(
            conservative_split_coordinate_notional(
                item.split_adjusted_close,
                item.split_adjusted_volume,
            )
            for item in liquidity_history
        )
    )
    cost_history = history[-policy.cost_sessions :]
    expected_cost: float | None = None
    if all(item.round_trip_spread_bps is not None for item in cost_history):
        costs = tuple(
            float(item.round_trip_spread_bps) + 2.0 * float(item.one_way_nonspread_cost_bps)
            for item in cost_history
            if item.round_trip_spread_bps is not None
            and item.one_way_nonspread_cost_bps is not None
        )
        if len(costs) == policy.cost_sessions:
            expected_cost = float(statistics.median(costs)) + policy.round_trip_commission_bps
    risk_history = history[-policy.risk_sessions - 1 :]
    gap_returns = tuple(
        current.split_adjusted_open / previous.split_adjusted_close - 1.0
        for previous, current in pairwise(risk_history)
    )
    log_returns = tuple(
        math.log(current.total_return_close / previous.total_return_close)
        for previous, current in pairwise(risk_history)
    )
    downside_volatility = math.sqrt(
        policy.annualization_sessions
        * math.fsum(min(value, 0.0) ** 2 for value in log_returns)
        / len(log_returns)
    )
    return InstrumentMarketFactorCalculation(
        security=security,
        reference_price=history[-1].raw_close,
        momentum_6_1=momentum_6_1,
        momentum_12_1=momentum_12_1,
        price_momentum=price_momentum,
        absolute_momentum=price_momentum,
        trend_return=trend_return,
        median_dollar_volume=median_dollar_volume,
        expected_round_trip_cost_bps=expected_cost,
        worst_gap_return=min(gap_returns),
        downside_volatility=downside_volatility,
        corporate_action_clear=all(item.corporate_action_clear for item in risk_history),
        data_quality_passed=True,
        momentum_end_session=momentum_end_session,
        latest_observation_id=history[-1].observation_id,
        source_observation_ids=tuple(item.observation_id for item in history),
        source_observation_sha256s=tuple(item.observation_sha256 for item in history),
    )


def _calculate_regime(
    benchmark_history: tuple[DailyEquityMarketObservation, ...],
    instruments: tuple[InstrumentMarketFactorCalculation, ...],
    structural_exclusions: tuple[StructuralBreadthExclusion, ...],
    policy: EquityMarketFactorPolicy,
) -> MarketRegimeCalculation:
    trend_return = (
        benchmark_history[-1].total_return_close
        / benchmark_history[-policy.trend_sessions - 1].total_return_close
        - 1.0
    )
    positive = sum(item.trend_return > 0.0 for item in instruments)
    observed_members = len(instruments)
    structural_members = len(structural_exclusions)
    total_members = observed_members + structural_members
    volatility_history = benchmark_history[-policy.volatility_sessions - 1 :]
    log_returns = tuple(
        math.log(current.total_return_close / previous.total_return_close)
        for previous, current in pairwise(volatility_history)
    )
    realized_volatility = statistics.stdev(log_returns) * math.sqrt(policy.annualization_sessions)
    return MarketRegimeCalculation(
        benchmark_trend_return=trend_return,
        benchmark_trend_score=1.0 if trend_return > 0.0 else 0.0,
        breadth_score=positive / total_members,
        breadth_upper_bound=(positive + structural_members) / total_members,
        breadth_uncertainty=structural_members / total_members,
        breadth_coverage_ratio=observed_members / total_members,
        breadth_positive_members=positive,
        breadth_observed_members=observed_members,
        breadth_structural_excluded_members=structural_members,
        breadth_total_members=total_members,
        realized_volatility=realized_volatility,
        structural_breadth_exclusions=structural_exclusions,
        source_observation_ids=tuple(item.observation_id for item in benchmark_history),
        source_observation_sha256s=tuple(item.observation_sha256 for item in benchmark_history),
    )


def _instrument_payload(item: InstrumentMarketFactorCalculation) -> dict[str, object]:
    return {
        "instrument_id": item.security.instrument_id,
        "security_id": item.security.security_id,
        "issuer_id": item.security.issuer_id,
        "symbol": item.security.symbol,
        "security_observation_id": item.security.observation_id,
        "security_observation_sha256": item.security.observation_sha256,
        "sector": item.security.sector,
        "industry": item.security.industry,
        "tradable": item.security.tradable,
        "reference_price": _float_token(item.reference_price),
        "momentum_6_1": _float_token(item.momentum_6_1),
        "momentum_12_1": _float_token(item.momentum_12_1),
        "price_momentum": _float_token(item.price_momentum),
        "absolute_momentum": _float_token(item.absolute_momentum),
        "trend_return": _float_token(item.trend_return),
        "median_dollar_volume": _float_token(item.median_dollar_volume),
        "expected_round_trip_cost_bps": _float_token(item.expected_round_trip_cost_bps),
        "worst_gap_return": _float_token(item.worst_gap_return),
        "downside_volatility": _float_token(item.downside_volatility),
        "corporate_action_clear": item.corporate_action_clear,
        "data_quality_passed": item.data_quality_passed,
        "momentum_end_session": item.momentum_end_session.isoformat(),
        "latest_observation_id": item.latest_observation_id,
        "source_observations": list(
            zip(
                item.source_observation_ids,
                item.source_observation_sha256s,
                strict=True,
            )
        ),
    }


def _regime_payload(item: MarketRegimeCalculation) -> dict[str, object]:
    return {
        "benchmark_trend_return": _float_token(item.benchmark_trend_return),
        "benchmark_trend_score": _float_token(item.benchmark_trend_score),
        "breadth_score": _float_token(item.breadth_score),
        "breadth_upper_bound": _float_token(item.breadth_upper_bound),
        "breadth_uncertainty": _float_token(item.breadth_uncertainty),
        "breadth_coverage_ratio": _float_token(item.breadth_coverage_ratio),
        "breadth_positive_members": item.breadth_positive_members,
        "breadth_observed_members": item.breadth_observed_members,
        "breadth_structural_excluded_members": (item.breadth_structural_excluded_members),
        "breadth_total_members": item.breadth_total_members,
        "realized_volatility": _float_token(item.realized_volatility),
        "structural_breadth_exclusions": [
            _structural_exclusion_payload(value) for value in item.structural_breadth_exclusions
        ],
        "source_observations": list(
            zip(
                item.source_observation_ids,
                item.source_observation_sha256s,
                strict=True,
            )
        ),
    }


def _structural_exclusion_payload(
    item: StructuralBreadthExclusion,
) -> dict[str, object]:
    return {
        "security": {
            "instrument_id": item.security.instrument_id,
            "security_id": item.security.security_id,
            "issuer_id": item.security.issuer_id,
            "symbol": item.security.symbol,
            "observation_id": item.security.observation_id,
            "observation_sha256": item.security.observation_sha256,
        },
        "reason_code": item.reason_code,
        "listing_date": item.listing_date.isoformat(),
        "listing_session": item.listing_session.isoformat(),
        "observed_history_sessions": item.observed_history_sessions,
        "required_history_sessions": item.required_history_sessions,
        "missing_session_dates": [value.isoformat() for value in item.missing_session_dates],
        "observed_sessions": [
            {
                "session_date": session_date.isoformat(),
                "observation_id": observation_id,
                "observation_sha256": observation_sha256,
            }
            for session_date, observation_id, observation_sha256 in zip(
                item.observed_session_dates,
                item.source_observation_ids,
                item.source_observation_sha256s,
                strict=True,
            )
        ],
        "membership_interval_id": item.membership_interval_id,
        "listing_evidence": {
            "evidence_id": item.evidence_id,
            "provider": item.evidence_provider,
            "provider_symbol": item.evidence_provider_symbol,
            "artifact_role": item.evidence_artifact_role,
            "source_ref": item.evidence_source_ref,
            "retrieved_at": item.evidence_retrieved_at.astimezone(UTC).isoformat(),
            "sha256": item.evidence_sha256,
            "identity_binding": item.identity_binding,
        },
    }


def _snapshot_sha256(
    *,
    policy_sha256: str,
    effective_session: date,
    cutoff: datetime,
    session_sha256s: tuple[str, ...],
    instruments: tuple[InstrumentMarketFactorCalculation, ...],
    regime: MarketRegimeCalculation,
) -> str:
    return canonical_json_hash(
        {
            "schema": "equity-market-factor-snapshot-v2",
            "policy_sha256": policy_sha256,
            "effective_session": effective_session.isoformat(),
            "cutoff": cutoff.astimezone(UTC).isoformat(),
            "session_sha256s": list(session_sha256s),
            "instruments": [_instrument_payload(item) for item in instruments],
            "regime": _regime_payload(regime),
        }
    )
