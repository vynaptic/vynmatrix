"""Point-in-time SEC fundamental factor calculation for US equities.

The module converts immutable, accession-linked SEC facts into canonical
annual observations and then calculates issuer-type-specific component
cross-sections.  Acquisition and persistence stay outside this calculation
boundary; the strategy consumes only the resulting provider-neutral sleeve
observations.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import NoReturn

from lib_common.hashing import canonical_json_hash
from lib_strategy.cross_sectional import (
    CrossSectionalEntity,
    CrossSectionalRanker,
    CrossSectionalSnapshot,
    FactorObservation,
    FactorSpec,
)

from .sec_edgar import SecAcceptedFact, parse_submissions_acceptance_time

__all__ = [
    "CanonicalFundamentalFact",
    "CanonicalFundamentalMetric",
    "FundamentalCalculationConfig",
    "FundamentalComponentValue",
    "FundamentalInstrumentCalculation",
    "FundamentalPanelSnapshot",
    "FundamentalSleeve",
    "FundamentalSleeveCrossSection",
    "IssuerFundamentalEvidence",
    "IssuerType",
    "MarketCapitalizationEvidence",
    "OutstandingSharesEvidence",
    "calculate_fundamental_panel",
    "canonical_sec_tag_mapping",
    "canonicalize_sec_facts",
    "classify_issuer_type",
    "cutoff_safe_shares_outstanding",
    "sic_peer_group",
]

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ANNUAL_FORMS = frozenset({"10-K", "10-K/A"})
_SHARE_COUNT_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A"})
_ANNUAL_FISCAL_PERIOD = "FY"
_USD_UNIT = "USD"
_SHARES_UNIT = "shares"
_MINIMUM_ANNUAL_DAYS = 300
_MAXIMUM_ANNUAL_DAYS = 400
_MAXIMUM_ANNUAL_DURATION_DIFFERENCE_DAYS = 14
_MINIMUM_PEER_COUNT = 2
_MINIMUM_SIC = 100
_MAXIMUM_SIC = 9999
_BANK_SICS = frozenset({6021, 6022, 6029, 6035, 6036})
_DIVERSIFIED_FINANCIAL_SICS = frozenset(
    {
        6099,
        6111,
        6141,
        6153,
        6159,
        6162,
        6163,
        6172,
        6199,
        6200,
        6211,
        6221,
        6282,
    }
)
_INSURANCE_SICS = frozenset({6311, 6321, 6324, 6331, 6351, 6361, 6399, 6411})
_REIT_SIC = 6798
_CALCULATION_VERSION = "sec-fundamental-components-v2"


def _invalid(message: str) -> NoReturn:
    raise ValueError(message)


def _non_blank(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _invalid(f"{field_name} must be a non-blank canonical string")
    return value


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _positive_decimal(value: Decimal, *, field_name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        _invalid(f"{field_name} must be a positive finite Decimal")
    return value


class CanonicalFundamentalMetric(StrEnum):
    """Registered annual accounting concepts used by the public-data version."""

    ASSETS = "assets"
    EQUITY = "equity"
    NET_INCOME = "net_income"
    OPERATING_CASH_FLOW = "operating_cash_flow"
    OPERATING_INCOME = "operating_income"
    REVENUE = "revenue"
    SHARES_OUTSTANDING = "shares_outstanding"


class IssuerType(StrEnum):
    """Accounting peer type derived from the exact filing's historical SIC."""

    BANK_OR_DIVERSIFIED_FINANCIAL = "bank_or_diversified_financial"
    INSURER = "insurer"
    OPERATING_COMPANY = "operating_company"
    REIT = "reit"


class FundamentalSleeve(StrEnum):
    """Provider-neutral factor names consumed by the strategy panel."""

    FUNDAMENTAL_GROWTH = "fundamental_growth"
    QUALITY = "quality"
    VALUATION = "valuation"


@dataclass(frozen=True, slots=True)
class _TagMapping:
    metric: CanonicalFundamentalMetric
    priority: int


# Ordered aliases are a registered mapping, not a data-dependent fallback.
# Lower priority wins only within the same accepted filing revision.
_SEC_TAG_MAPPINGS: Mapping[tuple[str, str], _TagMapping] = {
    ("us-gaap", "Assets"): _TagMapping(CanonicalFundamentalMetric.ASSETS, 0),
    ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"): _TagMapping(
        CanonicalFundamentalMetric.OPERATING_CASH_FLOW,
        0,
    ),
    ("us-gaap", "NetIncomeLoss"): _TagMapping(CanonicalFundamentalMetric.NET_INCOME, 0),
    ("us-gaap", "ProfitLoss"): _TagMapping(CanonicalFundamentalMetric.NET_INCOME, 1),
    ("us-gaap", "OperatingIncomeLoss"): _TagMapping(
        CanonicalFundamentalMetric.OPERATING_INCOME,
        0,
    ),
    ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"): _TagMapping(
        CanonicalFundamentalMetric.REVENUE,
        0,
    ),
    ("us-gaap", "SalesRevenueNet"): _TagMapping(CanonicalFundamentalMetric.REVENUE, 1),
    ("us-gaap", "Revenues"): _TagMapping(CanonicalFundamentalMetric.REVENUE, 2),
    ("us-gaap", "StockholdersEquity"): _TagMapping(CanonicalFundamentalMetric.EQUITY, 0),
    (
        "us-gaap",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ): _TagMapping(CanonicalFundamentalMetric.EQUITY, 1),
    ("dei", "EntityCommonStockSharesOutstanding"): _TagMapping(
        CanonicalFundamentalMetric.SHARES_OUTSTANDING,
        0,
    ),
    ("us-gaap", "CommonStockSharesOutstanding"): _TagMapping(
        CanonicalFundamentalMetric.SHARES_OUTSTANDING,
        1,
    ),
}


@dataclass(frozen=True, slots=True)
class CanonicalFundamentalFact:
    """One accession-linked annual SEC value after registered tag mapping."""

    symbol: str
    metric: CanonicalFundamentalMetric
    value: Decimal
    period_start: date | None
    period_end: date
    acceptance_time_raw: str
    acceptance_time: datetime
    accession: str
    historical_sic: int | None
    taxonomy: str
    raw_tag: str
    mapping_priority: int
    source_sha256: str
    availability_source_sha256: str
    classification_source_sha256: str | None
    observation_id: str

    def __post_init__(self) -> None:
        _non_blank(self.symbol, field_name="fundamental symbol")
        if not isinstance(self.metric, CanonicalFundamentalMetric):
            _invalid("fundamental metric must be a CanonicalFundamentalMetric")
        if not isinstance(self.value, Decimal) or not self.value.is_finite():
            _invalid("fundamental value must be a finite Decimal")
        if self.period_start is not None and self.period_start >= self.period_end:
            _invalid("fundamental period_start must precede period_end")
        object.__setattr__(
            self,
            "acceptance_time",
            _aware_utc(self.acceptance_time, field_name="fundamental acceptance_time"),
        )
        _non_blank(self.acceptance_time_raw, field_name="fundamental acceptance_time_raw")
        if parse_submissions_acceptance_time(self.acceptance_time_raw) != self.acceptance_time:
            _invalid("fundamental acceptance time differs from its raw submissions value")
        _non_blank(self.accession, field_name="fundamental accession")
        if self.historical_sic is not None and not (
            _MINIMUM_SIC <= self.historical_sic <= _MAXIMUM_SIC
        ):
            _invalid("historical_sic must be a three- or four-digit SIC")
        _non_blank(self.taxonomy, field_name="fundamental taxonomy")
        _non_blank(self.raw_tag, field_name="fundamental raw_tag")
        if (
            isinstance(self.mapping_priority, bool)
            or not isinstance(self.mapping_priority, int)
            or self.mapping_priority < 0
        ):
            _invalid("mapping_priority must be a non-negative integer")
        if _SHA256_PATTERN.fullmatch(self.source_sha256) is None:
            _invalid("fundamental source_sha256 must be a lowercase SHA-256 digest")
        if _SHA256_PATTERN.fullmatch(self.availability_source_sha256) is None:
            _invalid("fundamental availability_source_sha256 must be a lowercase SHA-256 digest")
        if (
            self.classification_source_sha256 is not None
            and _SHA256_PATTERN.fullmatch(self.classification_source_sha256) is None
        ):
            _invalid("fundamental classification_source_sha256 must be a lowercase SHA-256 digest")
        if (self.historical_sic is None) != (self.classification_source_sha256 is None):
            _invalid(
                "fundamental historical SIC and classification source must be present together"
            )
        if _SHA256_PATTERN.fullmatch(self.observation_id) is None:
            _invalid("fundamental observation_id must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class MarketCapitalizationEvidence:
    """Point-in-time market capitalization with explicit availability."""

    symbol: str
    value: Decimal
    observed_at: datetime
    available_at: datetime
    source_observation_id: str

    def __post_init__(self) -> None:
        _non_blank(self.symbol, field_name="market-cap symbol")
        _positive_decimal(self.value, field_name="market-cap value")
        observed = _aware_utc(self.observed_at, field_name="market-cap observed_at")
        available = _aware_utc(self.available_at, field_name="market-cap available_at")
        if available < observed:
            _invalid("market-cap available_at cannot precede observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)
        _non_blank(self.source_observation_id, field_name="market-cap source_observation_id")


@dataclass(frozen=True, slots=True)
class OutstandingSharesEvidence:
    """Latest unambiguous SEC share count available at one decision cutoff."""

    symbol: str
    value: Decimal
    period_end: date
    acceptance_time: datetime
    source_observation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _non_blank(self.symbol, field_name="shares symbol")
        _positive_decimal(self.value, field_name="shares value")
        object.__setattr__(
            self,
            "acceptance_time",
            _aware_utc(self.acceptance_time, field_name="shares acceptance_time"),
        )
        if (
            not isinstance(self.source_observation_ids, tuple)
            or not self.source_observation_ids
            or any(_SHA256_PATTERN.fullmatch(item) is None for item in self.source_observation_ids)
            or len(self.source_observation_ids) != len(set(self.source_observation_ids))
        ):
            _invalid("shares source_observation_ids must be unique SHA-256 identities")


@dataclass(frozen=True, slots=True)
class IssuerFundamentalEvidence:
    """Complete provider-neutral evidence offered for one cutoff calculation."""

    symbol: str
    peer_group: str
    cutoff: datetime
    historical_sic: int
    classification_available_at: datetime
    classification_source_id: str
    facts: tuple[CanonicalFundamentalFact, ...]
    market_cap: MarketCapitalizationEvidence | None
    decision_cutoff: datetime | None = None

    def __post_init__(self) -> None:
        _non_blank(self.symbol, field_name="issuer symbol")
        _non_blank(self.peer_group, field_name="issuer peer_group")
        cutoff = _aware_utc(self.cutoff, field_name="issuer cutoff")
        classification_available = _aware_utc(
            self.classification_available_at,
            field_name="classification_available_at",
        )
        decision_cutoff = (
            cutoff
            if self.decision_cutoff is None
            else _aware_utc(self.decision_cutoff, field_name="decision_cutoff")
        )
        if decision_cutoff > cutoff:
            _invalid("fundamental decision cutoff cannot follow the knowledge cutoff")
        if classification_available > cutoff:
            _invalid("historical SIC classification is unavailable at the cutoff")
        if not _MINIMUM_SIC <= self.historical_sic <= _MAXIMUM_SIC:
            _invalid("historical_sic must be a three- or four-digit SIC")
        _non_blank(self.classification_source_id, field_name="classification_source_id")
        if not isinstance(self.facts, tuple):
            _invalid("issuer facts must be an immutable tuple")
        if any(fact.symbol != self.symbol for fact in self.facts):
            _invalid("issuer facts must match the evidence symbol")
        if self.market_cap is not None and self.market_cap.symbol != self.symbol:
            _invalid("market-cap evidence must match the issuer symbol")
        object.__setattr__(self, "cutoff", cutoff)
        object.__setattr__(self, "classification_available_at", classification_available)
        object.__setattr__(self, "decision_cutoff", decision_cutoff)


@dataclass(frozen=True, slots=True)
class FundamentalCalculationConfig:
    """Versioned data-semantic and cross-sectional calculation policy."""

    max_fundamental_age_days: int
    minimum_peer_count: int = 5
    winsorize_limit: float = 3.0
    calculation_version: str = _CALCULATION_VERSION
    minimum_annual_days: int = _MINIMUM_ANNUAL_DAYS
    maximum_annual_days: int = _MAXIMUM_ANNUAL_DAYS
    maximum_annual_duration_difference_days: int = _MAXIMUM_ANNUAL_DURATION_DIFFERENCE_DAYS

    def __post_init__(self) -> None:
        for field_name in (
            "max_fundamental_age_days",
            "minimum_peer_count",
            "minimum_annual_days",
            "maximum_annual_days",
            "maximum_annual_duration_difference_days",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                _invalid(f"{field_name} must be a positive integer")
        if self.minimum_annual_days >= self.maximum_annual_days:
            _invalid("minimum_annual_days must be smaller than maximum_annual_days")
        if self.minimum_peer_count < _MINIMUM_PEER_COUNT:
            _invalid("minimum_peer_count must be at least two")
        if (
            isinstance(self.winsorize_limit, bool)
            or not isinstance(self.winsorize_limit, (int, float))
            or not math.isfinite(self.winsorize_limit)
            or self.winsorize_limit <= 0.0
        ):
            _invalid("winsorize_limit must be a positive finite number")
        _non_blank(self.calculation_version, field_name="calculation_version")

    def build_ranker(self) -> CrossSectionalRanker:
        """Build the deterministic shared ranker from the frozen policy."""

        return CrossSectionalRanker(
            minimum_peer_count=self.minimum_peer_count,
            winsorize_limit=self.winsorize_limit,
            calculation_version=self.calculation_version,
        )


@dataclass(frozen=True, slots=True)
class FundamentalComponentValue:
    """One raw accounting component or its explicit fail-closed reason."""

    name: str
    value: float | None
    source_observation_ids: tuple[str, ...]
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        _non_blank(self.name, field_name="fundamental component name")
        if self.value is None:
            _non_blank(self.missing_reason or "", field_name=f"{self.name} missing_reason")
        else:
            if not math.isfinite(self.value):
                _invalid(f"{self.name} value must be finite")
            if self.missing_reason is not None:
                _invalid(f"{self.name} cannot combine a value and missing_reason")
        if not isinstance(self.source_observation_ids, tuple) or len(
            self.source_observation_ids
        ) != len(set(self.source_observation_ids)):
            _invalid(f"{self.name} source_observation_ids must be a unique tuple")


@dataclass(frozen=True, slots=True)
class FundamentalInstrumentCalculation:
    """Issuer classification, annual periods, and component ledger."""

    symbol: str
    issuer_type: IssuerType
    peer_group: str
    current_period_end: date | None
    prior_period_end: date | None
    components: tuple[FundamentalComponentValue, ...]


@dataclass(frozen=True, slots=True)
class FundamentalSleeveCrossSection:
    """One issuer-type/component cross-section used to form a sleeve."""

    issuer_type: IssuerType
    sleeve: FundamentalSleeve
    snapshot: CrossSectionalSnapshot


@dataclass(frozen=True, slots=True)
class FundamentalPanelSnapshot:
    """Auditable sleeve values for one synchronized point-in-time panel."""

    cutoff: datetime
    configuration: FundamentalCalculationConfig
    calculations: tuple[FundamentalInstrumentCalculation, ...]
    component_cross_sections: tuple[FundamentalSleeveCrossSection, ...]
    sleeve_observations: tuple[FactorObservation, ...]
    content_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cutoff",
            _aware_utc(self.cutoff, field_name="fundamental panel cutoff"),
        )
        if not isinstance(self.configuration, FundamentalCalculationConfig):
            _invalid("fundamental panel configuration is invalid")
        if not self.calculations:
            _invalid("fundamental panel must contain at least one calculation")
        if self.content_digest != _fundamental_panel_digest(
            self.cutoff,
            self.configuration,
            self.calculations,
            self.component_cross_sections,
            self.sleeve_observations,
        ):
            _invalid("fundamental panel content_digest does not match its content")


@dataclass(frozen=True, slots=True)
class _ComponentSpec:
    name: str
    weight: float


_COMPONENTS: Mapping[
    IssuerType,
    Mapping[FundamentalSleeve, tuple[_ComponentSpec, ...]],
] = {
    IssuerType.OPERATING_COMPANY: {
        FundamentalSleeve.QUALITY: (
            _ComponentSpec("operating_profitability", 0.25),
            _ComponentSpec("cash_return_on_assets", 0.25),
            _ComponentSpec("accrual_quality", 0.25),
            _ComponentSpec("balance_sheet_safety", 0.25),
        ),
        FundamentalSleeve.FUNDAMENTAL_GROWTH: (
            _ComponentSpec("revenue_growth", 0.5),
            _ComponentSpec("operating_profit_growth", 0.5),
        ),
        FundamentalSleeve.VALUATION: (
            _ComponentSpec("earnings_yield", 1.0 / 3.0),
            _ComponentSpec("cash_flow_yield", 1.0 / 3.0),
            _ComponentSpec("book_to_market", 1.0 / 3.0),
        ),
    },
    IssuerType.BANK_OR_DIVERSIFIED_FINANCIAL: {
        FundamentalSleeve.QUALITY: (
            _ComponentSpec("return_on_assets", 1.0 / 3.0),
            _ComponentSpec("return_on_equity", 1.0 / 3.0),
            _ComponentSpec("balance_sheet_safety", 1.0 / 3.0),
        ),
        FundamentalSleeve.FUNDAMENTAL_GROWTH: (
            _ComponentSpec("revenue_growth", 0.5),
            _ComponentSpec("net_income_growth", 0.5),
        ),
        FundamentalSleeve.VALUATION: (
            _ComponentSpec("earnings_yield", 0.5),
            _ComponentSpec("book_to_market", 0.5),
        ),
    },
    IssuerType.INSURER: {
        FundamentalSleeve.QUALITY: (
            _ComponentSpec("return_on_assets", 1.0 / 3.0),
            _ComponentSpec("return_on_equity", 1.0 / 3.0),
            _ComponentSpec("balance_sheet_safety", 1.0 / 3.0),
        ),
        FundamentalSleeve.FUNDAMENTAL_GROWTH: (
            _ComponentSpec("revenue_growth", 0.5),
            _ComponentSpec("net_income_growth", 0.5),
        ),
        FundamentalSleeve.VALUATION: (
            _ComponentSpec("earnings_yield", 0.5),
            _ComponentSpec("book_to_market", 0.5),
        ),
    },
    IssuerType.REIT: {
        FundamentalSleeve.QUALITY: (
            _ComponentSpec("operating_profitability", 0.25),
            _ComponentSpec("cash_return_on_assets", 0.25),
            _ComponentSpec("accrual_quality", 0.25),
            _ComponentSpec("balance_sheet_safety", 0.25),
        ),
        FundamentalSleeve.FUNDAMENTAL_GROWTH: (
            _ComponentSpec("revenue_growth", 0.5),
            _ComponentSpec("cash_flow_growth", 0.5),
        ),
        FundamentalSleeve.VALUATION: (
            _ComponentSpec("cash_flow_yield", 1.0 / 3.0),
            _ComponentSpec("operating_income_yield", 1.0 / 3.0),
            _ComponentSpec("book_to_market", 1.0 / 3.0),
        ),
    },
}


def classify_issuer_type(historical_sic: int) -> IssuerType:
    """Classify exact SEC SICs using a fixed, version-controlled mapping."""

    if historical_sic == _REIT_SIC:
        return IssuerType.REIT
    if historical_sic in _INSURANCE_SICS:
        return IssuerType.INSURER
    if historical_sic in _BANK_SICS or historical_sic in _DIVERSIFIED_FINANCIAL_SICS:
        return IssuerType.BANK_OR_DIVERSIFIED_FINANCIAL
    return IssuerType.OPERATING_COMPANY


def sic_peer_group(historical_sic: int) -> str:
    """Return the registered SEC major-group peer identity."""

    if not _MINIMUM_SIC <= historical_sic <= _MAXIMUM_SIC:
        _invalid("historical_sic must be a three- or four-digit SIC")
    return f"sec_sic_major_group:{historical_sic // 100:02d}"


def canonical_sec_tag_mapping(
    taxonomy: str,
    raw_tag: str,
) -> tuple[CanonicalFundamentalMetric, int] | None:
    """Return the registered metric and alias priority for one SEC tag."""

    mapping = _SEC_TAG_MAPPINGS.get((taxonomy, raw_tag))
    if mapping is None:
        return None
    return mapping.metric, mapping.priority


def canonicalize_sec_facts(
    symbol: str,
    facts: Sequence[SecAcceptedFact],
    *,
    cutoff: datetime,
) -> tuple[CanonicalFundamentalFact, ...]:
    """Map accepted annual SEC facts without carrying later revisions backward."""

    canonical_symbol = _non_blank(symbol, field_name="SEC fact symbol")
    cutoff_utc = _aware_utc(cutoff, field_name="SEC fact cutoff")
    canonical: list[CanonicalFundamentalFact] = []
    for accepted in facts:
        fact = accepted.fact
        mapping = _SEC_TAG_MAPPINGS.get((fact.taxonomy, fact.tag))
        share_count = (
            mapping is not None and mapping.metric is CanonicalFundamentalMetric.SHARES_OUTSTANDING
        )
        if (
            mapping is None
            or accepted.acceptance_time > cutoff_utc
            or fact.end > cutoff_utc.date()
            or not isinstance(fact.value, Decimal)
            or not fact.value.is_finite()
        ):
            continue
        if share_count:
            if (
                fact.form not in _SHARE_COUNT_FORMS
                or fact.unit != _SHARES_UNIT
                or fact.start is not None
                or fact.value <= 0
            ):
                continue
        elif (
            fact.form not in _ANNUAL_FORMS
            or fact.fiscal_period != _ANNUAL_FISCAL_PERIOD
            or fact.unit != _USD_UNIT
        ):
            continue
        identity = canonical_json_hash(
            {
                "acceptance_time": accepted.acceptance_time.astimezone(UTC).isoformat(),
                "acceptance_time_raw": accepted.acceptance_time_raw,
                "accession": fact.accession,
                "availability_source_sha256": accepted.filing_source.content_sha256,
                "cik": fact.cik,
                "end": fact.end.isoformat(),
                "filed": fact.filed.isoformat(),
                "fiscal_period": fact.fiscal_period,
                "fiscal_year": fact.fiscal_year,
                "form": fact.form,
                "frame": fact.frame,
                "historical_sic": accepted.historical_sic,
                "classification_source_sha256": (
                    accepted.historical_sic_source.content_sha256
                    if accepted.historical_sic_source is not None
                    else None
                ),
                "mapping_priority": mapping.priority,
                "metric": mapping.metric.value,
                "source_sha256": fact.source.content_sha256,
                "start": fact.start.isoformat() if fact.start is not None else None,
                "symbol": canonical_symbol,
                "tag": fact.tag,
                "taxonomy": fact.taxonomy,
                "unit": fact.unit,
                "value": str(fact.value),
            }
        )
        canonical.append(
            CanonicalFundamentalFact(
                symbol=canonical_symbol,
                metric=mapping.metric,
                value=fact.value,
                period_start=fact.start,
                period_end=fact.end,
                acceptance_time_raw=accepted.acceptance_time_raw,
                acceptance_time=accepted.acceptance_time,
                accession=fact.accession,
                historical_sic=accepted.historical_sic,
                taxonomy=fact.taxonomy,
                raw_tag=fact.tag,
                mapping_priority=mapping.priority,
                source_sha256=fact.source.content_sha256,
                availability_source_sha256=accepted.filing_source.content_sha256,
                classification_source_sha256=(
                    accepted.historical_sic_source.content_sha256
                    if accepted.historical_sic_source is not None
                    else None
                ),
                observation_id=identity,
            )
        )
    return tuple(
        sorted(
            canonical,
            key=lambda item: (
                item.period_end,
                item.metric.value,
                item.acceptance_time,
                item.mapping_priority,
                item.observation_id,
            ),
        )
    )


def cutoff_safe_shares_outstanding(
    facts: Sequence[CanonicalFundamentalFact],
    *,
    cutoff: datetime,
    maximum_age_days: int,
) -> OutstandingSharesEvidence | None:
    """Select one SEC share-count revision without carrying later facts backward."""

    cutoff_utc = _aware_utc(cutoff, field_name="shares cutoff")
    if (
        isinstance(maximum_age_days, bool)
        or not isinstance(maximum_age_days, int)
        or maximum_age_days < 1
    ):
        _invalid("maximum_age_days must be a positive integer")
    share_facts = tuple(
        fact
        for fact in facts
        if fact.metric is CanonicalFundamentalMetric.SHARES_OUTSTANDING
        and fact.acceptance_time <= cutoff_utc
        and fact.period_end <= cutoff_utc.date()
        and 0 <= (cutoff_utc.date() - fact.period_end).days <= maximum_age_days
        and fact.value > 0
    )
    if not share_facts:
        return None
    symbol_set = {fact.symbol for fact in share_facts}
    if len(symbol_set) != 1:
        _invalid("shares facts must contain one issuer symbol")
    latest_period_end = max(fact.period_end for fact in share_facts)
    period_candidates = tuple(fact for fact in share_facts if fact.period_end == latest_period_end)
    latest_acceptance = max(fact.acceptance_time for fact in period_candidates)
    revision = tuple(
        fact for fact in period_candidates if fact.acceptance_time == latest_acceptance
    )
    best_priority = min(fact.mapping_priority for fact in revision)
    selected = tuple(fact for fact in revision if fact.mapping_priority == best_priority)
    values = {fact.value for fact in selected}
    if len(values) != 1:
        return None
    return OutstandingSharesEvidence(
        symbol=next(iter(symbol_set)),
        value=next(iter(values)),
        period_end=latest_period_end,
        acceptance_time=latest_acceptance,
        source_observation_ids=tuple(sorted(fact.observation_id for fact in selected)),
    )


def calculate_fundamental_panel(
    evidence: Sequence[IssuerFundamentalEvidence],
    config: FundamentalCalculationConfig,
) -> FundamentalPanelSnapshot:
    """Calculate issuer-specific components and normalized sleeve observations."""

    if not evidence:
        _invalid("fundamental calculation requires at least one issuer")
    if not isinstance(config, FundamentalCalculationConfig):
        _invalid("config must be a FundamentalCalculationConfig")
    by_symbol = {item.symbol: item for item in evidence}
    if len(by_symbol) != len(evidence):
        _invalid("fundamental issuer symbols must be unique")
    cutoffs = {item.cutoff for item in evidence}
    if len(cutoffs) != 1:
        _invalid("fundamental issuers must share one synchronized cutoff")
    decision_cutoffs = {item.decision_cutoff for item in evidence}
    if len(decision_cutoffs) != 1:
        _invalid("fundamental issuers must share one synchronized decision cutoff")
    cutoff = next(iter(cutoffs))
    calculations = tuple(
        _calculate_instrument(by_symbol[symbol], config) for symbol in sorted(by_symbol)
    )

    component_cross_sections: list[FundamentalSleeveCrossSection] = []
    sleeve_observations: list[FactorObservation] = []
    ranker = config.build_ranker()
    calculations_by_type: dict[IssuerType, list[FundamentalInstrumentCalculation]] = defaultdict(
        list
    )
    for calculation in calculations:
        calculations_by_type[calculation.issuer_type].append(calculation)

    for issuer_type in sorted(calculations_by_type, key=lambda item: item.value):
        typed_calculations = calculations_by_type[issuer_type]
        entities = tuple(
            CrossSectionalEntity(
                entity_id=calculation.symbol,
                symbol=calculation.symbol,
                peer_groups=(calculation.peer_group,),
            )
            for calculation in typed_calculations
        )
        for sleeve in sorted(_COMPONENTS[issuer_type], key=lambda item: item.value):
            specs = _COMPONENTS[issuer_type][sleeve]
            definitions = tuple(
                FactorSpec(
                    name=spec.name,
                    weight=spec.weight,
                    enabled=True,
                )
                for spec in specs
            )
            component_by_symbol = {
                calculation.symbol: {
                    component.name: component for component in calculation.components
                }
                for calculation in typed_calculations
            }
            observations = tuple(
                FactorObservation(
                    entity_id=calculation.symbol,
                    factor_name=spec.name,
                    raw_value=(
                        component := component_by_symbol[calculation.symbol][spec.name]
                    ).value,
                    missing_reason=component.missing_reason,
                    source_observation_ids=component.source_observation_ids,
                )
                for calculation in typed_calculations
                for spec in specs
            )
            component_snapshot = ranker.rank(
                cutoff=cutoff,
                entities=entities,
                factor_specs=definitions,
                observations=observations,
            )
            component_cross_sections.append(
                FundamentalSleeveCrossSection(
                    issuer_type=issuer_type,
                    sleeve=sleeve,
                    snapshot=component_snapshot,
                )
            )
            ranks_by_entity = {
                instrument.entity_id: instrument for instrument in component_snapshot.ranks
            }
            missing_by_entity: dict[str, list[str]] = defaultdict(list)
            sources_by_entity: dict[str, set[str]] = defaultdict(set)
            for contribution in component_snapshot.contributions:
                sources_by_entity[contribution.entity_id].update(
                    contribution.source_observation_ids
                )
            for decision in component_snapshot.missing_decisions:
                missing_by_entity[decision.entity_id].append(
                    f"{decision.factor_name}: {decision.detail}"
                )
                sources_by_entity[decision.entity_id].update(decision.source_observation_ids)
            for calculation in typed_calculations:
                instrument = ranks_by_entity.get(calculation.symbol)
                sleeve_observations.append(
                    FactorObservation(
                        entity_id=calculation.symbol,
                        factor_name=sleeve.value,
                        raw_value=(instrument.composite_score if instrument is not None else None),
                        missing_reason=(
                            None
                            if instrument is not None
                            else "; ".join(sorted(missing_by_entity[calculation.symbol]))
                            or "fundamental component cross-section is incomplete"
                        ),
                        source_observation_ids=tuple(sorted(sources_by_entity[calculation.symbol])),
                    )
                )

    canonical_cross_sections = tuple(
        sorted(
            component_cross_sections,
            key=lambda item: (item.issuer_type.value, item.sleeve.value),
        )
    )
    canonical_observations = tuple(
        sorted(sleeve_observations, key=lambda item: (item.entity_id, item.factor_name))
    )
    digest = _fundamental_panel_digest(
        cutoff,
        config,
        calculations,
        canonical_cross_sections,
        canonical_observations,
    )
    return FundamentalPanelSnapshot(
        cutoff=cutoff,
        configuration=config,
        calculations=calculations,
        component_cross_sections=canonical_cross_sections,
        sleeve_observations=canonical_observations,
        content_digest=digest,
    )


def _calculate_instrument(
    evidence: IssuerFundamentalEvidence,
    config: FundamentalCalculationConfig,
) -> FundamentalInstrumentCalculation:
    issuer_type = classify_issuer_type(evidence.historical_sic)
    assert evidence.decision_cutoff is not None
    period_ends = sorted(
        {
            fact.period_end
            for fact in evidence.facts
            if fact.metric is CanonicalFundamentalMetric.ASSETS
            and fact.acceptance_time <= evidence.decision_cutoff
        },
        reverse=True,
    )
    current_end = period_ends[0] if period_ends else None
    prior_end = period_ends[1] if len(period_ends) > 1 else None
    component_names = {
        spec.name for sleeve_specs in _COMPONENTS[issuer_type].values() for spec in sleeve_specs
    }
    components = tuple(
        _calculate_component(
            name,
            evidence,
            current_end=current_end,
            prior_end=prior_end,
            config=config,
        )
        for name in sorted(component_names)
    )
    return FundamentalInstrumentCalculation(
        symbol=evidence.symbol,
        issuer_type=issuer_type,
        peer_group=evidence.peer_group,
        current_period_end=current_end,
        prior_period_end=prior_end,
        components=components,
    )


@dataclass(frozen=True, slots=True)
class _SelectedFact:
    value: Decimal
    period_start: date | None
    period_end: date
    source_observation_ids: tuple[str, ...]


class _ComponentUnavailableError(ValueError):
    pass


def _component_unavailable(message: str) -> NoReturn:
    raise _ComponentUnavailableError(message)


def _calculate_component(
    name: str,
    evidence: IssuerFundamentalEvidence,
    *,
    current_end: date | None,
    prior_end: date | None,
    config: FundamentalCalculationConfig,
) -> FundamentalComponentValue:
    try:
        value, source_ids = _component_value(
            name,
            evidence,
            current_end=current_end,
            prior_end=prior_end,
            config=config,
        )
    except _ComponentUnavailableError as exc:
        return FundamentalComponentValue(
            name=name,
            value=None,
            source_observation_ids=(evidence.classification_source_id,),
            missing_reason=str(exc),
        )
    return FundamentalComponentValue(
        name=name,
        value=float(value),
        source_observation_ids=tuple(sorted(source_ids | {evidence.classification_source_id})),
    )


def _component_value(  # noqa: PLR0911
    name: str,
    evidence: IssuerFundamentalEvidence,
    *,
    current_end: date | None,
    prior_end: date | None,
    config: FundamentalCalculationConfig,
) -> tuple[Decimal, set[str]]:
    if current_end is None or prior_end is None:
        _component_unavailable("two cutoff-safe annual asset periods are required")
    assert evidence.decision_cutoff is not None
    age_days = (evidence.decision_cutoff.date() - current_end).days
    if age_days < 0 or age_days > config.max_fundamental_age_days:
        _component_unavailable("latest annual period is outside the registered freshness limit")

    def current(metric: CanonicalFundamentalMetric) -> _SelectedFact:
        return _select_fact(evidence, metric=metric, period_end=current_end)

    def prior(metric: CanonicalFundamentalMetric) -> _SelectedFact:
        return _select_fact(evidence, metric=metric, period_end=prior_end)

    assets_current = current(CanonicalFundamentalMetric.ASSETS)
    assets_prior = prior(CanonicalFundamentalMetric.ASSETS)
    average_assets = _positive_average(assets_current.value, assets_prior.value, "average assets")
    common_sources = set(
        assets_current.source_observation_ids + assets_prior.source_observation_ids
    )

    if name in {"earnings_yield", "cash_flow_yield", "book_to_market", "operating_income_yield"}:
        market_cap = evidence.market_cap
        if market_cap is None or market_cap.available_at > evidence.cutoff:
            _component_unavailable("cutoff-safe market capitalization is missing")
        metric_by_name = {
            "earnings_yield": CanonicalFundamentalMetric.NET_INCOME,
            "cash_flow_yield": CanonicalFundamentalMetric.OPERATING_CASH_FLOW,
            "book_to_market": CanonicalFundamentalMetric.EQUITY,
            "operating_income_yield": CanonicalFundamentalMetric.OPERATING_INCOME,
        }
        numerator = current(metric_by_name[name])
        return (
            numerator.value / market_cap.value,
            set(numerator.source_observation_ids) | {market_cap.source_observation_id},
        )

    if name == "revenue_growth":
        revenue_current = current(CanonicalFundamentalMetric.REVENUE)
        revenue_prior = prior(CanonicalFundamentalMetric.REVENUE)
        _validate_comparable_annual_flows(revenue_current, revenue_prior, config)
        if revenue_current.value <= 0 or revenue_prior.value <= 0:
            _component_unavailable("revenue growth requires positive comparable revenue")
        return (
            revenue_current.value / revenue_prior.value - Decimal(1),
            set(revenue_current.source_observation_ids + revenue_prior.source_observation_ids),
        )

    if name in {"operating_profit_growth", "net_income_growth", "cash_flow_growth"}:
        metric_by_name = {
            "operating_profit_growth": CanonicalFundamentalMetric.OPERATING_INCOME,
            "net_income_growth": CanonicalFundamentalMetric.NET_INCOME,
            "cash_flow_growth": CanonicalFundamentalMetric.OPERATING_CASH_FLOW,
        }
        value_current = current(metric_by_name[name])
        value_prior = prior(metric_by_name[name])
        _validate_comparable_annual_flows(value_current, value_prior, config)
        return (
            (value_current.value - value_prior.value) / average_assets,
            common_sources
            | set(value_current.source_observation_ids + value_prior.source_observation_ids),
        )

    if name in {
        "cash_return_on_assets",
        "operating_profitability",
        "return_on_assets",
    }:
        metric_by_name = {
            "cash_return_on_assets": CanonicalFundamentalMetric.OPERATING_CASH_FLOW,
            "operating_profitability": CanonicalFundamentalMetric.OPERATING_INCOME,
            "return_on_assets": CanonicalFundamentalMetric.NET_INCOME,
        }
        numerator = current(metric_by_name[name])
        _validate_annual_flow(numerator, config)
        return (
            numerator.value / average_assets,
            common_sources | set(numerator.source_observation_ids),
        )

    if name == "accrual_quality":
        cash_flow = current(CanonicalFundamentalMetric.OPERATING_CASH_FLOW)
        net_income = current(CanonicalFundamentalMetric.NET_INCOME)
        _validate_annual_flow(cash_flow, config)
        _validate_annual_flow(net_income, config)
        return (
            (cash_flow.value - net_income.value) / average_assets,
            common_sources
            | set(cash_flow.source_observation_ids + net_income.source_observation_ids),
        )

    if name == "balance_sheet_safety":
        equity = current(CanonicalFundamentalMetric.EQUITY)
        return (
            equity.value / assets_current.value,
            set(equity.source_observation_ids + assets_current.source_observation_ids),
        )

    if name == "return_on_equity":
        equity_current = current(CanonicalFundamentalMetric.EQUITY)
        equity_prior = prior(CanonicalFundamentalMetric.EQUITY)
        average_equity = _positive_average(
            equity_current.value,
            equity_prior.value,
            "average equity",
        )
        net_income = current(CanonicalFundamentalMetric.NET_INCOME)
        _validate_annual_flow(net_income, config)
        return (
            net_income.value / average_equity,
            set(
                equity_current.source_observation_ids
                + equity_prior.source_observation_ids
                + net_income.source_observation_ids
            ),
        )
    msg = f"unregistered fundamental component {name}"
    raise AssertionError(msg)


def _select_fact(
    evidence: IssuerFundamentalEvidence,
    *,
    metric: CanonicalFundamentalMetric,
    period_end: date,
) -> _SelectedFact:
    assert evidence.decision_cutoff is not None
    candidates = tuple(
        fact
        for fact in evidence.facts
        if fact.metric is metric
        and fact.period_end == period_end
        and fact.acceptance_time <= evidence.decision_cutoff
    )
    if not candidates:
        _component_unavailable(f"{metric.value} is missing for annual period {period_end}")
    latest_acceptance = max(fact.acceptance_time for fact in candidates)
    latest = tuple(fact for fact in candidates if fact.acceptance_time == latest_acceptance)
    best_priority = min(fact.mapping_priority for fact in latest)
    selected = tuple(fact for fact in latest if fact.mapping_priority == best_priority)
    signatures = {(fact.value, fact.period_start) for fact in selected}
    if len(signatures) != 1:
        _component_unavailable(
            f"{metric.value} has ambiguous values in revision {latest_acceptance.isoformat()}"
        )
    value, period_start = next(iter(signatures))
    return _SelectedFact(
        value=value,
        period_start=period_start,
        period_end=period_end,
        source_observation_ids=tuple(sorted(fact.observation_id for fact in selected)),
    )


def _validate_annual_flow(
    fact: _SelectedFact,
    config: FundamentalCalculationConfig,
) -> None:
    if fact.period_start is None:
        _component_unavailable("annual flow fact has no period start")
    duration_days = (fact.period_end - fact.period_start).days
    if not config.minimum_annual_days <= duration_days <= config.maximum_annual_days:
        _component_unavailable("annual flow duration is outside the registered fiscal-year bounds")


def _validate_comparable_annual_flows(
    current: _SelectedFact,
    prior: _SelectedFact,
    config: FundamentalCalculationConfig,
) -> None:
    _validate_annual_flow(current, config)
    _validate_annual_flow(prior, config)
    assert current.period_start is not None
    assert prior.period_start is not None
    current_duration = (current.period_end - current.period_start).days
    prior_duration = (prior.period_end - prior.period_start).days
    if abs(current_duration - prior_duration) > config.maximum_annual_duration_difference_days:
        _component_unavailable(
            "annual flow periods differ beyond the registered comparability bound"
        )


def _positive_average(first: Decimal, second: Decimal, name: str) -> Decimal:
    average = (first + second) / Decimal(2)
    if not average.is_finite() or average <= 0:
        _component_unavailable(f"{name} must be positive")
    return average


def _fundamental_panel_digest(
    cutoff: datetime,
    config: FundamentalCalculationConfig,
    calculations: Sequence[FundamentalInstrumentCalculation],
    component_cross_sections: Sequence[FundamentalSleeveCrossSection],
    sleeve_observations: Sequence[FactorObservation],
) -> str:
    return canonical_json_hash(
        {
            "calculation_schema": "sec-fundamental-panel-v2",
            "configuration": {
                "calculation_version": config.calculation_version,
                "max_fundamental_age_days": config.max_fundamental_age_days,
                "maximum_annual_days": config.maximum_annual_days,
                "maximum_annual_duration_difference_days": (
                    config.maximum_annual_duration_difference_days
                ),
                "minimum_annual_days": config.minimum_annual_days,
                "minimum_peer_count": config.minimum_peer_count,
                "winsorize_limit": config.winsorize_limit,
            },
            "calculations": [
                {
                    "components": [
                        {
                            "missing_reason": component.missing_reason,
                            "name": component.name,
                            "source_observation_ids": list(component.source_observation_ids),
                            "value": component.value,
                        }
                        for component in calculation.components
                    ],
                    "current_period_end": (
                        calculation.current_period_end.isoformat()
                        if calculation.current_period_end is not None
                        else None
                    ),
                    "issuer_type": calculation.issuer_type.value,
                    "peer_group": calculation.peer_group,
                    "prior_period_end": (
                        calculation.prior_period_end.isoformat()
                        if calculation.prior_period_end is not None
                        else None
                    ),
                    "symbol": calculation.symbol,
                }
                for calculation in calculations
            ],
            "component_cross_sections": [
                {
                    "content_digest": cross_section.snapshot.content_digest,
                    "issuer_type": cross_section.issuer_type.value,
                    "sleeve": cross_section.sleeve.value,
                }
                for cross_section in component_cross_sections
            ],
            "cutoff": cutoff.astimezone(UTC).isoformat(),
            "sleeve_observations": [
                {
                    "entity_id": observation.entity_id,
                    "factor_name": observation.factor_name,
                    "missing_reason": observation.missing_reason,
                    "raw_value": observation.raw_value,
                    "source_observation_ids": list(observation.source_observation_ids),
                }
                for observation in sleeve_observations
            ],
        }
    )
