"""Provider-neutral source contracts for disabled optional equity factors.

These contracts describe evidence that a future provider adapter must persist
before an optional factor can use the existing equity factor-snapshot path.
They do not acquire data, calculate a factor, or authorize a provider.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn

from lib_common.hashing import canonical_json_hash
from lib_strategy.cross_sectional import FactorDirection

OPTIONAL_FACTOR_REGISTRY_VERSION = "sp500-optional-factor-registry-v1"
OPTIONAL_FACTOR_SOURCE_SCHEMA = "equity-optional-factor-source-contract-v2"
OPTIONAL_FACTOR_TIMESTAMP_SCHEMA = "equity-optional-factor-timestamp-semantics-v1"
OPTIONAL_FACTOR_TRANSFORM_SCHEMA = "equity-optional-factor-model-transform-v1"

_FIELD_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_HOLDING_RANGE_LENGTH = 2
_TIMESTAMP_KEYS = frozenset(
    {
        "availability_is_retrieval_time",
        "availability_source_field",
        "available_at",
        "correction_policy",
        "event_at",
        "event_source_field",
        "revision_source_field",
        "schema",
        "source_timezone",
    }
)


class OptionalFactorContractError(ValueError):
    """Raised when optional-factor source semantics are incomplete or ambiguous."""


def _invalid(message: str) -> NoReturn:
    raise OptionalFactorContractError(message)


class OptionalFactorSleeve(StrEnum):
    """Registered but disabled optional sleeves for the equity rotation model."""

    ANALYST_REVISIONS = "analyst_revisions"
    CALL_SENTIMENT = "call_sentiment"
    CROWDING = "crowding"
    INSIDER_ACTIVITY = "insider_activity"
    MACRO = "macro"
    NEWS_SENTIMENT = "news_sentiment"


class OptionalFactorObservationKind(StrEnum):
    """Canonical raw observation classes accepted by optional-factor adapters."""

    ANALYST_ESTIMATE = "analyst_estimate"
    CALL_TRANSCRIPT = "call_transcript"
    INSIDER_TRANSACTION = "insider_transaction"
    MACRO_RELEASE = "macro_release"
    NEWS_EVENT = "news_event"
    POSITIONING = "positioning"


class OptionalFactorEventTimeSemantic(StrEnum):
    """Economic/event timestamp represented by ``EquityObservation.event_at``."""

    ESTIMATE_SNAPSHOT_AS_OF = "estimate_snapshot_as_of"
    CALL_EVENT = "call_event"
    MEASUREMENT_AS_OF = "measurement_as_of"
    ORIGINAL_PUBLICATION = "original_publication"
    REFERENCE_PERIOD = "reference_period"
    TRANSACTION_DATE = "transaction_date"


class OptionalFactorAvailabilitySemantic(StrEnum):
    """First timestamp at which the entitled user could consume the record."""

    FIRST_ENTITLED_PUBLICATION_OR_DELIVERY = "first_entitled_publication_or_delivery"


class OptionalFactorCorrectionPolicy(StrEnum):
    """How corrected records must extend immutable source history."""

    EXPLICIT_APPEND_ONLY_SUPERSESSION = "explicit_append_only_supersession"


class OptionalFactorValueType(StrEnum):
    """Normalized value types shared with ``EquityObservationValue``."""

    BOOLEAN = "boolean"
    DATE = "date"
    DECIMAL = "decimal"
    INTEGER = "integer"
    TEXT = "text"
    TIMESTAMP = "timestamp"


class OptionalFactorApplication(StrEnum):
    """Portfolio layer where a future validated factor is allowed to operate."""

    CROSS_SECTIONAL_RANK = "cross_sectional_rank"
    MARKET_REGIME_OVERLAY = "market_regime_overlay"


class OptionalFactorAggregation(StrEnum):
    """Frozen economic transformation family required before activation."""

    CALL_DELTA = "call_delta"
    CROWDING_RISK = "crowding_risk"
    DECAYED_ENTITY_SENTIMENT = "decayed_entity_sentiment"
    MACRO_REGIME = "macro_regime"
    NET_INSIDER_PURCHASE_INTENSITY = "net_insider_purchase_intensity"
    REVISION_BREADTH_MAGNITUDE = "revision_breadth_magnitude"


class OptionalFactorNormalization(StrEnum):
    """Required comparison axis for a future factor implementation."""

    MARKET_REGIME_CALIBRATION = "market_regime_calibration"
    SECTOR_INDUSTRY_ROBUST_Z = "sector_industry_robust_z"
    SECTOR_ROBUST_Z = "sector_robust_z"


class OptionalFactorMissingPolicy(StrEnum):
    """Fail-closed behavior when an activated factor lacks valid evidence."""

    INELIGIBLE = "instrument_ineligible"
    OVERLAY_FAIL_CLOSED = "overlay_fail_closed"


@dataclass(frozen=True, slots=True)
class OptionalFactorActivationSemantics:
    """Versioned behavior that must be implemented before a sleeve can be enabled."""

    application: OptionalFactorApplication
    direction: FactorDirection
    aggregation: OptionalFactorAggregation
    normalization: OptionalFactorNormalization
    missing_policy: OptionalFactorMissingPolicy
    maximum_source_age_days: int
    signal_half_life_days: int
    expected_holding_sessions: tuple[int, int]
    minimum_evidence_count: int = 1
    requires_model_transform: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.application, OptionalFactorApplication):
            _invalid("optional-factor application must be typed")
        if not isinstance(self.direction, FactorDirection):
            _invalid("optional-factor direction must be typed")
        if not isinstance(self.aggregation, OptionalFactorAggregation):
            _invalid("optional-factor aggregation must be typed")
        if not isinstance(self.normalization, OptionalFactorNormalization):
            _invalid("optional-factor normalization must be typed")
        if not isinstance(self.missing_policy, OptionalFactorMissingPolicy):
            _invalid("optional-factor missing policy must be typed")
        for field_name in (
            "maximum_source_age_days",
            "signal_half_life_days",
            "minimum_evidence_count",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                _invalid(f"optional-factor {field_name} must be a positive integer")
        if (
            not isinstance(self.expected_holding_sessions, tuple)
            or len(self.expected_holding_sessions) != _HOLDING_RANGE_LENGTH
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 1
                for value in self.expected_holding_sessions
            )
            or self.expected_holding_sessions[0] > self.expected_holding_sessions[1]
        ):
            _invalid("optional-factor expected holding sessions must be an ordered range")
        if not isinstance(self.requires_model_transform, bool):
            _invalid("optional-factor model-transform requirement must be boolean")

    def to_payload(self) -> dict[str, object]:
        """Return deterministic activation identity material."""

        return {
            "aggregation": self.aggregation.value,
            "application": self.application.value,
            "direction": self.direction.value,
            "expected_holding_sessions": list(self.expected_holding_sessions),
            "maximum_source_age_days": self.maximum_source_age_days,
            "minimum_evidence_count": self.minimum_evidence_count,
            "missing_policy": self.missing_policy.value,
            "normalization": self.normalization.value,
            "requires_model_transform": self.requires_model_transform,
            "signal_half_life_days": self.signal_half_life_days,
        }


@dataclass(frozen=True, slots=True)
class OptionalFactorRequiredValue:
    """One minimum normalized field required from a source observation."""

    field_name: str
    value_type: OptionalFactorValueType

    def __post_init__(self) -> None:
        if (
            not isinstance(self.field_name, str)
            or _FIELD_PATTERN.fullmatch(self.field_name) is None
        ):
            _invalid("optional-factor value field names must be canonical snake_case")
        if not isinstance(self.value_type, OptionalFactorValueType):
            _invalid("optional-factor value types must be OptionalFactorValueType values")

    def to_payload(self) -> dict[str, str]:
        """Return deterministic identity material."""

        return {"field_name": self.field_name, "value_type": self.value_type.value}


@dataclass(frozen=True, slots=True)
class OptionalFactorSourceContract:
    """Minimum point-in-time source semantics for one optional factor sleeve."""

    sleeve: OptionalFactorSleeve
    observation_kind: OptionalFactorObservationKind
    event_at_semantic: OptionalFactorEventTimeSemantic
    event_value_field: str
    requires_instrument: bool
    required_values: tuple[OptionalFactorRequiredValue, ...]
    activation: OptionalFactorActivationSemantics
    availability_value_field: str = "source_available_at"
    required_digest_fields: tuple[str, ...] = ()
    available_at_semantic: OptionalFactorAvailabilitySemantic = (
        OptionalFactorAvailabilitySemantic.FIRST_ENTITLED_PUBLICATION_OR_DELIVERY
    )
    correction_policy: OptionalFactorCorrectionPolicy = (
        OptionalFactorCorrectionPolicy.EXPLICIT_APPEND_ONLY_SUPERSESSION
    )

    def __post_init__(self) -> None:  # noqa: PLR0912 - explicit immutable contract ledger
        if not isinstance(self.sleeve, OptionalFactorSleeve):
            _invalid("optional-factor sleeve must be an OptionalFactorSleeve")
        if not isinstance(self.observation_kind, OptionalFactorObservationKind):
            _invalid("optional-factor observation kind must be typed")
        if not isinstance(self.event_at_semantic, OptionalFactorEventTimeSemantic):
            _invalid("optional-factor event timestamp semantic must be typed")
        for field_name in (self.event_value_field, self.availability_value_field):
            if _FIELD_PATTERN.fullmatch(field_name) is None:
                _invalid("optional-factor timestamp value fields must be canonical snake_case")
        if not isinstance(self.available_at_semantic, OptionalFactorAvailabilitySemantic):
            _invalid("optional-factor availability semantic must be typed")
        if not isinstance(self.correction_policy, OptionalFactorCorrectionPolicy):
            _invalid("optional-factor correction policy must be typed")
        if not isinstance(self.requires_instrument, bool):
            _invalid("optional-factor instrument scope must be boolean")
        if not isinstance(self.activation, OptionalFactorActivationSemantics):
            _invalid("optional-factor activation semantics must be typed")
        if not isinstance(self.required_values, tuple) or not self.required_values:
            _invalid("optional-factor contracts require normalized source values")
        if not all(isinstance(item, OptionalFactorRequiredValue) for item in self.required_values):
            _invalid("optional-factor required values must be typed")
        field_names = tuple(item.field_name for item in self.required_values)
        if field_names != tuple(sorted(set(field_names))):
            _invalid("optional-factor required values must be unique and canonically ordered")
        if not isinstance(self.required_digest_fields, tuple):
            _invalid("optional-factor digest fields must be an immutable tuple")
        if self.required_digest_fields != tuple(sorted(set(self.required_digest_fields))):
            _invalid("optional-factor digest fields must be unique and canonically ordered")
        if not set(self.required_digest_fields).issubset(field_names):
            _invalid("optional-factor digest fields must also be required values")
        if not {self.event_value_field, self.availability_value_field}.issubset(field_names):
            _invalid("optional-factor timestamps must be retained as normalized values")
        if self.activation.requires_model_transform and not set(
            _MODEL_TRANSFORM_DIGEST_FIELDS
        ).issubset(self.required_digest_fields):
            _invalid("model-transformed factors must bind the complete transform identity")

    @property
    def digest(self) -> str:
        """Return the immutable contract identity."""

        return canonical_json_hash(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        """Return deterministic JSON-safe contract material."""

        return {
            "activation": self.activation.to_payload(),
            "available_at_semantic": self.available_at_semantic.value,
            "availability_value_field": self.availability_value_field,
            "correction_policy": self.correction_policy.value,
            "event_at_semantic": self.event_at_semantic.value,
            "event_value_field": self.event_value_field,
            "observation_kind": self.observation_kind.value,
            "required_digest_fields": list(self.required_digest_fields),
            "required_values": [item.to_payload() for item in self.required_values],
            "requires_instrument": self.requires_instrument,
            "schema": OPTIONAL_FACTOR_SOURCE_SCHEMA,
            "sleeve": self.sleeve.value,
            "transform_schema": (
                OPTIONAL_FACTOR_TRANSFORM_SCHEMA
                if self.activation.requires_model_transform
                else None
            ),
        }


def _value(field_name: str, value_type: OptionalFactorValueType) -> OptionalFactorRequiredValue:
    return OptionalFactorRequiredValue(field_name=field_name, value_type=value_type)


def _values(*items: OptionalFactorRequiredValue) -> tuple[OptionalFactorRequiredValue, ...]:
    return tuple(sorted(items, key=lambda item: item.field_name))


_MODEL_TRANSFORM_DIGEST_FIELDS = (
    "inference_configuration_sha256",
    "model_artifact_sha256",
    "model_input_sha256",
    "model_output_sha256",
    "output_schema_sha256",
    "prompt_or_feature_schema_sha256",
)


def _model_transform_values() -> tuple[OptionalFactorRequiredValue, ...]:
    return (
        _value("inference_configuration_sha256", OptionalFactorValueType.TEXT),
        _value("model_artifact_sha256", OptionalFactorValueType.TEXT),
        _value("model_name", OptionalFactorValueType.TEXT),
        _value("model_output_sha256", OptionalFactorValueType.TEXT),
        _value("model_input_sha256", OptionalFactorValueType.TEXT),
        _value("model_provider", OptionalFactorValueType.TEXT),
        _value("model_version", OptionalFactorValueType.TEXT),
        _value("output_schema_sha256", OptionalFactorValueType.TEXT),
        _value("prompt_or_feature_schema_sha256", OptionalFactorValueType.TEXT),
    )


OPTIONAL_FACTOR_SOURCE_CONTRACTS = (
    OptionalFactorSourceContract(
        sleeve=OptionalFactorSleeve.ANALYST_REVISIONS,
        observation_kind=OptionalFactorObservationKind.ANALYST_ESTIMATE,
        event_at_semantic=OptionalFactorEventTimeSemantic.ESTIMATE_SNAPSHOT_AS_OF,
        event_value_field="estimate_snapshot_as_of",
        requires_instrument=True,
        required_values=_values(
            _value("analyst_count", OptionalFactorValueType.INTEGER),
            _value("consensus_value", OptionalFactorValueType.DECIMAL),
            _value("currency", OptionalFactorValueType.TEXT),
            _value("estimate_snapshot_as_of", OptionalFactorValueType.TIMESTAMP),
            _value("forecast_period_end", OptionalFactorValueType.DATE),
            _value("metric", OptionalFactorValueType.TEXT),
            _value("source_available_at", OptionalFactorValueType.TIMESTAMP),
            _value("split_adjustment_policy", OptionalFactorValueType.TEXT),
            _value("unit", OptionalFactorValueType.TEXT),
        ),
        activation=OptionalFactorActivationSemantics(
            application=OptionalFactorApplication.CROSS_SECTIONAL_RANK,
            direction=FactorDirection.HIGHER_IS_BETTER,
            aggregation=OptionalFactorAggregation.REVISION_BREADTH_MAGNITUDE,
            normalization=OptionalFactorNormalization.SECTOR_INDUSTRY_ROBUST_Z,
            missing_policy=OptionalFactorMissingPolicy.INELIGIBLE,
            maximum_source_age_days=14,
            signal_half_life_days=30,
            expected_holding_sessions=(20, 60),
            minimum_evidence_count=2,
        ),
    ),
    OptionalFactorSourceContract(
        sleeve=OptionalFactorSleeve.CALL_SENTIMENT,
        observation_kind=OptionalFactorObservationKind.CALL_TRANSCRIPT,
        event_at_semantic=OptionalFactorEventTimeSemantic.CALL_EVENT,
        event_value_field="call_event_at",
        requires_instrument=True,
        required_values=_values(
            _value("call_event_at", OptionalFactorValueType.TIMESTAMP),
            _value("language", OptionalFactorValueType.TEXT),
            _value("section", OptionalFactorValueType.TEXT),
            _value("source_available_at", OptionalFactorValueType.TIMESTAMP),
            _value("speaker_role", OptionalFactorValueType.TEXT),
            _value("transcript_coverage", OptionalFactorValueType.TEXT),
            _value("transcript_document_sha256", OptionalFactorValueType.TEXT),
            *_model_transform_values(),
        ),
        required_digest_fields=tuple(
            sorted((*_MODEL_TRANSFORM_DIGEST_FIELDS, "transcript_document_sha256"))
        ),
        activation=OptionalFactorActivationSemantics(
            application=OptionalFactorApplication.CROSS_SECTIONAL_RANK,
            direction=FactorDirection.HIGHER_IS_BETTER,
            aggregation=OptionalFactorAggregation.CALL_DELTA,
            normalization=OptionalFactorNormalization.SECTOR_INDUSTRY_ROBUST_Z,
            missing_policy=OptionalFactorMissingPolicy.INELIGIBLE,
            maximum_source_age_days=120,
            signal_half_life_days=45,
            expected_holding_sessions=(20, 60),
            minimum_evidence_count=2,
            requires_model_transform=True,
        ),
    ),
    OptionalFactorSourceContract(
        sleeve=OptionalFactorSleeve.CROWDING,
        observation_kind=OptionalFactorObservationKind.POSITIONING,
        event_at_semantic=OptionalFactorEventTimeSemantic.MEASUREMENT_AS_OF,
        event_value_field="measurement_as_of",
        requires_instrument=True,
        required_values=_values(
            _value("denominator", OptionalFactorValueType.TEXT),
            _value("measure_name", OptionalFactorValueType.TEXT),
            _value("measure_value", OptionalFactorValueType.DECIMAL),
            _value("measurement_as_of", OptionalFactorValueType.TIMESTAMP),
            _value("methodology_version", OptionalFactorValueType.TEXT),
            _value("source_available_at", OptionalFactorValueType.TIMESTAMP),
            _value("unit", OptionalFactorValueType.TEXT),
            _value("universe_identity", OptionalFactorValueType.TEXT),
        ),
        activation=OptionalFactorActivationSemantics(
            application=OptionalFactorApplication.CROSS_SECTIONAL_RANK,
            direction=FactorDirection.LOWER_IS_BETTER,
            aggregation=OptionalFactorAggregation.CROWDING_RISK,
            normalization=OptionalFactorNormalization.SECTOR_INDUSTRY_ROBUST_Z,
            missing_policy=OptionalFactorMissingPolicy.INELIGIBLE,
            maximum_source_age_days=14,
            signal_half_life_days=30,
            expected_holding_sessions=(20, 60),
        ),
    ),
    OptionalFactorSourceContract(
        sleeve=OptionalFactorSleeve.INSIDER_ACTIVITY,
        observation_kind=OptionalFactorObservationKind.INSIDER_TRANSACTION,
        event_at_semantic=OptionalFactorEventTimeSemantic.TRANSACTION_DATE,
        event_value_field="transaction_date",
        requires_instrument=True,
        required_values=_values(
            _value("accession_number", OptionalFactorValueType.TEXT),
            _value("amendment_indicator", OptionalFactorValueType.BOOLEAN),
            _value("derivative_indicator", OptionalFactorValueType.BOOLEAN),
            _value("ownership_nature", OptionalFactorValueType.TEXT),
            _value("reporting_owner_identity", OptionalFactorValueType.TEXT),
            _value("security_type", OptionalFactorValueType.TEXT),
            _value("source_available_at", OptionalFactorValueType.TIMESTAMP),
            _value("transaction_code", OptionalFactorValueType.TEXT),
            _value("transaction_date", OptionalFactorValueType.DATE),
            _value("transaction_price", OptionalFactorValueType.DECIMAL),
            _value("transaction_shares", OptionalFactorValueType.DECIMAL),
        ),
        activation=OptionalFactorActivationSemantics(
            application=OptionalFactorApplication.CROSS_SECTIONAL_RANK,
            direction=FactorDirection.HIGHER_IS_BETTER,
            aggregation=OptionalFactorAggregation.NET_INSIDER_PURCHASE_INTENSITY,
            normalization=OptionalFactorNormalization.SECTOR_ROBUST_Z,
            missing_policy=OptionalFactorMissingPolicy.INELIGIBLE,
            maximum_source_age_days=14,
            signal_half_life_days=45,
            expected_holding_sessions=(20, 60),
        ),
    ),
    OptionalFactorSourceContract(
        sleeve=OptionalFactorSleeve.MACRO,
        observation_kind=OptionalFactorObservationKind.MACRO_RELEASE,
        event_at_semantic=OptionalFactorEventTimeSemantic.REFERENCE_PERIOD,
        event_value_field="reference_period",
        requires_instrument=False,
        required_values=_values(
            _value("observed_value", OptionalFactorValueType.DECIMAL),
            _value("reference_period", OptionalFactorValueType.DATE),
            _value("release_vintage", OptionalFactorValueType.TIMESTAMP),
            _value("series_identity", OptionalFactorValueType.TEXT),
            _value("source_available_at", OptionalFactorValueType.TIMESTAMP),
            _value("unit", OptionalFactorValueType.TEXT),
        ),
        activation=OptionalFactorActivationSemantics(
            application=OptionalFactorApplication.MARKET_REGIME_OVERLAY,
            direction=FactorDirection.HIGHER_IS_BETTER,
            aggregation=OptionalFactorAggregation.MACRO_REGIME,
            normalization=OptionalFactorNormalization.MARKET_REGIME_CALIBRATION,
            missing_policy=OptionalFactorMissingPolicy.OVERLAY_FAIL_CLOSED,
            maximum_source_age_days=45,
            signal_half_life_days=30,
            expected_holding_sessions=(20, 60),
        ),
    ),
    OptionalFactorSourceContract(
        sleeve=OptionalFactorSleeve.NEWS_SENTIMENT,
        observation_kind=OptionalFactorObservationKind.NEWS_EVENT,
        event_at_semantic=OptionalFactorEventTimeSemantic.ORIGINAL_PUBLICATION,
        event_value_field="original_published_at",
        requires_instrument=True,
        required_values=_values(
            _value("document_type", OptionalFactorValueType.TEXT),
            _value("entity_relevance", OptionalFactorValueType.DECIMAL),
            _value("language", OptionalFactorValueType.TEXT),
            _value("original_published_at", OptionalFactorValueType.TIMESTAMP),
            _value("source_available_at", OptionalFactorValueType.TIMESTAMP),
            *_model_transform_values(),
        ),
        required_digest_fields=_MODEL_TRANSFORM_DIGEST_FIELDS,
        activation=OptionalFactorActivationSemantics(
            application=OptionalFactorApplication.CROSS_SECTIONAL_RANK,
            direction=FactorDirection.HIGHER_IS_BETTER,
            aggregation=OptionalFactorAggregation.DECAYED_ENTITY_SENTIMENT,
            normalization=OptionalFactorNormalization.SECTOR_ROBUST_Z,
            missing_policy=OptionalFactorMissingPolicy.INELIGIBLE,
            maximum_source_age_days=7,
            signal_half_life_days=3,
            expected_holding_sessions=(5, 20),
            requires_model_transform=True,
        ),
    ),
)

OPTIONAL_FACTOR_SLEEVES = tuple(
    contract.sleeve.value for contract in OPTIONAL_FACTOR_SOURCE_CONTRACTS
)
SP500_V1_DISABLED_OPTIONAL_SLEEVES = (
    "analyst_revisions",
    "call_sentiment",
    "crowding",
    "insider_activity",
    "macro",
    "news_sentiment",
)
if OPTIONAL_FACTOR_SLEEVES != SP500_V1_DISABLED_OPTIONAL_SLEEVES:
    _invalid("the frozen S&P 500 v1 optional-factor registry has drifted")


def optional_factor_source_contract(
    sleeve: str | OptionalFactorSleeve,
) -> OptionalFactorSourceContract:
    """Resolve one registered optional sleeve or fail closed."""

    try:
        typed_sleeve = OptionalFactorSleeve(sleeve)
    except (TypeError, ValueError) as exc:
        message = f"optional factor sleeve {sleeve!r} is not registered"
        raise OptionalFactorContractError(message) from exc
    for contract in OPTIONAL_FACTOR_SOURCE_CONTRACTS:
        if contract.sleeve is typed_sleeve:
            return contract
    return _invalid(f"optional factor sleeve {typed_sleeve.value!r} has no source contract")


def validate_optional_factor_timestamp_semantics(
    contract: OptionalFactorSourceContract,
    payload: object,
) -> None:
    """Validate persisted provider-field mappings for event and availability time."""

    if not isinstance(contract, OptionalFactorSourceContract):
        _invalid("optional-factor source contract is invalid")
    if not isinstance(payload, Mapping):
        _invalid("optional-factor timestamp semantics must be an object")
    if set(payload) != _TIMESTAMP_KEYS:
        _invalid(
            f"optional-factor timestamp semantic keys must be exactly {sorted(_TIMESTAMP_KEYS)!r}"
        )
    if payload.get("schema") != OPTIONAL_FACTOR_TIMESTAMP_SCHEMA:
        _invalid("optional-factor timestamp semantics schema is unsupported")
    if payload.get("event_at") != contract.event_at_semantic.value:
        _invalid("optional-factor event_at semantic does not match its source contract")
    if payload.get("available_at") != contract.available_at_semantic.value:
        _invalid("optional-factor available_at semantic does not match its source contract")
    if payload.get("correction_policy") != contract.correction_policy.value:
        _invalid("optional-factor correction policy does not match its source contract")
    if payload.get("availability_is_retrieval_time") is not False:
        _invalid("retrieval time cannot substitute for historical source availability")
    for field_name in (
        "availability_source_field",
        "event_source_field",
        "revision_source_field",
        "source_timezone",
    ):
        value = payload.get(field_name)
        if not isinstance(value, str) or not value or value != value.strip():
            _invalid(f"optional-factor timestamp semantic {field_name!r} must be non-blank")


def optional_factor_source_registry_sha256() -> str:
    """Return one identity for the complete registered optional source boundary."""

    return canonical_json_hash(
        {
            "contracts": [contract.to_payload() for contract in OPTIONAL_FACTOR_SOURCE_CONTRACTS],
            "registry_version": OPTIONAL_FACTOR_REGISTRY_VERSION,
            "schema": OPTIONAL_FACTOR_SOURCE_SCHEMA,
        }
    )


__all__ = [
    "OPTIONAL_FACTOR_REGISTRY_VERSION",
    "OPTIONAL_FACTOR_SLEEVES",
    "OPTIONAL_FACTOR_SOURCE_CONTRACTS",
    "OPTIONAL_FACTOR_SOURCE_SCHEMA",
    "OPTIONAL_FACTOR_TIMESTAMP_SCHEMA",
    "OPTIONAL_FACTOR_TRANSFORM_SCHEMA",
    "SP500_V1_DISABLED_OPTIONAL_SLEEVES",
    "OptionalFactorActivationSemantics",
    "OptionalFactorAggregation",
    "OptionalFactorApplication",
    "OptionalFactorAvailabilitySemantic",
    "OptionalFactorContractError",
    "OptionalFactorCorrectionPolicy",
    "OptionalFactorEventTimeSemantic",
    "OptionalFactorMissingPolicy",
    "OptionalFactorNormalization",
    "OptionalFactorObservationKind",
    "OptionalFactorRequiredValue",
    "OptionalFactorSleeve",
    "OptionalFactorSourceContract",
    "OptionalFactorValueType",
    "optional_factor_source_contract",
    "optional_factor_source_registry_sha256",
    "validate_optional_factor_timestamp_semantics",
]
