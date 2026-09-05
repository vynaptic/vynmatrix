"""Point-in-time portfolio factor-risk contracts and deterministic caps.

This module is deliberately separate from ``cross_sectional``.  The latter
owns alpha ranking and contribution arithmetic; these contracts describe an
external risk model and constrain the resulting equal-slot portfolio.  They
contain no provider client, persistence model, or strategy-specific alpha
proxy.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import NoReturn, Self

from lib_common.hashing import canonical_json_hash
from lib_strategy.data_authority import (
    DataUseScope,
    ProviderAuthorityPolicy,
)

FACTOR_RISK_OBSERVATION_KIND = "factor_risk_exposure"
FACTOR_RISK_EXPOSURE_CONTRACT = "pit-equity-factor-risk-exposure-v2"
FACTOR_RISK_TIMESTAMP_SEMANTICS_SCHEMA = "pit-equity-factor-risk-timestamp-semantics-v2"
FACTOR_RISK_PANEL_SCHEMA = "pit-equity-factor-risk-panel-v2"
FACTOR_RISK_POLICY_SCHEMA = "portfolio-factor-risk-policy-v1"
FACTOR_RISK_AUDIT_SCHEMA = "portfolio-factor-risk-audit-v1"
CANONICAL_STYLE_RISK_FACTORS = (
    "liquidity",
    "momentum",
    "quality",
    "residual_volatility",
    "size",
    "value",
)

_SHA256_LENGTH = 64
_WEIGHT_TOLERANCE = 1e-12
_UNPINNED_IDENTITIES = frozenset({"current", "latest", "unknown", "unversioned"})


class PortfolioFactorRiskError(ValueError):
    """Factor-risk evidence or cap arithmetic is incomplete or inconsistent."""


class PortfolioFactorRiskStatus(StrEnum):
    """Auditable activation state for one portfolio decision."""

    ACTIVE_WITHIN_CAPS = "active_within_caps"
    INACTIVE_HISTORICAL_MODEL_UNCONFIGURED = "inactive_historical_model_unconfigured"


def _invalid(message: str) -> NoReturn:
    raise PortfolioFactorRiskError(message)


def _text(value: object, *, field_name: str, lowercase: bool = False) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _invalid(f"{field_name} must be canonical non-blank text")
    if value.casefold() in _UNPINNED_IDENTITIES:
        _invalid(f"{field_name} must be a pinned identity")
    if lowercase and value != value.lower():
        _invalid(f"{field_name} must be a canonical lowercase identifier")
    return value


def _digest(value: object, *, field_name: str) -> str:
    digest = _text(value, field_name=field_name, lowercase=True)
    if len(digest) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in digest
    ):
        _invalid(f"{field_name} must be a lowercase SHA-256 digest")
    return digest


def _finite(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid(f"{field_name} must be finite")
    result = float(value)
    if not math.isfinite(result):
        _invalid(f"{field_name} must be finite")
    return 0.0 if result == 0.0 else result


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class FactorRiskModelIdentity:
    """Pinned provider/model semantics shared by every exposure in a panel."""

    provider: str
    model_id: str
    model_version: str
    model_definition_sha256: str

    def __post_init__(self) -> None:
        _text(self.provider, field_name="factor-risk provider", lowercase=True)
        _text(self.model_id, field_name="factor-risk model_id")
        _text(self.model_version, field_name="factor-risk model_version")
        _digest(
            self.model_definition_sha256,
            field_name="factor-risk model_definition_sha256",
        )

    def to_payload(self) -> dict[str, str]:
        """Return canonical model identity material."""

        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "model_definition_sha256": self.model_definition_sha256,
        }

    @classmethod
    def from_payload(cls, raw: Mapping[str, object]) -> Self:
        """Restore an exact model identity."""

        _require_keys(
            raw,
            expected={
                "provider",
                "model_id",
                "model_version",
                "model_definition_sha256",
            },
            field_name="factor-risk model",
        )
        return cls(
            provider=_text(raw.get("provider"), field_name="factor-risk provider"),
            model_id=_text(raw.get("model_id"), field_name="factor-risk model_id"),
            model_version=_text(raw.get("model_version"), field_name="factor-risk model_version"),
            model_definition_sha256=_digest(
                raw.get("model_definition_sha256"),
                field_name="factor-risk model_definition_sha256",
            ),
        )


@dataclass(frozen=True, slots=True)
class StyleRiskExposure:
    """One canonical standardized style exposure from a separate risk model."""

    factor_name: str
    standardized_exposure: float

    def __post_init__(self) -> None:
        if self.factor_name not in CANONICAL_STYLE_RISK_FACTORS:
            _invalid("style-risk factor_name is outside the canonical risk taxonomy")
        object.__setattr__(
            self,
            "standardized_exposure",
            _finite(self.standardized_exposure, field_name="standardized style exposure"),
        )

    def to_payload(self) -> dict[str, object]:
        """Return canonical style exposure material."""

        return {
            "factor_name": self.factor_name,
            "standardized_exposure": self.standardized_exposure,
        }


@dataclass(frozen=True, slots=True)
class EquityFactorRiskExposure:
    """One immutable, cutoff-safe security exposure with complete source lineage."""

    instrument_id: int
    security_id: str
    symbol: str
    model: FactorRiskModelIdentity
    benchmark_security_id: str
    input_manifest_sha256: str
    market_input_sha256: str
    fundamental_input_sha256: str
    membership_sha256: str
    provider_authority_sha256: str
    source_observation_set_sha256: str
    source_observation_count: int
    calculation_sha256: str
    observed_at: datetime
    available_at: datetime
    retrieved_at: datetime
    revision: int
    observation_id: str
    observation_content_sha256: str
    observation_authority_sha256: str
    lineage_id: str
    source_content_sha256: str
    source_product: str
    dataset_version: str
    tool_version: str
    source_revision: str
    timestamp_semantics_sha256: str
    adjustment_policy: str
    missing_data_policy: str
    entitlement_scope: str
    entitlement_owner_user_id: str | None
    market_beta: float
    raw_descriptors: tuple[tuple[str, float], ...]
    style_exposures: tuple[StyleRiskExposure, ...]

    def __post_init__(self) -> None:  # noqa: PLR0912
        if isinstance(self.instrument_id, bool) or not isinstance(self.instrument_id, int):
            _invalid("factor-risk instrument_id must be a positive integer")
        if self.instrument_id < 1:
            _invalid("factor-risk instrument_id must be a positive integer")
        _text(self.security_id, field_name="factor-risk security_id")
        _text(self.symbol, field_name="factor-risk symbol")
        if self.symbol != self.symbol.upper():
            _invalid("factor-risk symbol must be canonical uppercase")
        if not isinstance(self.model, FactorRiskModelIdentity):
            _invalid("factor-risk model identity must be typed")
        _text(self.benchmark_security_id, field_name="factor-risk benchmark_security_id")
        for field_name in (
            "input_manifest_sha256",
            "market_input_sha256",
            "fundamental_input_sha256",
            "membership_sha256",
            "provider_authority_sha256",
            "source_observation_set_sha256",
            "calculation_sha256",
        ):
            _digest(getattr(self, field_name), field_name=f"factor-risk {field_name}")
        if (
            isinstance(self.source_observation_count, bool)
            or not isinstance(self.source_observation_count, int)
            or self.source_observation_count < 1
        ):
            _invalid("factor-risk source_observation_count must be positive")
        observed_at = _aware_utc(self.observed_at, field_name="factor-risk observed_at")
        available_at = _aware_utc(self.available_at, field_name="factor-risk available_at")
        retrieved_at = _aware_utc(self.retrieved_at, field_name="factor-risk retrieved_at")
        if observed_at > available_at:
            _invalid("factor-risk exposure cannot be available before it was observed")
        if available_at > retrieved_at:
            _invalid("factor-risk retrieval cannot precede source availability")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "retrieved_at", retrieved_at)
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            _invalid("factor-risk revision must be a positive integer")
        for field_name in (
            "observation_id",
            "observation_content_sha256",
            "observation_authority_sha256",
            "lineage_id",
            "source_content_sha256",
            "timestamp_semantics_sha256",
        ):
            _digest(getattr(self, field_name), field_name=f"factor-risk {field_name}")
        _text(self.source_product, field_name="factor-risk source_product")
        _text(self.dataset_version, field_name="factor-risk dataset_version")
        _text(self.tool_version, field_name="factor-risk tool_version")
        _text(self.source_revision, field_name="factor-risk source_revision")
        _text(self.adjustment_policy, field_name="factor-risk adjustment_policy")
        if self.missing_data_policy != "fail-closed":
            _invalid("factor-risk sources must use the fail-closed missing-data policy")
        _text(self.entitlement_scope, field_name="factor-risk entitlement_scope")
        if self.entitlement_owner_user_id is not None:
            _text(
                self.entitlement_owner_user_id,
                field_name="factor-risk entitlement_owner_user_id",
            )
        object.__setattr__(
            self,
            "market_beta",
            _finite(self.market_beta, field_name="factor-risk market_beta"),
        )
        if not isinstance(self.raw_descriptors, tuple):
            _invalid("raw_descriptors must be an immutable tuple")
        if tuple(name for name, _value in self.raw_descriptors) != (CANONICAL_STYLE_RISK_FACTORS):
            _invalid("raw_descriptors must cover the complete canonical risk taxonomy")
        for _name, value in self.raw_descriptors:
            _finite(value, field_name="raw factor-risk descriptor")
        if not isinstance(self.style_exposures, tuple):
            _invalid("style_exposures must be an immutable tuple")
        canonical = tuple(sorted(self.style_exposures, key=lambda item: item.factor_name))
        if self.style_exposures != canonical or not all(
            isinstance(item, StyleRiskExposure) for item in canonical
        ):
            _invalid("style_exposures must be typed and canonically ordered")
        names = tuple(item.factor_name for item in canonical)
        if names != CANONICAL_STYLE_RISK_FACTORS:
            _invalid("style_exposures must cover the complete canonical risk taxonomy")

    def to_payload(self) -> dict[str, object]:
        """Return exact risk exposure and source-lineage identity material."""

        return {
            "instrument_id": self.instrument_id,
            "security_id": self.security_id,
            "symbol": self.symbol,
            "model": self.model.to_payload(),
            "benchmark_security_id": self.benchmark_security_id,
            "input_manifest_sha256": self.input_manifest_sha256,
            "market_input_sha256": self.market_input_sha256,
            "fundamental_input_sha256": self.fundamental_input_sha256,
            "membership_sha256": self.membership_sha256,
            "provider_authority_sha256": self.provider_authority_sha256,
            "source_observation_set_sha256": self.source_observation_set_sha256,
            "source_observation_count": self.source_observation_count,
            "calculation_sha256": self.calculation_sha256,
            "observed_at": self.observed_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "revision": self.revision,
            "observation_id": self.observation_id,
            "observation_content_sha256": self.observation_content_sha256,
            "observation_authority_sha256": self.observation_authority_sha256,
            "lineage_id": self.lineage_id,
            "source_content_sha256": self.source_content_sha256,
            "source_product": self.source_product,
            "dataset_version": self.dataset_version,
            "tool_version": self.tool_version,
            "source_revision": self.source_revision,
            "timestamp_semantics_sha256": self.timestamp_semantics_sha256,
            "adjustment_policy": self.adjustment_policy,
            "missing_data_policy": self.missing_data_policy,
            "entitlement_scope": self.entitlement_scope,
            "entitlement_owner_user_id": self.entitlement_owner_user_id,
            "market_beta": self.market_beta,
            "raw_descriptors": dict(self.raw_descriptors),
            "style_exposures": [item.to_payload() for item in self.style_exposures],
        }


@dataclass(frozen=True, slots=True)
class EquityFactorRiskPanel:
    """One revision-pinned risk-model cross-section at a decision cutoff."""

    cutoff: datetime
    model: FactorRiskModelIdentity
    exposures: tuple[EquityFactorRiskExposure, ...]
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        cutoff = _aware_utc(self.cutoff, field_name="factor-risk panel cutoff")
        object.__setattr__(self, "cutoff", cutoff)
        if not isinstance(self.model, FactorRiskModelIdentity):
            _invalid("factor-risk panel model must be typed")
        if not isinstance(self.exposures, tuple) or not self.exposures:
            _invalid("factor-risk panel requires at least one exposure")
        canonical = tuple(sorted(self.exposures, key=lambda item: item.security_id))
        if self.exposures != canonical or not all(
            isinstance(item, EquityFactorRiskExposure) for item in canonical
        ):
            _invalid("factor-risk exposures must be typed and canonically ordered")
        if len({item.security_id for item in canonical}) != len(canonical):
            _invalid("factor-risk security identities must be unique")
        if len({item.instrument_id for item in canonical}) != len(canonical):
            _invalid("factor-risk instrument identities must be unique")
        if len({item.symbol for item in canonical}) != len(canonical):
            _invalid("factor-risk symbols must be unique")
        for field_name in (
            "benchmark_security_id",
            "input_manifest_sha256",
            "market_input_sha256",
            "fundamental_input_sha256",
            "membership_sha256",
            "provider_authority_sha256",
            "calculation_sha256",
        ):
            if len({getattr(item, field_name) for item in canonical}) != 1:
                _invalid(f"factor-risk panel mixes {field_name} identities")
        for exposure in canonical:
            if exposure.model != self.model:
                _invalid("factor-risk panel mixes model identities")
            if exposure.observed_at > cutoff or exposure.available_at > cutoff:
                _invalid("factor-risk panel contains future-dated evidence")
        object.__setattr__(
            self,
            "content_sha256",
            canonical_json_hash(self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        """Return content-addressed panel identity material."""

        return {
            "schema": FACTOR_RISK_PANEL_SCHEMA,
            "cutoff": self.cutoff.isoformat(),
            "model": self.model.to_payload(),
            "exposures": [item.to_payload() for item in self.exposures],
        }


@dataclass(frozen=True, slots=True)
class StyleRiskCap:
    """Maximum absolute equal-slot portfolio exposure for one style axis."""

    factor_name: str
    maximum_absolute_exposure: float

    def __post_init__(self) -> None:
        if self.factor_name not in CANONICAL_STYLE_RISK_FACTORS:
            _invalid("style-risk cap is outside the canonical risk taxonomy")
        maximum = _finite(
            self.maximum_absolute_exposure,
            field_name="maximum absolute style-risk exposure",
        )
        if maximum <= 0.0:
            _invalid("maximum absolute style-risk exposure must be positive")
        object.__setattr__(self, "maximum_absolute_exposure", maximum)

    def to_payload(self) -> dict[str, object]:
        """Return canonical cap identity material."""

        return {
            "factor_name": self.factor_name,
            "maximum_absolute_exposure": self.maximum_absolute_exposure,
        }


@dataclass(frozen=True, slots=True)
class PortfolioFactorRiskPolicy:
    """Frozen model identity, freshness, and equal-slot portfolio caps."""

    policy_version: str
    model: FactorRiskModelIdentity | None
    maximum_age_days: int
    maximum_market_beta: float
    style_caps: tuple[StyleRiskCap, ...]
    require_forward_evidence: bool = True
    allow_inactive_historical_validation: bool = True
    policy_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.policy_version, field_name="factor-risk policy_version")
        if self.model is not None and not isinstance(self.model, FactorRiskModelIdentity):
            _invalid("factor-risk policy model must be typed or unconfigured")
        if (
            isinstance(self.maximum_age_days, bool)
            or not isinstance(self.maximum_age_days, int)
            or self.maximum_age_days < 1
        ):
            _invalid("factor-risk maximum_age_days must be a positive integer")
        maximum_beta = _finite(
            self.maximum_market_beta,
            field_name="maximum portfolio market beta",
        )
        if maximum_beta <= 0.0:
            _invalid("maximum portfolio market beta must be positive")
        object.__setattr__(self, "maximum_market_beta", maximum_beta)
        if not isinstance(self.style_caps, tuple):
            _invalid("style_caps must be an immutable tuple")
        canonical = tuple(sorted(self.style_caps, key=lambda item: item.factor_name))
        if self.style_caps != canonical or not all(
            isinstance(item, StyleRiskCap) for item in canonical
        ):
            _invalid("style_caps must be typed and canonically ordered")
        if tuple(item.factor_name for item in canonical) != CANONICAL_STYLE_RISK_FACTORS:
            _invalid("style_caps must cover the complete canonical risk taxonomy")
        if self.require_forward_evidence is not True:
            _invalid("factor-risk evidence must remain mandatory for forward scopes")
        if self.allow_inactive_historical_validation is not True:
            _invalid("historical missing-model behavior must remain explicitly diagnostic")
        object.__setattr__(self, "policy_sha256", canonical_json_hash(self.to_payload()))

    def to_payload(self) -> dict[str, object]:
        """Return deterministic policy identity material."""

        return {
            "schema": FACTOR_RISK_POLICY_SCHEMA,
            "policy_version": self.policy_version,
            "model": self.model.to_payload() if self.model is not None else None,
            "maximum_age_days": self.maximum_age_days,
            "maximum_market_beta": self.maximum_market_beta,
            "style_caps": [item.to_payload() for item in self.style_caps],
            "require_forward_evidence": self.require_forward_evidence,
            "allow_inactive_historical_validation": (self.allow_inactive_historical_validation),
        }


@dataclass(frozen=True, slots=True)
class PortfolioFactorRiskAudit:
    """Final selected-book risk arithmetic and activation disposition."""

    policy_sha256: str
    status: PortfolioFactorRiskStatus
    cap_active: bool
    panel_sha256: str | None
    slot_weight: float
    selected_security_ids: tuple[str, ...]
    portfolio_market_beta: float | None
    portfolio_style_exposures: tuple[tuple[str, float], ...]
    rejected_candidates: tuple[tuple[str, str], ...]
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _digest(self.policy_sha256, field_name="factor-risk policy_sha256")
        if not isinstance(self.status, PortfolioFactorRiskStatus):
            _invalid("factor-risk audit status must be typed")
        if not isinstance(self.cap_active, bool):
            _invalid("factor-risk cap_active must be boolean")
        if self.panel_sha256 is not None:
            _digest(self.panel_sha256, field_name="factor-risk panel_sha256")
        slot_weight = _finite(self.slot_weight, field_name="factor-risk slot_weight")
        if not 0.0 <= slot_weight <= 1.0:
            _invalid("factor-risk slot_weight must be in [0, 1]")
        object.__setattr__(self, "slot_weight", slot_weight)
        if self.selected_security_ids != tuple(sorted(set(self.selected_security_ids))):
            _invalid("factor-risk selected identities must be unique and canonical")
        for security_id in self.selected_security_ids:
            _text(security_id, field_name="factor-risk selected security_id")
        if self.portfolio_market_beta is not None:
            object.__setattr__(
                self,
                "portfolio_market_beta",
                _finite(self.portfolio_market_beta, field_name="portfolio market beta"),
            )
        style_names = tuple(name for name, _value in self.portfolio_style_exposures)
        if style_names not in ((), CANONICAL_STYLE_RISK_FACTORS):
            _invalid("factor-risk audit styles are incomplete or non-canonical")
        for _name, value in self.portfolio_style_exposures:
            _finite(value, field_name="portfolio style exposure")
        if self.rejected_candidates != tuple(sorted(set(self.rejected_candidates))):
            _invalid("factor-risk rejected candidates must be unique and canonical")
        for security_id, reason in self.rejected_candidates:
            _text(security_id, field_name="factor-risk rejected security_id")
            _text(reason, field_name="factor-risk rejection reason")
        if self.cap_active:
            if (
                self.status is not PortfolioFactorRiskStatus.ACTIVE_WITHIN_CAPS
                or self.panel_sha256 is None
                or self.portfolio_market_beta is None
                or style_names != CANONICAL_STYLE_RISK_FACTORS
            ):
                _invalid("active factor-risk audit lacks complete calculated evidence")
        elif (
            self.status is not PortfolioFactorRiskStatus.INACTIVE_HISTORICAL_MODEL_UNCONFIGURED
            or self.panel_sha256 is not None
            or self.portfolio_market_beta is not None
            or self.portfolio_style_exposures
            or self.rejected_candidates
        ):
            _invalid("inactive factor-risk audit cannot claim calculated risk evidence")
        object.__setattr__(self, "content_sha256", canonical_json_hash(self.to_payload()))

    def to_payload(self) -> dict[str, object]:
        """Return deterministic audit material for panel and signal lineage."""

        return {
            "schema": FACTOR_RISK_AUDIT_SCHEMA,
            "policy_sha256": self.policy_sha256,
            "status": self.status.value,
            "cap_active": self.cap_active,
            "panel_sha256": self.panel_sha256,
            "slot_weight": self.slot_weight,
            "selected_security_ids": list(self.selected_security_ids),
            "portfolio_market_beta": self.portfolio_market_beta,
            "portfolio_style_exposures": dict(self.portfolio_style_exposures),
            "rejected_candidates": [
                {"security_id": security_id, "reason": reason}
                for security_id, reason in self.rejected_candidates
            ],
        }


@dataclass(frozen=True, slots=True)
class PortfolioFactorRiskContext:
    """Validated risk panel plus pure deterministic cap calculations."""

    policy: PortfolioFactorRiskPolicy
    status: PortfolioFactorRiskStatus
    panel: EquityFactorRiskPanel | None

    @property
    def cap_active(self) -> bool:
        return self.status is PortfolioFactorRiskStatus.ACTIVE_WITHIN_CAPS

    def candidate_failure(
        self,
        *,
        selected_security_ids: Sequence[str],
        candidate_security_id: str,
        slot_weight: float,
    ) -> str | None:
        """Return the first cap breach for one deterministic greedy candidate."""

        if not self.cap_active:
            return None
        if _finite(slot_weight, field_name="factor-risk slot_weight") == 0.0:
            return None
        selected = tuple(selected_security_ids)
        if candidate_security_id in selected:
            _invalid("factor-risk candidate is already selected")
        market_beta, styles = self._aggregate(
            (*selected, candidate_security_id),
            slot_weight=slot_weight,
        )
        if market_beta > self.policy.maximum_market_beta + _WEIGHT_TOLERANCE:
            return "market_beta_factor_risk_limit"
        caps = {item.factor_name: item.maximum_absolute_exposure for item in self.policy.style_caps}
        for factor_name in CANONICAL_STYLE_RISK_FACTORS:
            if abs(styles[factor_name]) > caps[factor_name] + _WEIGHT_TOLERANCE:
                return f"style_factor_risk_limit:{factor_name}"
        return None

    def audit(
        self,
        *,
        selected_security_ids: Sequence[str],
        slot_weight: float,
        rejected_candidates: Sequence[tuple[str, str]],
    ) -> PortfolioFactorRiskAudit:
        """Produce final active/inactive arithmetic for immutable decision audit."""

        selected = tuple(sorted(set(selected_security_ids)))
        if not self.cap_active:
            return PortfolioFactorRiskAudit(
                policy_sha256=self.policy.policy_sha256,
                status=self.status,
                cap_active=False,
                panel_sha256=None,
                slot_weight=slot_weight,
                selected_security_ids=selected,
                portfolio_market_beta=None,
                portfolio_style_exposures=(),
                rejected_candidates=(),
            )
        market_beta, styles = self._aggregate(selected, slot_weight=slot_weight)
        assert self.panel is not None
        audit = PortfolioFactorRiskAudit(
            policy_sha256=self.policy.policy_sha256,
            status=self.status,
            cap_active=True,
            panel_sha256=self.panel.content_sha256,
            slot_weight=slot_weight,
            selected_security_ids=selected,
            portfolio_market_beta=market_beta,
            portfolio_style_exposures=tuple(
                (factor_name, styles[factor_name]) for factor_name in CANONICAL_STYLE_RISK_FACTORS
            ),
            rejected_candidates=tuple(sorted(set(rejected_candidates))),
        )
        if market_beta > self.policy.maximum_market_beta + _WEIGHT_TOLERANCE:
            _invalid("final portfolio exceeds its market-beta cap")
        caps = {item.factor_name: item.maximum_absolute_exposure for item in self.policy.style_caps}
        if any(
            abs(styles[factor_name]) > caps[factor_name] + _WEIGHT_TOLERANCE
            for factor_name in CANONICAL_STYLE_RISK_FACTORS
        ):
            _invalid("final portfolio exceeds a style-risk cap")
        return audit

    def _aggregate(
        self,
        security_ids: Sequence[str],
        *,
        slot_weight: float,
    ) -> tuple[float, dict[str, float]]:
        weight = _finite(slot_weight, field_name="factor-risk slot_weight")
        if not 0.0 <= weight <= 1.0:
            _invalid("factor-risk slot_weight must be in [0, 1]")
        assert self.panel is not None
        exposure_by_security = {item.security_id: item for item in self.panel.exposures}
        missing = sorted(set(security_ids) - set(exposure_by_security))
        if missing:
            _invalid(f"selected portfolio lacks factor-risk evidence: {missing}")
        market_beta = math.fsum(
            exposure_by_security[security_id].market_beta * weight for security_id in security_ids
        )
        style_values = {
            factor_name: math.fsum(
                {
                    item.factor_name: item.standardized_exposure
                    for item in exposure_by_security[security_id].style_exposures
                }[factor_name]
                * weight
                for security_id in security_ids
            )
            for factor_name in CANONICAL_STYLE_RISK_FACTORS
        }
        return market_beta, style_values


def prepare_portfolio_factor_risk_context(
    *,
    policy: PortfolioFactorRiskPolicy,
    panel: EquityFactorRiskPanel | None,
    data_use_scope: DataUseScope,
    cutoff: datetime,
    provider_authority_policy: ProviderAuthorityPolicy,
) -> PortfolioFactorRiskContext:
    """Validate activation, freshness, model identity, and provider authority."""

    if not isinstance(policy, PortfolioFactorRiskPolicy):
        _invalid("factor-risk policy must be typed")
    if not isinstance(data_use_scope, DataUseScope):
        _invalid("factor-risk data_use_scope must be typed")
    if not isinstance(provider_authority_policy, ProviderAuthorityPolicy):
        _invalid("factor-risk provider authority must be typed")
    if provider_authority_policy.data_use_scope is not data_use_scope:
        _invalid("factor-risk provider authority scope differs from the strategy panel")
    decision_cutoff = _aware_utc(cutoff, field_name="factor-risk decision cutoff")
    if policy.model is None:
        if panel is not None:
            _invalid("unconfigured factor-risk policy cannot consume exposure evidence")
        if data_use_scope is not DataUseScope.HISTORICAL_VALIDATION:
            _invalid("paper/live promotion requires a configured point-in-time risk model")
        return PortfolioFactorRiskContext(
            policy=policy,
            status=PortfolioFactorRiskStatus.INACTIVE_HISTORICAL_MODEL_UNCONFIGURED,
            panel=None,
        )
    if panel is None:
        _invalid("configured factor-risk policy requires a complete exposure panel")
    if panel.cutoff != decision_cutoff:
        _invalid("factor-risk panel cutoff differs from the strategy decision")
    if panel.model != policy.model:
        _invalid("factor-risk evidence model differs from the frozen policy")
    oldest = decision_cutoff - timedelta(days=policy.maximum_age_days)
    for exposure in panel.exposures:
        if exposure.observed_at < oldest:
            _invalid("factor-risk evidence is stale at the strategy cutoff")
        if exposure.available_at > decision_cutoff or exposure.observed_at > decision_cutoff:
            _invalid("factor-risk evidence is future-dated at the strategy cutoff")
        if (
            data_use_scope is not DataUseScope.HISTORICAL_VALIDATION
            and exposure.retrieved_at > decision_cutoff
        ):
            _invalid("forward factor-risk evidence was retrieved after the strategy cutoff")
        try:
            provider_authority_policy.require_authorized(
                provider=exposure.model.provider,
                entitlement_scope=exposure.entitlement_scope,
                entitlement_owner_user_id=exposure.entitlement_owner_user_id,
            )
        except ValueError as exc:
            message = "factor-risk evidence is outside panel provider authority"
            raise PortfolioFactorRiskError(message) from exc
    return PortfolioFactorRiskContext(
        policy=policy,
        status=PortfolioFactorRiskStatus.ACTIVE_WITHIN_CAPS,
        panel=panel,
    )


def equity_factor_risk_panel_to_payload(
    panel: EquityFactorRiskPanel | None,
) -> dict[str, object] | None:
    """Encode an optional risk panel without provider-specific response fields."""

    return None if panel is None else panel.identity_payload()


def equity_factor_risk_panel_from_payload(
    raw: Mapping[str, object] | None,
) -> EquityFactorRiskPanel | None:
    """Restore an optional risk panel and re-run every invariant."""

    if raw is None:
        return None
    _require_keys(
        raw,
        expected={"schema", "cutoff", "model", "exposures"},
        field_name="factor-risk panel",
    )
    if raw.get("schema") != FACTOR_RISK_PANEL_SCHEMA:
        _invalid("factor-risk panel schema is incompatible")
    model_raw = raw.get("model")
    if not isinstance(model_raw, Mapping):
        _invalid("factor-risk panel model must be an object")
    model = FactorRiskModelIdentity.from_payload(model_raw)
    exposures_raw = raw.get("exposures")
    if not isinstance(exposures_raw, list):
        _invalid("factor-risk exposures must be an array")
    return EquityFactorRiskPanel(
        cutoff=_parse_timestamp(raw.get("cutoff"), field_name="factor-risk cutoff"),
        model=model,
        exposures=tuple(
            sorted(
                (_exposure_from_payload(item, expected_model=model) for item in exposures_raw),
                key=lambda item: item.security_id,
            )
        ),
    )


def _exposure_from_payload(
    raw: object,
    *,
    expected_model: FactorRiskModelIdentity,
) -> EquityFactorRiskExposure:
    if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
        _invalid("factor-risk exposure must be an object with string keys")
    _require_keys(
        raw,
        expected={
            "instrument_id",
            "security_id",
            "symbol",
            "model",
            "benchmark_security_id",
            "input_manifest_sha256",
            "market_input_sha256",
            "fundamental_input_sha256",
            "membership_sha256",
            "provider_authority_sha256",
            "source_observation_set_sha256",
            "source_observation_count",
            "calculation_sha256",
            "observed_at",
            "available_at",
            "retrieved_at",
            "revision",
            "observation_id",
            "observation_content_sha256",
            "observation_authority_sha256",
            "lineage_id",
            "source_content_sha256",
            "source_product",
            "dataset_version",
            "tool_version",
            "source_revision",
            "timestamp_semantics_sha256",
            "adjustment_policy",
            "missing_data_policy",
            "entitlement_scope",
            "entitlement_owner_user_id",
            "market_beta",
            "raw_descriptors",
            "style_exposures",
        },
        field_name="factor-risk exposure",
    )
    model_raw = raw.get("model")
    if not isinstance(model_raw, Mapping):
        _invalid("factor-risk exposure model must be an object")
    model = FactorRiskModelIdentity.from_payload(model_raw)
    if model != expected_model:
        _invalid("factor-risk exposure model differs from its panel")
    styles_raw = raw.get("style_exposures")
    if not isinstance(styles_raw, list):
        _invalid("factor-risk style exposures must be an array")
    raw_descriptors = raw.get("raw_descriptors")
    if not isinstance(raw_descriptors, Mapping):
        _invalid("factor-risk raw descriptors must be an object")
    if set(raw_descriptors) != set(CANONICAL_STYLE_RISK_FACTORS):
        _invalid("factor-risk raw descriptors have an incompatible field set")
    styles: list[StyleRiskExposure] = []
    for item in styles_raw:
        if not isinstance(item, Mapping) or any(not isinstance(key, str) for key in item):
            _invalid("factor-risk style exposure must be an object")
        _require_keys(
            item,
            expected={"factor_name", "standardized_exposure"},
            field_name="factor-risk style exposure",
        )
        styles.append(
            StyleRiskExposure(
                factor_name=_text(item.get("factor_name"), field_name="style factor_name"),
                standardized_exposure=_finite(
                    item.get("standardized_exposure"),
                    field_name="standardized style exposure",
                ),
            )
        )
    instrument_id = raw.get("instrument_id")
    revision = raw.get("revision")
    source_observation_count = raw.get("source_observation_count")
    if isinstance(instrument_id, bool) or not isinstance(instrument_id, int):
        _invalid("factor-risk instrument_id must be an integer")
    if isinstance(revision, bool) or not isinstance(revision, int):
        _invalid("factor-risk revision must be an integer")
    if isinstance(source_observation_count, bool) or not isinstance(source_observation_count, int):
        _invalid("factor-risk source_observation_count must be an integer")
    owner = raw.get("entitlement_owner_user_id")
    if owner is not None and not isinstance(owner, str):
        _invalid("factor-risk entitlement owner must be text or null")
    return EquityFactorRiskExposure(
        instrument_id=instrument_id,
        security_id=_text(raw.get("security_id"), field_name="factor-risk security_id"),
        symbol=_text(raw.get("symbol"), field_name="factor-risk symbol"),
        model=model,
        benchmark_security_id=_text(
            raw.get("benchmark_security_id"), field_name="benchmark_security_id"
        ),
        input_manifest_sha256=_digest(
            raw.get("input_manifest_sha256"), field_name="input_manifest_sha256"
        ),
        market_input_sha256=_digest(
            raw.get("market_input_sha256"), field_name="market_input_sha256"
        ),
        fundamental_input_sha256=_digest(
            raw.get("fundamental_input_sha256"), field_name="fundamental_input_sha256"
        ),
        membership_sha256=_digest(raw.get("membership_sha256"), field_name="membership_sha256"),
        provider_authority_sha256=_digest(
            raw.get("provider_authority_sha256"), field_name="provider_authority_sha256"
        ),
        source_observation_set_sha256=_digest(
            raw.get("source_observation_set_sha256"),
            field_name="source_observation_set_sha256",
        ),
        source_observation_count=source_observation_count,
        calculation_sha256=_digest(raw.get("calculation_sha256"), field_name="calculation_sha256"),
        observed_at=_parse_timestamp(raw.get("observed_at"), field_name="observed_at"),
        available_at=_parse_timestamp(raw.get("available_at"), field_name="available_at"),
        retrieved_at=_parse_timestamp(raw.get("retrieved_at"), field_name="retrieved_at"),
        revision=revision,
        observation_id=_digest(raw.get("observation_id"), field_name="observation_id"),
        observation_content_sha256=_digest(
            raw.get("observation_content_sha256"), field_name="observation_content_sha256"
        ),
        observation_authority_sha256=_digest(
            raw.get("observation_authority_sha256"), field_name="observation_authority_sha256"
        ),
        lineage_id=_digest(raw.get("lineage_id"), field_name="lineage_id"),
        source_content_sha256=_digest(
            raw.get("source_content_sha256"), field_name="source_content_sha256"
        ),
        source_product=_text(raw.get("source_product"), field_name="source_product"),
        dataset_version=_text(raw.get("dataset_version"), field_name="dataset_version"),
        tool_version=_text(raw.get("tool_version"), field_name="tool_version"),
        source_revision=_text(raw.get("source_revision"), field_name="source_revision"),
        timestamp_semantics_sha256=_digest(
            raw.get("timestamp_semantics_sha256"),
            field_name="timestamp_semantics_sha256",
        ),
        adjustment_policy=_text(raw.get("adjustment_policy"), field_name="adjustment_policy"),
        missing_data_policy=_text(raw.get("missing_data_policy"), field_name="missing_data_policy"),
        entitlement_scope=_text(raw.get("entitlement_scope"), field_name="entitlement_scope"),
        entitlement_owner_user_id=owner,
        market_beta=_finite(raw.get("market_beta"), field_name="market_beta"),
        raw_descriptors=tuple(
            (
                factor_name,
                _finite(raw_descriptors[factor_name], field_name=f"raw {factor_name}"),
            )
            for factor_name in CANONICAL_STYLE_RISK_FACTORS
        ),
        style_exposures=tuple(sorted(styles, key=lambda item: item.factor_name)),
    )


def _parse_timestamp(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        _invalid(f"{field_name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        message = f"{field_name} must be an ISO timestamp"
        raise PortfolioFactorRiskError(message) from exc
    return _aware_utc(parsed, field_name=field_name)


def _require_keys(
    raw: Mapping[str, object],
    *,
    expected: set[str],
    field_name: str,
) -> None:
    if set(raw) != expected:
        _invalid(f"{field_name} has an incompatible field set")


__all__ = [
    "CANONICAL_STYLE_RISK_FACTORS",
    "FACTOR_RISK_AUDIT_SCHEMA",
    "FACTOR_RISK_EXPOSURE_CONTRACT",
    "FACTOR_RISK_OBSERVATION_KIND",
    "FACTOR_RISK_PANEL_SCHEMA",
    "FACTOR_RISK_POLICY_SCHEMA",
    "FACTOR_RISK_TIMESTAMP_SEMANTICS_SCHEMA",
    "EquityFactorRiskExposure",
    "EquityFactorRiskPanel",
    "FactorRiskModelIdentity",
    "PortfolioFactorRiskAudit",
    "PortfolioFactorRiskContext",
    "PortfolioFactorRiskError",
    "PortfolioFactorRiskPolicy",
    "PortfolioFactorRiskStatus",
    "StyleRiskCap",
    "StyleRiskExposure",
    "equity_factor_risk_panel_from_payload",
    "equity_factor_risk_panel_to_payload",
    "prepare_portfolio_factor_risk_context",
]
