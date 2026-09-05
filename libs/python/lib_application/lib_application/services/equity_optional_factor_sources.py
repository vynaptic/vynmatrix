"""Cutoff-safe validation for optional equity-factor source observations.

The service reuses the immutable observation, provider-authority, correction,
and factor-evidence boundaries. It is deliberately not a provider client or a
factor calculator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import EquityObservation, EquityObservationValue
from lib_application.services.equity_lineage import validate_equity_observation_authority
from lib_strategy.data_authority import ProviderAuthorityPolicy
from lib_strategy.equity_optional_factors import (
    OptionalFactorContractError,
    OptionalFactorValueType,
    optional_factor_source_contract,
    validate_optional_factor_timestamp_semantics,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_UNPINNED_IDENTITIES = frozenset({"current", "latest", "n/a", "na", "unknown", "unversioned"})


class OptionalFactorEvidenceValidationError(RuntimeError):
    """An optional-factor observation is incomplete or cannot be reproduced."""


@dataclass(frozen=True, slots=True)
class ValidatedOptionalFactorEvidence:
    """Stable identities admitted for an optional factor calculation."""

    sleeve_name: str
    factor_version: str
    source_contract_sha256: str
    observation_id: str
    lineage_id: str
    provider: str
    product: str
    dataset_version: str
    source_revision: str
    entitlement_scope: str
    entitlement_owner_user_id: str | None


def validate_optional_factor_evidence(  # noqa: PLR0912 - explicit evidence gate ledger
    session: Session,
    *,
    sleeve_name: str,
    factor_version: str | None,
    observation_id: str | None,
    cutoff: datetime,
    provider_authority_policy: ProviderAuthorityPolicy,
    expected_instrument_id: int | None,
) -> ValidatedOptionalFactorEvidence:
    """Admit one exact optional-factor source record through existing authority."""

    contract = optional_factor_source_contract(sleeve_name)
    semantic_version = _require_pinned_identity(factor_version, field_name="factor_version")
    if contract.requires_instrument and expected_instrument_id is None:
        _invalid(f"optional factor {contract.sleeve.value!r} requires an instrument identity")
    if not contract.requires_instrument and expected_instrument_id is not None:
        _invalid(f"optional factor {contract.sleeve.value!r} must use a global observation")

    observation, lineage = validate_equity_observation_authority(
        session,
        observation_id=observation_id,
        expected_kind=contract.observation_kind.value,
        cutoff=cutoff,
        provider_authority_policy=provider_authority_policy,
        expected_instrument_id=expected_instrument_id,
    )
    if int(observation.revision) > 1 and observation.supersedes_observation_id is None:
        _invalid("optional-factor corrections require an explicit supersession parent")
    try:
        validate_optional_factor_timestamp_semantics(contract, lineage.timestamp_semantics)
    except OptionalFactorContractError as exc:
        message = f"optional factor {contract.sleeve.value!r} timestamp semantics are invalid"
        raise OptionalFactorEvidenceValidationError(message) from exc

    dataset_version = _require_pinned_identity(
        str(lineage.dataset_version), field_name="dataset_version"
    )
    _require_pinned_identity(str(lineage.tool_version), field_name="tool_version")
    source_revision = _require_pinned_identity(
        str(lineage.source_revision), field_name="source_revision"
    )
    if str(lineage.missing_data_policy) != "fail-closed":
        _invalid("optional-factor sources must use the fail-closed missing-data policy")
    if observation.available_at is None or _utc(observation.available_at) < _utc(
        cutoff
    ) - timedelta(days=contract.activation.maximum_source_age_days):
        _invalid(f"optional factor {contract.sleeve.value!r} evidence is stale at the cutoff")

    rows = tuple(
        session.scalars(
            select(EquityObservationValue)
            .where(EquityObservationValue.observation_id == str(observation.observation_id))
            .order_by(EquityObservationValue.field_name, EquityObservationValue.ordinal)
        )
    )
    by_field: dict[str, list[EquityObservationValue]] = {}
    for row in rows:
        by_field.setdefault(str(row.field_name), []).append(row)
    for requirement in contract.required_values:
        candidates = by_field.get(requirement.field_name, [])
        if not candidates:
            _invalid(
                f"optional factor {contract.sleeve.value!r} lacks required value "
                f"{requirement.field_name!r}"
            )
        if not any(str(row.value_type) == requirement.value_type.value for row in candidates):
            _invalid(
                f"optional factor value {requirement.field_name!r} must use "
                f"{requirement.value_type.value!r}"
            )
        typed = [row for row in candidates if str(row.value_type) == requirement.value_type.value]
        if requirement.value_type is OptionalFactorValueType.TEXT and not any(
            isinstance(row.text_value, str)
            and bool(row.text_value)
            and row.text_value == row.text_value.strip()
            for row in typed
        ):
            _invalid(f"optional factor value {requirement.field_name!r} must be canonical text")
    for field_name in contract.required_digest_fields:
        digest_rows = (
            row
            for row in by_field[field_name]
            if str(row.value_type) == OptionalFactorValueType.TEXT.value
        )
        if not any(
            isinstance(row.text_value, str)
            and _SHA256_PATTERN.fullmatch(row.text_value) is not None
            for row in digest_rows
        ):
            _invalid(f"optional factor digest value {field_name!r} must be lowercase SHA-256")
    if contract.activation.requires_model_transform:
        for field_name in ("model_name", "model_provider", "model_version"):
            values = [
                row.text_value
                for row in by_field[field_name]
                if str(row.value_type) == OptionalFactorValueType.TEXT.value
            ]
            if not any(
                isinstance(value, str)
                and value
                and value == value.strip()
                and value.casefold() not in _UNPINNED_IDENTITIES
                for value in values
            ):
                _invalid(f"optional factor model identity {field_name!r} must be pinned")
    _validate_observation_timestamps(
        observation=observation,
        by_field=by_field,
        event_value_field=contract.event_value_field,
        availability_value_field=contract.availability_value_field,
    )

    return ValidatedOptionalFactorEvidence(
        sleeve_name=contract.sleeve.value,
        factor_version=semantic_version,
        source_contract_sha256=contract.digest,
        observation_id=str(observation.observation_id),
        lineage_id=str(lineage.lineage_id),
        provider=str(lineage.provider),
        product=str(lineage.product),
        dataset_version=dataset_version,
        source_revision=source_revision,
        entitlement_scope=str(lineage.entitlement_scope),
        entitlement_owner_user_id=(
            str(lineage.entitlement_owner_user_id)
            if lineage.entitlement_owner_user_id is not None
            else None
        ),
    )


def _validate_observation_timestamps(
    *,
    observation: EquityObservation,
    by_field: dict[str, list[EquityObservationValue]],
    event_value_field: str,
    availability_value_field: str,
) -> None:
    event_rows = by_field[event_value_field]
    if len(event_rows) != 1 or int(event_rows[0].ordinal) != 0:
        _invalid("optional-factor event timestamp requires one canonical ordinal-zero value")
    event_row = event_rows[0]
    event_at = observation.event_at
    if str(event_row.value_type) == OptionalFactorValueType.TIMESTAMP.value:
        if event_row.timestamp_value is None or _utc(event_row.timestamp_value) != _utc(event_at):
            _invalid("optional-factor normalized event timestamp differs from event_at")
    elif str(event_row.value_type) == OptionalFactorValueType.DATE.value:
        if event_row.date_value is None or event_row.date_value != _utc(event_at).date():
            _invalid("optional-factor normalized event date differs from event_at")
    else:
        _invalid("optional-factor event timestamp has an unsupported value type")

    available_rows = by_field[availability_value_field]
    if len(available_rows) != 1 or int(available_rows[0].ordinal) != 0:
        _invalid("optional-factor availability requires one canonical ordinal-zero value")
    available_at = observation.available_at
    available_value = available_rows[0].timestamp_value
    if (
        available_at is None
        or available_value is None
        or _utc(available_value) != _utc(available_at)
    ):
        _invalid("optional-factor normalized availability differs from available_at")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_pinned_identity(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _invalid(f"optional-factor {field_name} must be a non-blank immutable identity")
    if value.casefold() in _UNPINNED_IDENTITIES:
        _invalid(f"optional-factor {field_name} cannot use an unpinned placeholder")
    return value


def _invalid(message: str) -> NoReturn:
    raise OptionalFactorEvidenceValidationError(message)


__all__ = [
    "OptionalFactorEvidenceValidationError",
    "ValidatedOptionalFactorEvidence",
    "validate_optional_factor_evidence",
]
