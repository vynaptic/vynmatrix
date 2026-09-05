"""Concentrated, low-turnover selection policy for US quality compounders.

The module consumes a cutoff-bound :class:`CrossSectionalSnapshot`. Data
acquisition, factor materialization, account sizing, and execution remain in
their existing layers. Missing enabled factor evidence is therefore handled by
the shared ranker before a security can reach this policy.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from enum import StrEnum
from statistics import median, pstdev
from typing import NoReturn

from lib_common.hashing import canonical_json_hash

from .cross_sectional import CrossSectionalSnapshot, FactorSpec

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_TARGET_HOLDINGS = 25
_MINIMUM_GROUP_SIZE = 2
_MAD_TO_NORMAL_SCALE = 1.482602218505602

QUALITY_COMPOUNDER_CALCULATION_VERSION = "us-quality-compounder-rank-v2"
QUALITY_COMPOUNDER_PANEL_DERIVATION_VERSION = "us-quality-compounder-panel-v2"
QUALITY_COMPOUNDER_PEER_TAXONOMY_VERSION = "point_in_time_sector_industry_v1"
QUALITY_COMPOUNDER_STRATEGY_VERSION = "0.2.0"
QUALITY_COMPOUNDER_UNIVERSE = "SP500"
QUALITY_COMPOUNDER_UNIVERSE_CONTRACT = "point_in_time_sp500_membership"
QUALITY_COMPOUNDER_MINIMUM_PEER_COUNT = 5
QUALITY_COMPOUNDER_WINSORIZE_LIMIT = 3.0
QUALITY_COMPOUNDER_REQUIRED_FACTOR_VERSIONS: tuple[tuple[str, str], ...] = (
    ("fundamental_growth", "sec-fundamental-components-v2"),
    (
        "momentum",
        "intermediate-price-momentum-126d-252d-skip21-current-trend-v2",
    ),
    ("quality", "sec-fundamental-components-v2"),
    ("valuation", "sec-fundamental-components-v2"),
)
QUALITY_COMPOUNDER_FACTOR_SPECS = (
    FactorSpec("fundamental_growth", 0.30),
    FactorSpec("momentum", 0.15),
    FactorSpec("quality", 0.35),
    FactorSpec("valuation", 0.20),
)


def _invalid(message: str) -> NoReturn:
    raise ValueError(message)


def _invalid_type(message: str) -> NoReturn:
    raise TypeError(message)


class MarketCapBucket(StrEnum):
    """Registered phase-one market-cap buckets."""

    SMALL = "small"
    MID = "mid"
    LARGE = "large"


class QualityCompounderGroupLevel(StrEnum):
    """Point-in-time classifications used by the two group gates."""

    SECTOR = "sector"
    INDUSTRY = "industry"


@dataclass(frozen=True, slots=True)
class QualityCompounderEvidencePolicy:
    """Frozen MVP derivations for eligibility, regime, and group evidence."""

    minimum_market_cap_usd: float = 300_000_000.0
    small_cap_upper_usd: float = 2_000_000_000.0
    mid_cap_upper_usd: float = 10_000_000_000.0
    minimum_reference_price_usd: float = 5.0
    minimum_median_dollar_volume_usd: float = 50_000_000.0
    minimum_breadth_score: float = 0.50
    minimum_breadth_coverage_ratio: float = 0.95
    maximum_realized_volatility: float = 0.35
    maximum_downside_volatility: float = 0.60
    minimum_worst_gap_return: float = -0.15
    minimum_sector_members: int = 5
    minimum_industry_members: int = 3
    minimum_group_count: int = 5
    max_fundamental_age_days: int = 800
    max_shares_age_days: int = 120
    relative_strength_weight: float = 0.40
    breadth_weight: float = 0.30
    fundamental_growth_weight: float = 0.30
    winsorize_limit: float = QUALITY_COMPOUNDER_WINSORIZE_LIMIT

    def __post_init__(self) -> None:
        positive_names = (
            "minimum_market_cap_usd",
            "small_cap_upper_usd",
            "mid_cap_upper_usd",
            "minimum_reference_price_usd",
            "minimum_median_dollar_volume_usd",
            "maximum_realized_volatility",
            "maximum_downside_volatility",
            "winsorize_limit",
        )
        for name in positive_names:
            if _finite(getattr(self, name), field_name=name) <= 0.0:
                _invalid(f"{name} must be positive")
        if not (self.minimum_market_cap_usd < self.small_cap_upper_usd < self.mid_cap_upper_usd):
            _invalid("market-cap thresholds must be strictly increasing")
        for name in ("minimum_breadth_score", "minimum_breadth_coverage_ratio"):
            value = _finite(getattr(self, name), field_name=name)
            if not 0.0 <= value <= 1.0:
                _invalid(f"{name} must be in [0, 1]")
        if _finite(self.minimum_worst_gap_return, field_name="minimum_worst_gap_return") >= 0.0:
            _invalid("minimum_worst_gap_return must be negative")
        for name in (
            "minimum_sector_members",
            "minimum_industry_members",
            "minimum_group_count",
            "max_fundamental_age_days",
            "max_shares_age_days",
        ):
            value = getattr(self, name)
            minimum = (
                _MINIMUM_GROUP_SIZE
                if name
                in {
                    "minimum_sector_members",
                    "minimum_industry_members",
                    "minimum_group_count",
                }
                else 1
            )
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                _invalid(f"{name} must be an integer of at least {minimum}")
        group_weight = math.fsum(
            (
                _finite(self.relative_strength_weight, field_name="relative_strength_weight"),
                _finite(self.breadth_weight, field_name="breadth_weight"),
                _finite(
                    self.fundamental_growth_weight,
                    field_name="fundamental_growth_weight",
                ),
            )
        )
        if not math.isclose(group_weight, 1.0, rel_tol=0.0, abs_tol=1e-12):
            _invalid("group-score weights must sum to one")


@dataclass(frozen=True, slots=True)
class QualityCompounderGroupMember:
    """Complete member inputs used to derive sector or industry scores."""

    entity_id: str
    sector: str
    industry: str
    price_momentum: float
    trend_return: float
    fundamental_growth: float

    def __post_init__(self) -> None:
        for name in ("entity_id", "sector", "industry"):
            _text(getattr(self, name), field_name=name)
        for name in ("price_momentum", "trend_return", "fundamental_growth"):
            _finite(getattr(self, name), field_name=name)


@dataclass(frozen=True, slots=True)
class QualityCompounderPolicy:
    """Versioned defaults for ranking and portfolio construction."""

    target_holdings: int = 15
    entry_score: float = 0.50
    entry_percentile: float = 0.10
    hold_score: float = 0.0
    hold_percentile: float = 0.20
    quality_floor: float = 0.25
    growth_floor: float = 0.0
    entry_group_floor: float = 0.0
    hold_group_floor: float = -0.50
    challenger_score_gap: float = 0.35
    max_sector_weight: float = 0.25
    max_industry_weight: float = 0.15
    max_small_cap_weight: float = 0.20
    max_mid_cap_weight: float = 0.40
    small_cap_position_weight: float = 0.05
    max_expected_round_trip_cost_bps: float = 40.0
    factor_specs: tuple[FactorSpec, ...] = field(
        default_factory=lambda: QUALITY_COMPOUNDER_FACTOR_SPECS
    )

    def __post_init__(self) -> None:
        if (
            isinstance(self.target_holdings, bool)
            or not isinstance(self.target_holdings, int)
            or self.target_holdings < 1
            or self.target_holdings > _MAX_TARGET_HOLDINGS
        ):
            _invalid("target_holdings must be an integer in [1, 25]")
        for name in (
            "entry_percentile",
            "hold_percentile",
            "max_sector_weight",
            "max_industry_weight",
            "max_small_cap_weight",
            "max_mid_cap_weight",
            "small_cap_position_weight",
        ):
            value = _finite(getattr(self, name), field_name=name)
            if not 0.0 < value <= 1.0:
                _invalid(f"{name} must be in (0, 1]")
        if self.entry_percentile > self.hold_percentile:
            _invalid("entry_percentile cannot exceed hold_percentile")
        for name in (
            "entry_score",
            "hold_score",
            "quality_floor",
            "growth_floor",
            "entry_group_floor",
            "hold_group_floor",
            "challenger_score_gap",
            "max_expected_round_trip_cost_bps",
        ):
            _finite(getattr(self, name), field_name=name)
        if self.challenger_score_gap < 0.0:
            _invalid("challenger_score_gap cannot be negative")
        if self.max_expected_round_trip_cost_bps <= 0.0:
            _invalid("max_expected_round_trip_cost_bps must be positive")
        if self.hold_group_floor > self.entry_group_floor:
            _invalid("hold_group_floor cannot exceed entry_group_floor")
        if self.factor_specs != QUALITY_COMPOUNDER_FACTOR_SPECS:
            _invalid("factor_specs must match the registered quality-compounder policy")
        if self.position_weight(MarketCapBucket.SMALL) > self.max_small_cap_weight:
            _invalid("small-cap position weight exceeds the aggregate small-cap cap")
        # Constructing a snapshot validates names, enabled state, and the exact
        # enabled-weight sum. Keep the policy-side error immediate and clearer.
        enabled_weight = math.fsum(spec.weight for spec in self.factor_specs if spec.enabled)
        if not math.isclose(enabled_weight, 1.0, rel_tol=0.0, abs_tol=1e-12):
            _invalid("enabled factor weights must sum to one")

    @property
    def slot_weight(self) -> float:
        """Return the unlevered equal slot size."""

        return 1.0 / self.target_holdings

    def position_weight(self, bucket: MarketCapBucket) -> float:
        """Return a target weight without inflating smaller-company risk."""

        if bucket is MarketCapBucket.SMALL:
            return min(self.slot_weight, self.small_cap_position_weight)
        return self.slot_weight


def quality_compounder_configuration_sha256(
    strategy_version: str,
    *,
    selection_policy: QualityCompounderPolicy | None = None,
    evidence_policy: QualityCompounderEvidencePolicy | None = None,
) -> str:
    """Return the complete immutable identity of the registered model inputs."""

    version = _text(strategy_version, field_name="strategy_version")
    if version != QUALITY_COMPOUNDER_STRATEGY_VERSION:
        _invalid(
            "quality-compounder configuration supports only strategy version "
            f"{QUALITY_COMPOUNDER_STRATEGY_VERSION}"
        )
    selection = selection_policy or QualityCompounderPolicy()
    evidence = evidence_policy or QualityCompounderEvidencePolicy()
    selection_payload = {
        item.name: getattr(selection, item.name)
        for item in fields(selection)
        if item.name != "factor_specs"
    }
    selection_payload["factor_specs"] = [item.to_dict() for item in selection.factor_specs]
    return canonical_json_hash(
        {
            "calculation_version": QUALITY_COMPOUNDER_CALCULATION_VERSION,
            "evidence_policy": {
                item.name: getattr(evidence, item.name) for item in fields(evidence)
            },
            "minimum_peer_count": QUALITY_COMPOUNDER_MINIMUM_PEER_COUNT,
            "panel_derivation_version": QUALITY_COMPOUNDER_PANEL_DERIVATION_VERSION,
            "peer_taxonomy_version": QUALITY_COMPOUNDER_PEER_TAXONOMY_VERSION,
            "selection_policy": selection_payload,
            "required_factor_versions": list(QUALITY_COMPOUNDER_REQUIRED_FACTOR_VERSIONS),
            "strategy_version": version,
            "universe_contract": QUALITY_COMPOUNDER_UNIVERSE_CONTRACT,
            "winsorize_limit": QUALITY_COMPOUNDER_WINSORIZE_LIMIT,
        }
    )


def quality_compounder_market_cap_bucket(
    market_cap_usd: float,
    *,
    policy: QualityCompounderEvidencePolicy | None = None,
) -> MarketCapBucket:
    """Map an observed USD market capitalization to the frozen phase-one buckets."""

    resolved = policy or QualityCompounderEvidencePolicy()
    value = _finite(market_cap_usd, field_name="market_cap_usd")
    if value < resolved.minimum_market_cap_usd:
        _invalid("market_cap_usd is below the eligible minimum")
    if value < resolved.small_cap_upper_usd:
        return MarketCapBucket.SMALL
    if value < resolved.mid_cap_upper_usd:
        return MarketCapBucket.MID
    return MarketCapBucket.LARGE


def quality_compounder_market_ineligibility_reason(  # noqa: PLR0911 - stable gate ledger
    *,
    quote_currency: str,
    tradable: bool,
    market_cap_usd: float,
    reference_price: float,
    median_dollar_volume: float,
    expected_round_trip_cost_bps: float | None,
    worst_gap_return: float,
    downside_volatility: float,
    corporate_action_clear: bool,
    data_quality_passed: bool,
    evidence_policy: QualityCompounderEvidencePolicy | None = None,
    selection_policy: QualityCompounderPolicy | None = None,
) -> str | None:
    """Return the first stable hard-gate failure, or ``None`` when eligible."""

    evidence = evidence_policy or QualityCompounderEvidencePolicy()
    selection = selection_policy or QualityCompounderPolicy()
    if _text(quote_currency, field_name="quote_currency") != "USD":
        return "quote_currency_not_usd"
    if not isinstance(tradable, bool):
        _invalid_type("tradable must be boolean")
    if not tradable:
        return "not_tradable"
    if _finite(market_cap_usd, field_name="market_cap_usd") < evidence.minimum_market_cap_usd:
        return "market_cap_below_minimum"
    if _finite(reference_price, field_name="reference_price") < (
        evidence.minimum_reference_price_usd
    ):
        return "reference_price_below_minimum"
    if _finite(median_dollar_volume, field_name="median_dollar_volume") < (
        evidence.minimum_median_dollar_volume_usd
    ):
        return "liquidity_below_minimum"
    if not isinstance(corporate_action_clear, bool) or not isinstance(data_quality_passed, bool):
        _invalid_type("market quality gates must be boolean")
    if not corporate_action_clear:
        return "corporate_action_unresolved"
    if not data_quality_passed:
        return "market_data_quality_failed"
    if _finite(worst_gap_return, field_name="worst_gap_return") < (
        evidence.minimum_worst_gap_return
    ):
        return "worst_gap_limit_breached"
    if _finite(downside_volatility, field_name="downside_volatility") > (
        evidence.maximum_downside_volatility
    ):
        return "downside_volatility_limit_breached"
    if expected_round_trip_cost_bps is None:
        return "transaction_cost_unavailable"
    if (
        _finite(
            expected_round_trip_cost_bps,
            field_name="expected_round_trip_cost_bps",
        )
        < 0.0
    ):
        _invalid("expected_round_trip_cost_bps cannot be negative")
    if expected_round_trip_cost_bps > selection.max_expected_round_trip_cost_bps:
        return "transaction_cost_limit_breached"
    return None


def quality_compounder_entries_allowed(
    *,
    benchmark_trend_score: float,
    breadth_score: float,
    breadth_coverage_ratio: float,
    realized_volatility: float,
    policy: QualityCompounderEvidencePolicy | None = None,
) -> bool:
    """Apply the frozen market-level gate used only for new positions."""

    resolved = policy or QualityCompounderEvidencePolicy()
    trend = _finite(benchmark_trend_score, field_name="benchmark_trend_score")
    breadth = _finite(breadth_score, field_name="breadth_score")
    coverage = _finite(
        breadth_coverage_ratio,
        field_name="breadth_coverage_ratio",
    )
    volatility = _finite(realized_volatility, field_name="realized_volatility")
    if not 0.0 <= trend <= 1.0 or not 0.0 <= breadth <= 1.0 or not 0.0 <= coverage <= 1.0:
        _invalid("market regime scores must be in [0, 1]")
    if volatility < 0.0:
        _invalid("realized_volatility cannot be negative")
    return (
        trend == 1.0
        and breadth >= resolved.minimum_breadth_score
        and coverage >= resolved.minimum_breadth_coverage_ratio
        and volatility <= resolved.maximum_realized_volatility
    )


def calculate_quality_compounder_group_scores(
    members: Sequence[QualityCompounderGroupMember],
    *,
    level: QualityCompounderGroupLevel,
    policy: QualityCompounderEvidencePolicy | None = None,
) -> Mapping[str, float]:
    """Score eligible groups from relative strength, breadth, and filing growth."""

    resolved = policy or QualityCompounderEvidencePolicy()
    if not isinstance(level, QualityCompounderGroupLevel):
        _invalid_type("level must be a QualityCompounderGroupLevel")
    canonical = tuple(sorted(members, key=lambda item: item.entity_id))
    if any(not isinstance(item, QualityCompounderGroupMember) for item in canonical):
        _invalid_type("group members must be QualityCompounderGroupMember values")
    if len({item.entity_id for item in canonical}) != len(canonical):
        _invalid("group member entity identities must be unique")
    grouped: dict[str, list[QualityCompounderGroupMember]] = {}
    for member in canonical:
        group = member.sector if level is QualityCompounderGroupLevel.SECTOR else member.industry
        grouped.setdefault(group, []).append(member)
    minimum_members = (
        resolved.minimum_sector_members
        if level is QualityCompounderGroupLevel.SECTOR
        else resolved.minimum_industry_members
    )
    eligible = {
        group: values for group, values in grouped.items() if len(values) >= minimum_members
    }
    if len(eligible) < resolved.minimum_group_count:
        return {}
    relative_strength = {
        group: median(item.price_momentum for item in values) for group, values in eligible.items()
    }
    breadth = {
        group: math.fsum(item.trend_return > 0.0 for item in values) / len(values)
        for group, values in eligible.items()
    }
    growth = {
        group: median(item.fundamental_growth for item in values)
        for group, values in eligible.items()
    }
    normalized_strength = _robust_group_scores(relative_strength, resolved.winsorize_limit)
    normalized_breadth = _robust_group_scores(breadth, resolved.winsorize_limit)
    normalized_growth = _robust_group_scores(growth, resolved.winsorize_limit)
    return {
        group: (
            resolved.relative_strength_weight * normalized_strength[group]
            + resolved.breadth_weight * normalized_breadth[group]
            + resolved.fundamental_growth_weight * normalized_growth[group]
        )
        for group in sorted(eligible)
    }


def _robust_group_scores(values: Mapping[str, float], winsorize_limit: float) -> dict[str, float]:
    ordered = tuple(_finite(values[key], field_name=f"group score {key}") for key in sorted(values))
    center = median(ordered)
    scale = median(abs(value - center) for value in ordered) * _MAD_TO_NORMAL_SCALE
    if scale == 0.0:
        scale = pstdev(ordered)
    if scale == 0.0:
        return dict.fromkeys(sorted(values), 0.0)
    return {
        key: max(-winsorize_limit, min(winsorize_limit, (values[key] - center) / scale))
        for key in sorted(values)
    }


@dataclass(frozen=True, slots=True)
class QualityCompounderSecurity:
    """Cutoff-bound non-factor evidence needed for construction."""

    entity_id: str
    instrument_id: int
    symbol: str
    factor_snapshot_id: str
    sector: str
    industry: str
    market_cap_bucket: MarketCapBucket | None
    sector_score: float | None
    industry_score: float | None
    reference_price: float
    expected_round_trip_cost_bps: float | None
    market_eligible: bool = True
    market_ineligibility_reason: str | None = None

    def __post_init__(self) -> None:
        for name in ("entity_id", "sector", "industry"):
            _text(getattr(self, name), field_name=name)
        _sha256(self.factor_snapshot_id, field_name="factor_snapshot_id")
        symbol = _text(self.symbol, field_name="symbol")
        if symbol != symbol.upper():
            _invalid("symbol must be canonical uppercase")
        if (
            isinstance(self.instrument_id, bool)
            or not isinstance(self.instrument_id, int)
            or self.instrument_id < 1
        ):
            _invalid("instrument_id must be a positive integer")
        if self.market_cap_bucket is not None and not isinstance(
            self.market_cap_bucket, MarketCapBucket
        ):
            _invalid_type("market_cap_bucket is invalid")
        for name in ("sector_score", "industry_score"):
            value = getattr(self, name)
            if value is not None:
                _finite(value, field_name=name)
        if _finite(self.reference_price, field_name="reference_price") <= 0.0:
            _invalid("reference_price must be positive")
        if self.expected_round_trip_cost_bps is not None and (
            _finite(
                self.expected_round_trip_cost_bps,
                field_name="expected_round_trip_cost_bps",
            )
            < 0.0
        ):
            _invalid("expected_round_trip_cost_bps cannot be negative")
        if not isinstance(self.market_eligible, bool):
            _invalid_type("market_eligible must be boolean")
        if self.market_eligible:
            if self.market_ineligibility_reason is not None:
                _invalid("eligible market evidence cannot carry an ineligibility reason")
            if self.expected_round_trip_cost_bps is None:
                _invalid("eligible market evidence requires an expected transaction cost")
            if self.market_cap_bucket is None:
                _invalid("eligible market evidence requires a market-cap bucket")
            if self.sector_score is None or self.industry_score is None:
                _invalid("eligible market evidence requires sector and industry scores")
        else:
            _text(
                self.market_ineligibility_reason,
                field_name="market_ineligibility_reason",
            )


@dataclass(frozen=True, slots=True)
class QualityCompounderHolding:
    """Minimal durable identity for a model incumbent."""

    entity_id: str
    instrument_id: int
    symbol: str
    factor_snapshot_id: str
    sector: str
    industry: str

    def __post_init__(self) -> None:
        _text(self.entity_id, field_name="entity_id")
        _sha256(self.factor_snapshot_id, field_name="factor_snapshot_id")
        _text(self.sector, field_name="sector")
        _text(self.industry, field_name="industry")
        symbol = _text(self.symbol, field_name="symbol")
        if symbol != symbol.upper():
            _invalid("symbol must be canonical uppercase")
        if (
            isinstance(self.instrument_id, bool)
            or not isinstance(self.instrument_id, int)
            or self.instrument_id < 1
        ):
            _invalid("instrument_id must be a positive integer")


@dataclass(frozen=True, slots=True)
class QualityCompounderPosition:
    """One selected security and its unlevered target weight."""

    security: QualityCompounderSecurity
    rank: float
    composite_score: float
    target_weight: float
    incumbent: bool


@dataclass(frozen=True, slots=True)
class QualityCompounderExit:
    """One incumbent no longer present in the target portfolio."""

    holding: QualityCompounderHolding
    reason: str


@dataclass(frozen=True, slots=True)
class QualityCompounderSelection:
    """Complete construction result; unused capacity intentionally remains cash."""

    positions: tuple[QualityCompounderPosition, ...]
    exits: tuple[QualityCompounderExit, ...]
    exclusion_reasons: tuple[tuple[str, str], ...]
    intentional_cash_slots: int

    @property
    def target_gross_exposure(self) -> float:
        return math.fsum(position.target_weight for position in self.positions)


@dataclass(frozen=True, slots=True)
class _RankedSecurity:
    security: QualityCompounderSecurity
    rank: float
    composite_score: float
    normalized_factors: Mapping[str, float]
    incumbent: bool


def select_quality_compounders(  # noqa: PLR0912, PLR0915 - ordered policy ledger
    *,
    snapshot: CrossSectionalSnapshot,
    securities: Sequence[QualityCompounderSecurity],
    incumbents: Sequence[QualityCompounderHolding] = (),
    entries_allowed: bool,
    policy: QualityCompounderPolicy | None = None,
) -> QualityCompounderSelection:
    """Select one concentrated target portfolio from an immutable rank snapshot."""

    resolved = policy or QualityCompounderPolicy()
    if snapshot.factor_specs != tuple(sorted(resolved.factor_specs, key=lambda item: item.name)):
        _invalid("rank snapshot factor policy does not match quality-compounder policy")
    if not isinstance(entries_allowed, bool):
        _invalid_type("entries_allowed must be boolean")

    security_by_id = _unique_by_entity(securities)
    incumbent_by_id = _unique_holdings(incumbents)
    ranks_by_id = {item.entity_id: item for item in snapshot.ranks}
    snapshot_entity_ids = ranks_by_id | {
        item.entity_id: None for item in snapshot.missing_decisions
    }
    if set(security_by_id) != set(snapshot_entity_ids):
        _invalid("security evidence must exactly cover the rank snapshot")
    for entity_id in set(security_by_id) & set(incumbent_by_id):
        security = security_by_id[entity_id]
        holding = incumbent_by_id[entity_id]
        if security.instrument_id != holding.instrument_id or security.symbol != holding.symbol:
            _invalid("incumbent identity disagrees with current security evidence")
    for entity_id, holding in incumbent_by_id.items():
        if entity_id in security_by_id:
            continue
        if any(
            security.instrument_id == holding.instrument_id or security.symbol == holding.symbol
            for security in security_by_id.values()
        ):
            _invalid("departed incumbent identity conflicts with current security evidence")
    contributions: dict[str, dict[str, float]] = {}
    for contribution in snapshot.contributions:
        contributions.setdefault(contribution.entity_id, {})[contribution.factor_name] = (
            contribution.normalized_value
        )

    ranked: list[_RankedSecurity] = []
    reasons: dict[str, str] = {}
    for entity_id, security in security_by_id.items():
        rank = ranks_by_id.get(entity_id)
        if rank is None:
            reasons[entity_id] = "factor_evidence_incomplete"
            continue
        factor_values = contributions.get(entity_id, {})
        enabled_names = {spec.name for spec in resolved.factor_specs if spec.enabled}
        if set(factor_values) != enabled_names:
            _invalid("ranked security lacks exact enabled-factor contribution coverage")
        ranked.append(
            _RankedSecurity(
                security=security,
                rank=rank.rank,
                composite_score=rank.composite_score,
                normalized_factors=factor_values,
                incumbent=entity_id in incumbent_by_id,
            )
        )

    ranked.sort(key=lambda item: (item.rank, item.security.symbol, item.security.entity_id))
    ranked_count = len(snapshot.ranks)
    entry_rank_limit = max(1, math.ceil(ranked_count * resolved.entry_percentile))
    hold_rank_limit = max(1, math.ceil(ranked_count * resolved.hold_percentile))

    eligible_incumbents: list[_RankedSecurity] = []
    challengers: list[_RankedSecurity] = []
    for item in ranked:
        if item.incumbent:
            reason = _hold_failure(item, rank_limit=hold_rank_limit, policy=resolved)
            if reason is None:
                eligible_incumbents.append(item)
            else:
                reasons[item.security.entity_id] = reason
            continue
        reason = _entry_failure(
            item,
            rank_limit=entry_rank_limit,
            entries_allowed=entries_allowed,
            policy=resolved,
        )
        if reason is None:
            challengers.append(item)
        else:
            reasons[item.security.entity_id] = reason

    selected: list[_RankedSecurity] = []
    for item in sorted(
        eligible_incumbents,
        key=lambda value: (-value.composite_score, value.rank, value.security.symbol),
    ):
        if len(selected) >= resolved.target_holdings:
            reasons[item.security.entity_id] = "portfolio_full"
            continue
        if _fits(selected, item, policy=resolved):
            selected.append(item)
        else:
            reasons[item.security.entity_id] = "concentration_limit"

    for challenger in challengers:
        if len(selected) < resolved.target_holdings:
            if _fits(selected, challenger, policy=resolved):
                selected.append(challenger)
            else:
                reasons[challenger.security.entity_id] = "concentration_limit"
            continue
        replaceable = sorted(
            (item for item in selected if item.incumbent),
            key=lambda item: (item.composite_score, -item.rank, item.security.symbol),
        )
        replaced = False
        for incumbent in replaceable:
            if challenger.composite_score < (
                incumbent.composite_score + resolved.challenger_score_gap
            ):
                break
            remainder = [item for item in selected if item is not incumbent]
            if _fits(remainder, challenger, policy=resolved):
                selected = [*remainder, challenger]
                reasons[incumbent.security.entity_id] = "replaced_by_superior_challenger"
                replaced = True
                break
        if not replaced:
            reasons[challenger.security.entity_id] = "turnover_buffer"

    selected.sort(key=lambda item: (item.rank, item.security.symbol, item.security.entity_id))
    selected_ids = {item.security.entity_id for item in selected}
    positions = tuple(
        QualityCompounderPosition(
            security=item.security,
            rank=item.rank,
            composite_score=item.composite_score,
            target_weight=resolved.position_weight(_required_bucket(item.security)),
            incumbent=item.incumbent,
        )
        for item in selected
    )
    exits = tuple(
        QualityCompounderExit(
            holding=holding,
            reason=reasons.get(entity_id, "current_evidence_unavailable"),
        )
        for entity_id, holding in sorted(incumbent_by_id.items())
        if entity_id not in selected_ids
    )
    return QualityCompounderSelection(
        positions=positions,
        exits=exits,
        exclusion_reasons=tuple(sorted(reasons.items())),
        intentional_cash_slots=resolved.target_holdings - len(positions),
    )


def _entry_failure(  # noqa: PLR0911 - explicit gate order is audit-relevant
    item: _RankedSecurity,
    *,
    rank_limit: int,
    entries_allowed: bool,
    policy: QualityCompounderPolicy,
) -> str | None:
    if not item.security.market_eligible:
        return f"market_ineligible:{item.security.market_ineligibility_reason}"
    if not entries_allowed:
        return "market_entry_gate_closed"
    if item.rank > rank_limit or item.composite_score < policy.entry_score:
        return "entry_rank_or_score"
    factors = item.normalized_factors
    if factors["quality"] < policy.quality_floor:
        return "quality_floor"
    if factors["fundamental_growth"] < policy.growth_floor:
        return "growth_floor"
    if (
        _required_group_score(item.security.sector_score) < policy.entry_group_floor
        or _required_group_score(item.security.industry_score) < policy.entry_group_floor
    ):
        return "group_entry_floor"
    expected_cost = item.security.expected_round_trip_cost_bps
    if expected_cost is None:
        _invalid("market-eligible security lacks expected transaction cost")
    if expected_cost > policy.max_expected_round_trip_cost_bps:
        return "expected_cost_limit"
    return None


def _hold_failure(
    item: _RankedSecurity,
    *,
    rank_limit: int,
    policy: QualityCompounderPolicy,
) -> str | None:
    if not item.security.market_eligible:
        return f"market_ineligible:{item.security.market_ineligibility_reason}"
    if item.rank > rank_limit or item.composite_score < policy.hold_score:
        return "hold_rank_or_score"
    factors = item.normalized_factors
    if factors["quality"] < policy.quality_floor:
        return "quality_deterioration"
    if factors["fundamental_growth"] < policy.growth_floor:
        return "growth_deterioration"
    if (
        _required_group_score(item.security.sector_score) < policy.hold_group_floor
        or _required_group_score(item.security.industry_score) < policy.hold_group_floor
    ):
        return "group_hold_floor"
    return None


def _fits(
    selected: Sequence[_RankedSecurity],
    candidate: _RankedSecurity,
    *,
    policy: QualityCompounderPolicy,
) -> bool:
    candidate_bucket = _required_bucket(candidate.security)
    weight = policy.position_weight(candidate_bucket)
    sector_weight = math.fsum(
        policy.position_weight(_required_bucket(item.security))
        for item in selected
        if item.security.sector == candidate.security.sector
    )
    industry_weight = math.fsum(
        policy.position_weight(_required_bucket(item.security))
        for item in selected
        if item.security.industry == candidate.security.industry
    )
    if sector_weight + weight > policy.max_sector_weight + 1e-12:
        return False
    if industry_weight + weight > policy.max_industry_weight + 1e-12:
        return False
    if candidate_bucket is MarketCapBucket.SMALL:
        bucket_weight = math.fsum(
            policy.position_weight(_required_bucket(item.security))
            for item in selected
            if item.security.market_cap_bucket is MarketCapBucket.SMALL
        )
        if bucket_weight + weight > policy.max_small_cap_weight + 1e-12:
            return False
    if candidate_bucket is MarketCapBucket.MID:
        bucket_weight = math.fsum(
            policy.position_weight(_required_bucket(item.security))
            for item in selected
            if item.security.market_cap_bucket is MarketCapBucket.MID
        )
        if bucket_weight + weight > policy.max_mid_cap_weight + 1e-12:
            return False
    return True


def _unique_by_entity(
    securities: Sequence[QualityCompounderSecurity],
) -> dict[str, QualityCompounderSecurity]:
    result: dict[str, QualityCompounderSecurity] = {}
    instrument_ids: set[int] = set()
    symbols: set[str] = set()
    for item in securities:
        if (
            item.entity_id in result
            or item.instrument_id in instrument_ids
            or item.symbol in symbols
        ):
            _invalid("security identities must be unique")
        result[item.entity_id] = item
        instrument_ids.add(item.instrument_id)
        symbols.add(item.symbol)
    return result


def _required_bucket(security: QualityCompounderSecurity) -> MarketCapBucket:
    bucket = security.market_cap_bucket
    if bucket is None:
        _invalid("market-eligible security lacks a market-cap bucket")
    return bucket


def _required_group_score(value: float | None) -> float:
    if value is None:
        _invalid("market-eligible security lacks a group score")
    return value


def _unique_holdings(
    holdings: Sequence[QualityCompounderHolding],
) -> dict[str, QualityCompounderHolding]:
    result: dict[str, QualityCompounderHolding] = {}
    instrument_ids: set[int] = set()
    symbols: set[str] = set()
    for item in holdings:
        if (
            item.entity_id in result
            or item.instrument_id in instrument_ids
            or item.symbol in symbols
        ):
            _invalid("incumbent identities must be unique")
        result[item.entity_id] = item
        instrument_ids.add(item.instrument_id)
        symbols.add(item.symbol)
    return result


def _text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _invalid(f"{field_name} must be a non-blank canonical string")
    return value


def _sha256(value: object, *, field_name: str) -> str:
    text = _text(value, field_name=field_name)
    if _SHA256_RE.fullmatch(text) is None:
        _invalid(f"{field_name} must be a lowercase SHA-256 digest")
    return text


def _finite(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid_type(f"{field_name} must be a finite number")
    converted = float(value)
    if not math.isfinite(converted):
        _invalid(f"{field_name} must be a finite number")
    return converted


__all__ = [
    "QUALITY_COMPOUNDER_CALCULATION_VERSION",
    "QUALITY_COMPOUNDER_FACTOR_SPECS",
    "QUALITY_COMPOUNDER_MINIMUM_PEER_COUNT",
    "QUALITY_COMPOUNDER_PANEL_DERIVATION_VERSION",
    "QUALITY_COMPOUNDER_PEER_TAXONOMY_VERSION",
    "QUALITY_COMPOUNDER_REQUIRED_FACTOR_VERSIONS",
    "QUALITY_COMPOUNDER_STRATEGY_VERSION",
    "QUALITY_COMPOUNDER_UNIVERSE",
    "QUALITY_COMPOUNDER_UNIVERSE_CONTRACT",
    "QUALITY_COMPOUNDER_WINSORIZE_LIMIT",
    "MarketCapBucket",
    "QualityCompounderEvidencePolicy",
    "QualityCompounderExit",
    "QualityCompounderGroupLevel",
    "QualityCompounderGroupMember",
    "QualityCompounderHolding",
    "QualityCompounderPolicy",
    "QualityCompounderPosition",
    "QualityCompounderSecurity",
    "QualityCompounderSelection",
    "calculate_quality_compounder_group_scores",
    "quality_compounder_configuration_sha256",
    "quality_compounder_entries_allowed",
    "quality_compounder_market_cap_bucket",
    "quality_compounder_market_ineligibility_reason",
    "select_quality_compounders",
]
