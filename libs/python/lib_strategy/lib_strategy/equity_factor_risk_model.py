"""Deterministic point-in-time descriptive style-exposure control model.

This module is deliberately provider-, persistence-, and strategy-alpha-free.
It consumes immutable daily total-return observations plus raw accounting
ratios and emits descriptive beta/style exposures.  It is not a covariance,
specific-risk, VaR, tracking-error, or return-forecast model.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise
from typing import NoReturn

from lib_common.hashing import canonical_json_bytes, canonical_json_hash

from .equity_factor_risk import (
    CANONICAL_STYLE_RISK_FACTORS,
    FactorRiskModelIdentity,
)
from .equity_market_factors import (
    DailyEquityMarketObservation,
    EquityMarketFactorInput,
    conservative_split_coordinate_notional,
)

INTERNAL_FACTOR_RISK_PROVIDER = "vynmatrix"
INTERNAL_FACTOR_RISK_MODEL_ID = "sp500-pit-descriptive-style-exposure-control"
INTERNAL_FACTOR_RISK_MODEL_VERSION = "2.0.0"
INTERNAL_FACTOR_RISK_PRODUCT = "internally-derived-pit-style-exposure-control"
INTERNAL_FACTOR_RISK_ENDPOINT = "vynmatrix://equity-evidence/factor-risk"
INTERNAL_FACTOR_RISK_ENTITLEMENT_SCOPE = "personal-research-derived-eodhd-sec-factor-risk"
INTERNAL_FACTOR_RISK_ADJUSTMENT_POLICY = "not-applicable-derived-style-exposure-v2"
INTERNAL_FACTOR_RISK_MISSING_DATA_POLICY = "fail-closed"
INTERNAL_FACTOR_RISK_DATASET_VERSION = "sp500-pit-style-exposure-input-v2"
INTERNAL_FACTOR_RISK_TOOL_VERSION = "sp500-pit-style-exposure-calculator-v2"
INTERNAL_FACTOR_RISK_INPUT_SCHEMA = "sp500-pit-style-exposure-input-manifest-v2"
INTERNAL_FACTOR_RISK_CALCULATION_SCHEMA = "sp500-pit-style-exposure-calculation-v2"

_BETA_RETURN_SESSIONS = 252
_BETA_CLOSES = _BETA_RETURN_SESSIONS + 1
_ANNUALIZATION = 252
_LIQUIDITY_SESSIONS = 126
_MOMENTUM_SESSIONS = 252
_MOMENTUM_SKIP_SESSIONS = 21
_MOMENTUM_CLOSES = _MOMENTUM_SESSIONS + _MOMENTUM_SKIP_SESSIONS + 1
_ROBUST_CLIP = 3.0
_MINIMUM_SECTOR_SIZE = 5
_MAD_NORMALIZER = 1.4826
_QUANTUM = Decimal("0.000000000000000001")
_SHA256_LENGTH = 64
_BENCHMARK_SYMBOL = "SPY"

_QUALITY_COMPONENTS: Mapping[str, tuple[str, ...]] = {
    "operating_company": (
        "operating_profitability",
        "cash_return_on_assets",
        "accrual_quality",
        "balance_sheet_safety",
    ),
    "reit": (
        "operating_profitability",
        "cash_return_on_assets",
        "accrual_quality",
        "balance_sheet_safety",
    ),
    "bank_or_diversified_financial": (
        "return_on_assets",
        "return_on_equity",
        "balance_sheet_safety",
    ),
    "insurer": (
        "return_on_assets",
        "return_on_equity",
        "balance_sheet_safety",
    ),
}

INTERNAL_FACTOR_RISK_MODEL_DEFINITION = {
    "schema": "sp500-pit-descriptive-style-exposure-control-model-v2",
    "description": "descriptive exposure control; not covariance or return forecast",
    "provider": INTERNAL_FACTOR_RISK_PROVIDER,
    "model_id": INTERNAL_FACTOR_RISK_MODEL_ID,
    "model_version": INTERNAL_FACTOR_RISK_MODEL_VERSION,
    "benchmark": "SPY total return",
    "beta": {
        "returns": _BETA_RETURN_SESSIONS,
        "closes": _BETA_CLOSES,
        "regression": "OLS with intercept against simple SPY total returns",
    },
    "residual_volatility": {
        "annualization": _ANNUALIZATION,
        "formula": "log(sqrt(252 * residual_sum_squares / (252 - 2)))",
    },
    "liquidity": {
        "sessions": _LIQUIDITY_SESSIONS,
        "formula": "negative log median conservative split-coordinate notional",
    },
    "momentum": {
        "sessions": _MOMENTUM_SESSIONS,
        "skip_sessions": _MOMENTUM_SKIP_SESSIONS,
        "formula": "log ending total-return close / starting total-return close",
    },
    "size": "log cutoff-safe persisted market capitalization USD",
    "value": "asinh cutoff-safe book equity divided by market capitalization",
    "quality": {
        "formula": "asinh equal-weight mean of issuer-type raw component ratios",
        "issuer_type_components": {
            key: list(value) for key, value in sorted(_QUALITY_COMPONENTS.items())
        },
    },
    "normalization": {
        "sector_axes": [
            "liquidity",
            "momentum",
            "quality",
            "residual_volatility",
            "value",
        ],
        "global_axes": ["size"],
        "center": "median",
        "scale": "1.4826 MAD with sample-standard-deviation fallback",
        "clip": _ROBUST_CLIP,
        "minimum_complete_sector_size": _MINIMUM_SECTOR_SIZE,
        "neutralization": "market-cap-weighted mean",
        "final_scale": "full-cross-section market-cap-weighted RMS",
        "post_clip": False,
    },
    "numeric_storage": "Numeric(38,18) ROUND_HALF_EVEN",
    "styles": list(CANONICAL_STYLE_RISK_FACTORS),
    "missing_data_policy": INTERNAL_FACTOR_RISK_MISSING_DATA_POLICY,
}
INTERNAL_FACTOR_RISK_MODEL_DEFINITION_SHA256 = canonical_json_hash(
    INTERNAL_FACTOR_RISK_MODEL_DEFINITION
)
INTERNAL_FACTOR_RISK_MODEL = FactorRiskModelIdentity(
    provider=INTERNAL_FACTOR_RISK_PROVIDER,
    model_id=INTERNAL_FACTOR_RISK_MODEL_ID,
    model_version=INTERNAL_FACTOR_RISK_MODEL_VERSION,
    model_definition_sha256=INTERNAL_FACTOR_RISK_MODEL_DEFINITION_SHA256,
)


class FactorRiskModelError(ValueError):
    """The descriptive exposure model input is incomplete or inconsistent."""


def _invalid(message: str) -> NoReturn:
    raise FactorRiskModelError(message)


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


def _aware(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _finite(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        _invalid(f"{field_name} must be finite")
    result = float(value)
    if not math.isfinite(result):
        _invalid(f"{field_name} must be finite")
    return 0.0 if result == 0.0 else result


def _quantize(value: float) -> Decimal:
    result = Decimal(str(_finite(value, field_name="factor-risk result"))).quantize(
        _QUANTUM,
        rounding=ROUND_HALF_EVEN,
    )
    return Decimal(0).quantize(_QUANTUM) if result == 0 else result


@dataclass(frozen=True, slots=True)
class FactorRiskSourceReference:
    """One immutable atomic input identity and its authority digest."""

    observation_id: str
    authority_sha256: str
    available_at: datetime

    def __post_init__(self) -> None:
        _digest(self.observation_id, field_name="source observation_id")
        _digest(self.authority_sha256, field_name="source authority_sha256")
        object.__setattr__(
            self,
            "available_at",
            _aware(self.available_at, field_name="source available_at"),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "authority_sha256": self.authority_sha256,
            "available_at": self.available_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class FactorRiskBenchmarkObservation:
    """One observed SPY total-return close on an official session."""

    session_date: date
    total_return_close: float
    observed_at: datetime
    available_at: datetime
    source: FactorRiskSourceReference

    def __post_init__(self) -> None:
        if not isinstance(self.session_date, date):
            _invalid("benchmark session_date must be a date")
        object.__setattr__(
            self,
            "total_return_close",
            _finite(self.total_return_close, field_name="SPY total_return_close"),
        )
        if self.total_return_close <= 0.0:
            _invalid("SPY total_return_close must be positive")
        observed = _aware(self.observed_at, field_name="benchmark observed_at")
        available = _aware(self.available_at, field_name="benchmark available_at")
        if available < observed or self.source.available_at != available:
            _invalid("benchmark source availability is inconsistent")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)

    def to_payload(self) -> dict[str, object]:
        return {
            "session_date": self.session_date.isoformat(),
            "total_return_close": self.total_return_close.hex(),
            "observed_at": self.observed_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "source": self.source.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class FactorRiskBenchmarkInput:
    """Independent SPY total-return benchmark evidence for exposure regression."""

    instrument_id: int
    security_id: str
    symbol: str
    identity_source: FactorRiskSourceReference
    observations: tuple[FactorRiskBenchmarkObservation, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.instrument_id, bool)
            or not isinstance(self.instrument_id, int)
            or self.instrument_id < 1
        ):
            _invalid("benchmark instrument_id must be a positive integer")
        _text(self.security_id, field_name="benchmark security_id")
        if self.symbol != _BENCHMARK_SYMBOL:
            _invalid("factor-risk benchmark must be SPY total return")
        if not isinstance(self.identity_source, FactorRiskSourceReference):
            _invalid("benchmark identity source must be typed")
        if not isinstance(self.observations, tuple) or not self.observations:
            _invalid("benchmark observations must be a non-empty immutable tuple")
        canonical = tuple(sorted(self.observations, key=lambda item: item.session_date))
        if self.observations != canonical or len(
            {item.session_date for item in self.observations}
        ) != len(self.observations):
            _invalid("benchmark observations must be unique and canonically ordered")


@dataclass(frozen=True, slots=True)
class FactorRiskRawComponent:
    """One raw accounting ratio, never an alpha rank or contribution."""

    name: str
    value: float
    sources: tuple[FactorRiskSourceReference, ...]

    def __post_init__(self) -> None:
        _text(self.name, field_name="raw component name")
        object.__setattr__(self, "value", _finite(self.value, field_name=self.name))
        _canonical_sources(self.sources, field_name=f"{self.name} sources")


@dataclass(frozen=True, slots=True)
class FactorRiskFundamentalInput:
    """Raw, cutoff-safe accounting and market-cap inputs for one security."""

    instrument_id: int
    security_id: str
    symbol: str
    sector: str
    issuer_type: str
    market_cap_usd: Decimal
    book_to_market: float
    quality_components: tuple[FactorRiskRawComponent, ...]
    market_cap_sources: tuple[FactorRiskSourceReference, ...]
    book_to_market_sources: tuple[FactorRiskSourceReference, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.instrument_id, bool)
            or not isinstance(self.instrument_id, int)
            or self.instrument_id < 1
        ):
            _invalid("fundamental instrument_id must be a positive integer")
        _text(self.security_id, field_name="fundamental security_id")
        symbol = _text(self.symbol, field_name="fundamental symbol")
        if symbol != symbol.upper():
            _invalid("fundamental symbol must be canonical uppercase")
        _text(self.sector, field_name="fundamental sector")
        issuer_type = _text(self.issuer_type, field_name="fundamental issuer_type")
        expected = _QUALITY_COMPONENTS.get(issuer_type)
        if expected is None:
            _invalid("fundamental issuer_type is not registered")
        if (
            not isinstance(self.market_cap_usd, Decimal)
            or not self.market_cap_usd.is_finite()
            or self.market_cap_usd <= 0
        ):
            _invalid("market_cap_usd must be a positive finite Decimal")
        object.__setattr__(
            self,
            "book_to_market",
            _finite(self.book_to_market, field_name="book_to_market"),
        )
        if not isinstance(self.quality_components, tuple):
            _invalid("quality_components must be an immutable tuple")
        canonical = tuple(sorted(self.quality_components, key=lambda item: item.name))
        if self.quality_components != canonical or not all(
            isinstance(item, FactorRiskRawComponent) for item in canonical
        ):
            _invalid("quality_components must be typed and canonically ordered")
        if tuple(item.name for item in canonical) != tuple(sorted(expected)):
            _invalid("quality_components do not match the frozen issuer-type definition")
        _canonical_sources(self.market_cap_sources, field_name="market-cap sources")
        _canonical_sources(self.book_to_market_sources, field_name="book-to-market sources")


@dataclass(frozen=True, slots=True)
class FactorRiskModelInput:
    """Complete raw cross-section and exact source-authority manifest inputs."""

    market: EquityMarketFactorInput
    benchmark: FactorRiskBenchmarkInput
    fundamentals: tuple[FactorRiskFundamentalInput, ...]
    required_security_ids: tuple[str, ...]
    security_identity_sources: tuple[FactorRiskSourceReference, ...]
    membership_sha256: str
    provider_authority_sha256: str
    market_policy_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.market, EquityMarketFactorInput):
            _invalid("factor-risk market input must use EquityMarketFactorInput")
        if not isinstance(self.benchmark, FactorRiskBenchmarkInput):
            _invalid("factor-risk benchmark input must be typed")
        if not isinstance(self.fundamentals, tuple) or not all(
            isinstance(item, FactorRiskFundamentalInput) for item in self.fundamentals
        ):
            _invalid("factor-risk fundamentals must be a typed immutable tuple")
        canonical_fundamentals = tuple(sorted(self.fundamentals, key=lambda item: item.security_id))
        if len({item.security_id for item in canonical_fundamentals}) != len(
            canonical_fundamentals
        ):
            _invalid("factor-risk fundamental security IDs must be unique")
        object.__setattr__(self, "fundamentals", canonical_fundamentals)
        if len(set(self.required_security_ids)) != len(self.required_security_ids):
            _invalid("required factor-risk security IDs must be unique")
        object.__setattr__(
            self,
            "required_security_ids",
            tuple(sorted(self.required_security_ids)),
        )
        if not self.required_security_ids:
            _invalid("factor-risk calculation requires at least one rankable security")
        _canonical_sources(
            self.security_identity_sources,
            field_name="security identity sources",
        )
        expected_identity_ids = {
            item.observation_id
            for item in self.market.members
            if item.security_id in self.required_security_ids
        }
        if {item.observation_id for item in self.security_identity_sources} != (
            expected_identity_ids
        ):
            _invalid("security identity sources must exactly cover members and SPY")
        for value, name in (
            (self.membership_sha256, "membership_sha256"),
            (self.provider_authority_sha256, "provider_authority_sha256"),
            (self.market_policy_sha256, "market_policy_sha256"),
        ):
            _digest(value, field_name=name)


@dataclass(frozen=True, slots=True)
class CalculatedFactorRiskExposure:
    """Quantized model output and exact per-security source set."""

    instrument_id: int
    security_id: str
    symbol: str
    market_beta: Decimal
    raw_descriptors: tuple[tuple[str, Decimal], ...]
    style_exposures: tuple[tuple[str, Decimal], ...]
    source_references: tuple[FactorRiskSourceReference, ...]
    source_observation_set_sha256: str
    calculation_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.raw_descriptors != tuple(sorted(self.raw_descriptors, key=lambda item: item[0])):
            _invalid("raw descriptors must be canonically ordered")
        if tuple(name for name, _value in self.raw_descriptors) != (CANONICAL_STYLE_RISK_FACTORS):
            _invalid("raw descriptors must cover the canonical style taxonomy")
        if tuple(name for name, _value in self.style_exposures) != (CANONICAL_STYLE_RISK_FACTORS):
            _invalid("style exposures must cover the canonical style taxonomy")
        for _name, value in (*self.raw_descriptors, *self.style_exposures):
            if not isinstance(value, Decimal) or not value.is_finite():
                _invalid("factor-risk output values must be finite Decimals")
        if not isinstance(self.market_beta, Decimal) or not self.market_beta.is_finite():
            _invalid("market beta must be a finite Decimal")
        _canonical_sources(self.source_references, field_name="exposure sources")
        if self.source_observation_set_sha256 != _source_set_sha256(self.source_references):
            _invalid("exposure source-observation digest differs from its references")
        object.__setattr__(
            self,
            "calculation_sha256",
            canonical_json_hash(self.to_payload(include_calculation=False)),
        )

    def to_payload(self, *, include_calculation: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "instrument_id": self.instrument_id,
            "security_id": self.security_id,
            "symbol": self.symbol,
            "market_beta": str(self.market_beta),
            "raw_descriptors": {name: str(value) for name, value in self.raw_descriptors},
            "style_exposures": {name: str(value) for name, value in self.style_exposures},
            "source_references": [item.to_payload() for item in self.source_references],
            "source_observation_set_sha256": self.source_observation_set_sha256,
        }
        if include_calculation:
            payload["calculation_sha256"] = self.calculation_sha256
        return payload


@dataclass(frozen=True, slots=True)
class CalculatedFactorRiskPanel:
    """One complete raw-input calculation before persistence adaptation."""

    effective_session: date
    observed_at: datetime
    cutoff: datetime
    model: FactorRiskModelIdentity
    exposures: tuple[CalculatedFactorRiskExposure, ...]
    market_input_sha256: str
    fundamental_input_sha256: str
    membership_sha256: str
    provider_authority_sha256: str
    market_policy_sha256: str
    input_manifest_sha256: str
    input_manifest_json: str
    source_references: tuple[FactorRiskSourceReference, ...]
    available_at: datetime
    calculation_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.model != INTERNAL_FACTOR_RISK_MODEL:
            _invalid("calculated factor-risk panel uses an unregistered model")
        object.__setattr__(
            self,
            "observed_at",
            _aware(self.observed_at, field_name="panel observed_at"),
        )
        object.__setattr__(self, "cutoff", _aware(self.cutoff, field_name="panel cutoff"))
        object.__setattr__(
            self,
            "available_at",
            _aware(self.available_at, field_name="panel available_at"),
        )
        if self.available_at > self.cutoff:
            _invalid("factor-risk inputs were unavailable at the calculation cutoff")
        if self.observed_at > self.available_at:
            _invalid("factor-risk exposure cannot be available before the official close")
        canonical = tuple(sorted(self.exposures, key=lambda item: item.security_id))
        if self.exposures != canonical or not self.exposures:
            _invalid("calculated exposures must be non-empty and canonically ordered")
        for field_name in (
            "market_input_sha256",
            "fundamental_input_sha256",
            "membership_sha256",
            "provider_authority_sha256",
            "market_policy_sha256",
            "input_manifest_sha256",
        ):
            _digest(getattr(self, field_name), field_name=field_name)
        _canonical_sources(self.source_references, field_name="panel sources")
        try:
            manifest = json.loads(self.input_manifest_json)
        except (json.JSONDecodeError, TypeError) as exc:
            message = "factor-risk input manifest is invalid JSON"
            raise FactorRiskModelError(message) from exc
        if (
            not isinstance(manifest, dict)
            or canonical_json_bytes(manifest).decode("utf-8") != self.input_manifest_json
            or canonical_json_hash(manifest) != self.input_manifest_sha256
        ):
            _invalid("factor-risk input manifest digest is not internally bound")
        object.__setattr__(
            self,
            "calculation_sha256",
            canonical_json_hash(
                {
                    "schema": INTERNAL_FACTOR_RISK_CALCULATION_SCHEMA,
                    "model": self.model.to_payload(),
                    "effective_session": self.effective_session.isoformat(),
                    "observed_at": self.observed_at.isoformat(),
                    "cutoff": self.cutoff.isoformat(),
                    "input_manifest_sha256": self.input_manifest_sha256,
                    "exposures": [item.to_payload() for item in self.exposures],
                }
            ),
        )


def calculate_internal_factor_risk_panel(  # noqa: PLR0915
    model_input: FactorRiskModelInput,
) -> CalculatedFactorRiskPanel:
    """Calculate the frozen model from raw PIT inputs only."""

    if not isinstance(model_input, FactorRiskModelInput):
        _invalid("factor-risk calculator requires FactorRiskModelInput")
    market = model_input.market
    member_by_security = {item.security_id: item for item in market.members}
    fundamental_by_security = {item.security_id: item for item in model_input.fundamentals}
    required = model_input.required_security_ids
    if set(required) - set(member_by_security):
        _invalid("required factor-risk names are absent from the PIT market universe")
    if set(fundamental_by_security) != set(required):
        _invalid("factor-risk fundamental coverage must exactly equal the rankable set")
    for security_id in required:
        security = member_by_security[security_id]
        fundamental = fundamental_by_security[security_id]
        if (
            fundamental.instrument_id != security.instrument_id
            or fundamental.symbol != security.symbol
            or fundamental.sector != security.sector
        ):
            _invalid("factor-risk fundamental and permanent market identity differ")

    price_by_instrument: dict[int, list[DailyEquityMarketObservation]] = defaultdict(list)
    for item in market.prices:
        price_by_instrument[item.instrument_id].append(item)
    for values in price_by_instrument.values():
        values.sort(key=lambda item: item.session_date)
    benchmark_prices = model_input.benchmark.observations
    _require_benchmark_history(benchmark_prices, market=market)
    benchmark_returns = _simple_returns(
        [item.total_return_close for item in benchmark_prices[-_BETA_CLOSES:]]
    )
    benchmark_mean = math.fsum(benchmark_returns) / len(benchmark_returns)
    benchmark_variance_sum = math.fsum((value - benchmark_mean) ** 2 for value in benchmark_returns)
    if benchmark_variance_sum <= 0.0 or not math.isfinite(benchmark_variance_sum):
        _invalid("SPY total-return variance is zero or invalid")

    raw_by_security: dict[str, dict[str, float]] = {}
    beta_by_security: dict[str, float] = {}
    sources_by_security: dict[str, tuple[FactorRiskSourceReference, ...]] = {}
    identity_by_observation = {
        item.observation_id: item for item in model_input.security_identity_sources
    }
    benchmark_identity_source = model_input.benchmark.identity_source
    for security_id in required:
        security = member_by_security[security_id]
        fundamental = fundamental_by_security[security_id]
        prices = price_by_instrument.get(security.instrument_id, [])
        _require_history(prices, market=market, field_name=security.symbol)
        returns = _simple_returns([item.total_return_close for item in prices[-_BETA_CLOSES:]])
        return_mean = math.fsum(returns) / len(returns)
        beta = (
            math.fsum(
                (asset - return_mean) * (benchmark - benchmark_mean)
                for asset, benchmark in zip(returns, benchmark_returns, strict=True)
            )
            / benchmark_variance_sum
        )
        alpha = return_mean - beta * benchmark_mean
        rss = math.fsum(
            (asset - alpha - beta * benchmark) ** 2
            for asset, benchmark in zip(returns, benchmark_returns, strict=True)
        )
        residual_volatility = math.sqrt(_ANNUALIZATION * rss / (_BETA_RETURN_SESSIONS - 2))
        if residual_volatility <= 0.0 or not math.isfinite(residual_volatility):
            _invalid(f"{security.symbol} residual volatility is zero or invalid")
        liquidity_window = prices[-_LIQUIDITY_SESSIONS:]
        median_notional = statistics.median(
            conservative_split_coordinate_notional(
                item.split_adjusted_close,
                item.split_adjusted_volume,
            )
            for item in liquidity_window
        )
        if median_notional <= 0.0 or not math.isfinite(median_notional):
            _invalid(f"{security.symbol} liquidity notional is zero or invalid")
        momentum_prices = prices[-_MOMENTUM_CLOSES:]
        momentum_start = momentum_prices[0].total_return_close
        momentum_end = momentum_prices[-(_MOMENTUM_SKIP_SESSIONS + 1)].total_return_close
        if momentum_start <= 0.0 or momentum_end <= 0.0:
            _invalid(f"{security.symbol} momentum prices must be positive")
        quality = math.asinh(
            math.fsum(item.value for item in fundamental.quality_components)
            / len(fundamental.quality_components)
        )
        raw_by_security[security_id] = {
            "liquidity": -math.log(median_notional),
            "momentum": math.log(momentum_end / momentum_start),
            "quality": quality,
            "residual_volatility": math.log(residual_volatility),
            "size": math.log(float(fundamental.market_cap_usd)),
            "value": math.asinh(fundamental.book_to_market),
        }
        beta_by_security[security_id] = beta
        price_sources = tuple(
            FactorRiskSourceReference(
                observation_id=item.observation_id,
                authority_sha256=item.observation_sha256,
                available_at=item.available_at,
            )
            for item in prices[-_MOMENTUM_CLOSES:]
        )
        benchmark_sources = tuple(item.source for item in benchmark_prices[-_BETA_CLOSES:])
        identity_source = identity_by_observation[security.observation_id]
        fundamental_sources = (
            tuple(
                source
                for component in fundamental.quality_components
                for source in component.sources
            )
            + fundamental.market_cap_sources
            + fundamental.book_to_market_sources
        )
        sources_by_security[security_id] = _merge_sources(
            (
                *price_sources,
                *benchmark_sources,
                identity_source,
                benchmark_identity_source,
                *fundamental_sources,
            )
        )

    cap_by_security = {
        security_id: float(fundamental_by_security[security_id].market_cap_usd)
        for security_id in required
    }
    sector_by_security = {
        security_id: member_by_security[security_id].sector for security_id in required
    }
    styles = _normalize_styles(
        raw_by_security,
        cap_by_security=cap_by_security,
        sector_by_security=sector_by_security,
    )
    panel_sources = _merge_sources(
        tuple(source for security_id in required for source in sources_by_security[security_id])
    )
    market_input_sha256 = _market_input_sha256(model_input)
    fundamental_input_sha256 = _fundamental_input_sha256(model_input.fundamentals)
    manifest_payload = _input_manifest_payload(
        model_input=model_input,
        exposure_security_ids=required,
        source_references=panel_sources,
        market_input_sha256=market_input_sha256,
        fundamental_input_sha256=fundamental_input_sha256,
    )
    input_manifest_sha256 = canonical_json_hash(manifest_payload)
    exposures = tuple(
        CalculatedFactorRiskExposure(
            instrument_id=member_by_security[security_id].instrument_id,
            security_id=security_id,
            symbol=member_by_security[security_id].symbol,
            market_beta=_quantize(beta_by_security[security_id]),
            raw_descriptors=tuple(
                (name, _quantize(raw_by_security[security_id][name]))
                for name in CANONICAL_STYLE_RISK_FACTORS
            ),
            style_exposures=tuple(
                (name, _quantize(styles[security_id][name]))
                for name in CANONICAL_STYLE_RISK_FACTORS
            ),
            source_references=sources_by_security[security_id],
            source_observation_set_sha256=_source_set_sha256(sources_by_security[security_id]),
        )
        for security_id in required
    )
    return CalculatedFactorRiskPanel(
        effective_session=market.effective_session,
        observed_at=market.official_sessions[-1].closes_at,
        cutoff=market.cutoff,
        model=INTERNAL_FACTOR_RISK_MODEL,
        exposures=exposures,
        market_input_sha256=market_input_sha256,
        fundamental_input_sha256=fundamental_input_sha256,
        membership_sha256=model_input.membership_sha256,
        provider_authority_sha256=model_input.provider_authority_sha256,
        market_policy_sha256=model_input.market_policy_sha256,
        input_manifest_sha256=input_manifest_sha256,
        input_manifest_json=canonical_json_bytes(manifest_payload).decode("utf-8"),
        source_references=panel_sources,
        available_at=max(item.available_at for item in panel_sources),
    )


def factor_risk_quality_component_names(issuer_type: str) -> tuple[str, ...]:
    """Return the frozen raw quality ratios for one issuer accounting type."""

    names = _QUALITY_COMPONENTS.get(issuer_type)
    if names is None:
        _invalid("factor-risk issuer type is not registered")
    return tuple(sorted(names))


def factor_risk_input_manifest_payload(
    calculated: CalculatedFactorRiskPanel,
) -> dict[str, object]:
    """Return the exact registered input manifest for persistence validation."""

    try:
        payload = json.loads(calculated.input_manifest_json)
    except json.JSONDecodeError as exc:  # pragma: no cover - guarded by contract
        message = "calculated factor-risk manifest is invalid"
        raise FactorRiskModelError(message) from exc
    if not isinstance(payload, dict):  # pragma: no cover - guarded by contract
        _invalid("calculated factor-risk manifest must be an object")
    return payload


def _input_manifest_payload(
    *,
    model_input: FactorRiskModelInput,
    exposure_security_ids: Sequence[str],
    source_references: Sequence[FactorRiskSourceReference],
    market_input_sha256: str,
    fundamental_input_sha256: str,
) -> dict[str, object]:
    return {
        "schema": INTERNAL_FACTOR_RISK_INPUT_SCHEMA,
        "model": INTERNAL_FACTOR_RISK_MODEL.to_payload(),
        "model_definition": INTERNAL_FACTOR_RISK_MODEL_DEFINITION,
        "effective_session": model_input.market.effective_session.isoformat(),
        "cutoff": model_input.market.cutoff.isoformat(),
        "official_sessions": [
            item.session_date.isoformat() for item in model_input.market.official_sessions
        ],
        "benchmark": {
            "security_id": model_input.benchmark.security_id,
            "symbol": model_input.benchmark.symbol,
            "identity_source": model_input.benchmark.identity_source.to_payload(),
            "observations": [item.to_payload() for item in model_input.benchmark.observations],
        },
        "required_security_ids": list(exposure_security_ids),
        "security_identity_sources": [
            item.to_payload() for item in model_input.security_identity_sources
        ],
        "market_input_sha256": market_input_sha256,
        "fundamental_input_sha256": fundamental_input_sha256,
        "membership_sha256": model_input.membership_sha256,
        "provider_authority_sha256": model_input.provider_authority_sha256,
        "market_policy_sha256": model_input.market_policy_sha256,
        "source_references": [item.to_payload() for item in source_references],
        "missing_data_policy": INTERNAL_FACTOR_RISK_MISSING_DATA_POLICY,
    }


def _market_input_sha256(model_input: FactorRiskModelInput) -> str:
    market = model_input.market
    required = set(model_input.required_security_ids)
    allowed_instrument_ids = {
        item.instrument_id for item in market.members if item.security_id in required
    }
    return canonical_json_hash(
        {
            "schema": "sp500-factor-risk-market-input-v1",
            "effective_session": market.effective_session.isoformat(),
            "cutoff": market.cutoff.isoformat(),
            "official_sessions": [item.content_sha256 for item in market.official_sessions],
            "benchmark": market.benchmark.security_id,
            "factor_risk_benchmark": {
                "instrument_id": model_input.benchmark.instrument_id,
                "security_id": model_input.benchmark.security_id,
                "symbol": model_input.benchmark.symbol,
                "identity_source": model_input.benchmark.identity_source.to_payload(),
                "observations": [item.to_payload() for item in model_input.benchmark.observations],
            },
            "members": sorted(required),
            "security_identity_sources": [
                item.to_payload() for item in model_input.security_identity_sources
            ],
            "observations": [
                {
                    "instrument_id": item.instrument_id,
                    "session": item.session_date.isoformat(),
                    "observation_id": item.observation_id,
                    "observation_sha256": item.observation_sha256,
                    "total_return_close": item.total_return_close.hex(),
                    "split_adjusted_close": item.split_adjusted_close.hex(),
                    "split_adjusted_volume": item.split_adjusted_volume.hex(),
                }
                for item in sorted(
                    (
                        value
                        for value in market.prices
                        if value.instrument_id in allowed_instrument_ids
                    ),
                    key=lambda value: (value.instrument_id, value.session_date),
                )
            ],
        }
    )


def _fundamental_input_sha256(
    fundamentals: Sequence[FactorRiskFundamentalInput],
) -> str:
    return canonical_json_hash(
        {
            "schema": "sp500-factor-risk-fundamental-input-v1",
            "instruments": [
                {
                    "security_id": item.security_id,
                    "symbol": item.symbol,
                    "sector": item.sector,
                    "issuer_type": item.issuer_type,
                    "market_cap_usd": str(item.market_cap_usd),
                    "book_to_market": item.book_to_market.hex(),
                    "quality_components": [
                        {
                            "name": component.name,
                            "value": component.value.hex(),
                            "sources": [source.to_payload() for source in component.sources],
                        }
                        for component in item.quality_components
                    ],
                    "market_cap_sources": [
                        source.to_payload() for source in item.market_cap_sources
                    ],
                    "book_to_market_sources": [
                        source.to_payload() for source in item.book_to_market_sources
                    ],
                }
                for item in sorted(fundamentals, key=lambda value: value.security_id)
            ],
        }
    )


def _require_history(
    prices: Sequence[DailyEquityMarketObservation],
    *,
    market: EquityMarketFactorInput,
    field_name: str,
) -> None:
    required_dates = tuple(item.session_date for item in market.official_sessions)
    actual_dates = tuple(item.session_date for item in prices)
    if len(prices) < _MOMENTUM_CLOSES or actual_dates[-len(required_dates) :] != required_dates:
        _invalid(f"{field_name} lacks exact official-session history")
    if any(item.available_at > market.cutoff for item in prices[-_MOMENTUM_CLOSES:]):
        _invalid(f"{field_name} contains post-cutoff market evidence")


def _require_benchmark_history(
    prices: Sequence[FactorRiskBenchmarkObservation],
    *,
    market: EquityMarketFactorInput,
) -> None:
    required_dates = tuple(item.session_date for item in market.official_sessions)
    actual_dates = tuple(item.session_date for item in prices)
    if len(prices) < _MOMENTUM_CLOSES or actual_dates[-len(required_dates) :] != required_dates:
        _invalid("SPY lacks exact official-session total-return history")
    if any(item.available_at > market.cutoff for item in prices[-_MOMENTUM_CLOSES:]):
        _invalid("SPY contains post-cutoff total-return evidence")


def _simple_returns(closes: Sequence[float]) -> tuple[float, ...]:
    if len(closes) != _BETA_CLOSES:
        _invalid("beta calculation requires exactly 253 closes")
    result = tuple(current / previous - 1.0 for previous, current in pairwise(closes))
    if any(not math.isfinite(item) for item in result):
        _invalid("total-return series produced a non-finite return")
    return result


def _normalize_styles(
    raw: Mapping[str, Mapping[str, float]],
    *,
    cap_by_security: Mapping[str, float],
    sector_by_security: Mapping[str, str],
) -> dict[str, dict[str, float]]:
    security_ids = tuple(sorted(raw))
    sectors: dict[str, list[str]] = defaultdict(list)
    for security_id in security_ids:
        sectors[sector_by_security[security_id]].append(security_id)
    undersized = sorted(
        sector for sector, members in sectors.items() if len(members) < _MINIMUM_SECTOR_SIZE
    )
    if undersized:
        _invalid(f"factor-risk sectors have fewer than five complete names: {undersized}")
    residuals: dict[str, dict[str, float]] = {security_id: {} for security_id in security_ids}
    for factor_name in CANONICAL_STYLE_RISK_FACTORS:
        groups = {"__global__": list(security_ids)} if factor_name == "size" else sectors
        for members in groups.values():
            ordered = tuple(sorted(members))
            values = [raw[security_id][factor_name] for security_id in ordered]
            center = statistics.median(values)
            mad = statistics.median(abs(value - center) for value in values)
            scale = _MAD_NORMALIZER * mad
            if scale <= 0.0:
                scale = statistics.stdev(values) if len(values) > 1 else 0.0
            if scale <= 0.0 or not math.isfinite(scale):
                _invalid(f"factor-risk {factor_name} cross-section is constant")
            clipped = {
                security_id: min(
                    center + _ROBUST_CLIP * scale,
                    max(center - _ROBUST_CLIP * scale, raw[security_id][factor_name]),
                )
                for security_id in ordered
            }
            cap_sum = math.fsum(cap_by_security[item] for item in ordered)
            if cap_sum <= 0.0:
                _invalid("factor-risk market-cap weights are invalid")
            weighted_mean = (
                math.fsum(cap_by_security[item] * clipped[item] for item in ordered) / cap_sum
            )
            for security_id in ordered:
                residuals[security_id][factor_name] = clipped[security_id] - weighted_mean
        total_cap = math.fsum(cap_by_security[item] for item in security_ids)
        rms = math.sqrt(
            math.fsum(
                cap_by_security[item] * residuals[item][factor_name] ** 2 for item in security_ids
            )
            / total_cap
        )
        if rms <= 0.0 or not math.isfinite(rms):
            _invalid(f"factor-risk {factor_name} weighted RMS is zero")
        for security_id in security_ids:
            residuals[security_id][factor_name] /= rms
    return residuals


def _canonical_sources(
    values: tuple[FactorRiskSourceReference, ...],
    *,
    field_name: str,
) -> None:
    if (
        not isinstance(values, tuple)
        or not values
        or not all(isinstance(item, FactorRiskSourceReference) for item in values)
    ):
        _invalid(f"{field_name} must be a non-empty typed tuple")
    canonical = tuple(sorted(values, key=lambda item: item.observation_id))
    if values != canonical or len({item.observation_id for item in values}) != len(values):
        _invalid(f"{field_name} must be unique and canonically ordered")


def _merge_sources(
    values: Sequence[FactorRiskSourceReference],
) -> tuple[FactorRiskSourceReference, ...]:
    by_id: dict[str, FactorRiskSourceReference] = {}
    for item in values:
        existing = by_id.setdefault(item.observation_id, item)
        if existing != item:
            _invalid("one factor-risk source observation has divergent authority")
    return tuple(by_id[key] for key in sorted(by_id))


def _source_set_sha256(values: Sequence[FactorRiskSourceReference]) -> str:
    return str(
        canonical_json_hash(
            {
                "schema": "factor-risk-source-observation-set-v1",
                "sources": [item.to_payload() for item in values],
            }
        )
    )


__all__ = [
    "INTERNAL_FACTOR_RISK_ADJUSTMENT_POLICY",
    "INTERNAL_FACTOR_RISK_CALCULATION_SCHEMA",
    "INTERNAL_FACTOR_RISK_DATASET_VERSION",
    "INTERNAL_FACTOR_RISK_ENDPOINT",
    "INTERNAL_FACTOR_RISK_ENTITLEMENT_SCOPE",
    "INTERNAL_FACTOR_RISK_INPUT_SCHEMA",
    "INTERNAL_FACTOR_RISK_MISSING_DATA_POLICY",
    "INTERNAL_FACTOR_RISK_MODEL",
    "INTERNAL_FACTOR_RISK_MODEL_DEFINITION",
    "INTERNAL_FACTOR_RISK_MODEL_DEFINITION_SHA256",
    "INTERNAL_FACTOR_RISK_MODEL_ID",
    "INTERNAL_FACTOR_RISK_MODEL_VERSION",
    "INTERNAL_FACTOR_RISK_PRODUCT",
    "INTERNAL_FACTOR_RISK_PROVIDER",
    "INTERNAL_FACTOR_RISK_TOOL_VERSION",
    "CalculatedFactorRiskExposure",
    "CalculatedFactorRiskPanel",
    "FactorRiskBenchmarkInput",
    "FactorRiskBenchmarkObservation",
    "FactorRiskFundamentalInput",
    "FactorRiskModelError",
    "FactorRiskModelInput",
    "FactorRiskRawComponent",
    "FactorRiskSourceReference",
    "calculate_internal_factor_risk_panel",
    "factor_risk_input_manifest_payload",
    "factor_risk_quality_component_names",
]
