"""Append-only persistence boundary for normalized equity source evidence."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    EquityObservation,
    EquityObservationValue,
    EquitySourceLineage,
    User,
)
from lib_common.hashing import canonical_json_hash

_VALUE_TYPES = frozenset({"boolean", "date", "decimal", "integer", "text", "timestamp"})
_SHA256_LENGTH = 64


class EquityObservationWriteError(RuntimeError):
    """Normalized evidence cannot be inserted or replayed safely."""


def _invalid(message: str) -> NoReturn:
    raise EquityObservationWriteError(message)


@dataclass(frozen=True, slots=True)
class EquityObservationValueInput:
    """One exactly typed scalar in an immutable source observation."""

    field_name: str
    value_type: str
    value: Decimal | int | str | bool | date | datetime
    ordinal: int = 0
    unit: str | None = None
    context_identity: str | None = None
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    period_start: date | None = None
    period_end: date | None = None

    def __post_init__(self) -> None:
        _text(self.field_name, field_name="field_name")
        if self.value_type not in _VALUE_TYPES:
            _invalid("equity observation value_type is unsupported")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 0:
            _invalid("equity observation ordinal must be a non-negative integer")
        _validate_scalar(self.value_type, self.value)
        for name in ("unit", "context_identity", "fiscal_period"):
            value = getattr(self, name)
            if value is not None:
                _text(value, field_name=name)
        if (
            self.period_start is not None
            and self.period_end is not None
            and self.period_end < self.period_start
        ):
            _invalid("equity observation period_end cannot precede period_start")

    def payload(self) -> dict[str, object]:
        return {
            "context_identity": self.context_identity,
            "field_name": self.field_name,
            "fiscal_period": self.fiscal_period,
            "fiscal_year": self.fiscal_year,
            "ordinal": self.ordinal,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "unit": self.unit,
            "value": _json_scalar(self.value),
            "value_type": self.value_type,
        }


@dataclass(frozen=True, slots=True)
class EquityObservationSubmission:
    """One source artifact lineage and one normalized record within it."""

    provider: str
    product: str
    endpoint: str
    dataset_version: str
    tool_version: str
    source_identity: str
    source_revision: str
    retrieved_at: datetime
    timestamp_semantics: Mapping[str, object]
    adjustment_policy: str
    entitlement_scope: str
    entitlement_owner_user_id: str | None
    missing_data_policy: str
    artifact_content_sha256: str
    instrument_id: int | None
    observation_kind: str
    source_record_identity: str
    event_at: datetime
    available_at: datetime | None
    disposition: str
    normalized_content_sha256: str
    values: tuple[EquityObservationValueInput, ...]
    revision: int = 1
    supersedes_observation_id: str | None = None
    accession_number: str | None = None
    filing_form: str | None = None
    sic_code: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "provider",
            "product",
            "endpoint",
            "dataset_version",
            "tool_version",
            "source_identity",
            "source_revision",
            "adjustment_policy",
            "entitlement_scope",
            "missing_data_policy",
            "observation_kind",
            "source_record_identity",
            "disposition",
        ):
            _text(getattr(self, name), field_name=name)
        _utc(self.retrieved_at, field_name="retrieved_at")
        event_at = _utc(self.event_at, field_name="event_at")
        if self.available_at is not None:
            available_at = _utc(self.available_at, field_name="available_at")
            if available_at < event_at:
                _invalid("available_at cannot precede event_at")
        elif self.disposition == "observed":
            _invalid("observed evidence requires available_at")
        _sha256(self.artifact_content_sha256, field_name="artifact_content_sha256")
        _sha256(self.normalized_content_sha256, field_name="normalized_content_sha256")
        if self.instrument_id is not None and (
            isinstance(self.instrument_id, bool)
            or not isinstance(self.instrument_id, int)
            or self.instrument_id < 1
        ):
            _invalid("instrument_id must be null or a positive integer")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            _invalid("revision must be a positive integer")
        if self.supersedes_observation_id is not None:
            _sha256(self.supersedes_observation_id, field_name="supersedes_observation_id")
        if not isinstance(self.values, tuple):
            _invalid("values must be an immutable tuple")
        keys = tuple((item.field_name, item.ordinal) for item in self.values)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            _invalid("equity observation values must be unique and canonical")


def persist_equity_observation(
    session: Session,
    submission: EquityObservationSubmission,
) -> EquityObservation:
    """Insert one immutable observation or accept only its exact replay."""

    if not isinstance(submission, EquityObservationSubmission):
        _invalid("submission must be an EquityObservationSubmission")
    owner = submission.entitlement_owner_user_id
    if owner is not None:
        user = session.get(User, owner)
        if user is None or str(user.status) != "active":
            _invalid("entitlement owner must be an active persisted user")
    lineage_values = _lineage_values(submission)
    lineage_id = canonical_json_hash(
        {
            "schema": "equity-source-lineage-v1",
            **_json_mapping(lineage_values),
        }
    )
    lineage = session.get(EquitySourceLineage, lineage_id)
    if lineage is None:
        session.add(EquitySourceLineage(lineage_id=lineage_id, **lineage_values))
        session.flush()
    else:
        _assert_row(lineage, lineage_values, message="equity source lineage replay diverged")

    observation_values = _observation_values(submission, lineage_id=lineage_id)
    observation_id = canonical_json_hash(
        {"schema": "equity-observation-v1", **_observation_identity(observation_values)}
    )
    conflicting = session.scalar(
        select(EquityObservation).where(
            EquityObservation.lineage_id == lineage_id,
            EquityObservation.source_record_identity == submission.source_record_identity,
            EquityObservation.revision == submission.revision,
        )
    )
    if conflicting is not None and str(conflicting.observation_id) != observation_id:
        _invalid("equity source record revision already contains different content")
    existing = session.get(EquityObservation, observation_id)
    if existing is None:
        session.add(EquityObservation(observation_id=observation_id, **observation_values))
        session.flush()
        session.add_all(_value_rows(observation_id, submission.values))
        session.flush()
        return session.get(EquityObservation, observation_id)  # type: ignore[return-value]
    _assert_row(existing, observation_values, message="equity observation replay diverged")
    expected_values = _value_row_payloads(observation_id, submission.values)
    persisted_values = {
        str(row.value_id): row
        for row in session.scalars(
            select(EquityObservationValue).where(
                EquityObservationValue.observation_id == observation_id
            )
        )
    }
    if set(persisted_values) != set(expected_values):
        _invalid("equity observation value replay is incomplete")
    for value_id, expected in expected_values.items():
        _assert_row(
            persisted_values[value_id],
            expected,
            message="equity observation value replay diverged",
        )
    return existing


def _lineage_values(submission: EquityObservationSubmission) -> dict[str, object]:
    return {
        "provider": submission.provider,
        "product": submission.product,
        "endpoint": submission.endpoint,
        "dataset_version": submission.dataset_version,
        "tool_version": submission.tool_version,
        "source_identity": submission.source_identity,
        "source_revision": submission.source_revision,
        "retrieved_at": _utc(submission.retrieved_at, field_name="retrieved_at"),
        "timestamp_semantics": dict(submission.timestamp_semantics),
        "adjustment_policy": submission.adjustment_policy,
        "entitlement_scope": submission.entitlement_scope,
        "entitlement_owner_user_id": submission.entitlement_owner_user_id,
        "missing_data_policy": submission.missing_data_policy,
        "content_sha256": submission.artifact_content_sha256,
    }


def _observation_values(
    submission: EquityObservationSubmission,
    *,
    lineage_id: str,
) -> dict[str, object]:
    return {
        "lineage_id": lineage_id,
        "instr_id": submission.instrument_id,
        "observation_kind": submission.observation_kind,
        "source_record_identity": submission.source_record_identity,
        "event_at": _utc(submission.event_at, field_name="event_at"),
        "available_at": (
            _utc(submission.available_at, field_name="available_at")
            if submission.available_at is not None
            else None
        ),
        "revision": submission.revision,
        "supersedes_observation_id": submission.supersedes_observation_id,
        "accession_number": submission.accession_number,
        "filing_form": submission.filing_form,
        "sic_code": submission.sic_code,
        "disposition": submission.disposition,
        "content_sha256": submission.normalized_content_sha256,
    }


def _observation_identity(values: Mapping[str, object]) -> dict[str, object]:
    return _json_mapping(values)


def _json_mapping(values: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in values.items()
    }


def _value_rows(
    observation_id: str,
    values: tuple[EquityObservationValueInput, ...],
) -> list[EquityObservationValue]:
    return [
        EquityObservationValue(**payload)
        for payload in _value_row_payloads(observation_id, values).values()
    ]


def _value_row_payloads(
    observation_id: str,
    values: tuple[EquityObservationValueInput, ...],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for value in values:
        value_id = canonical_json_hash(
            {
                "schema": "equity-observation-value-v1",
                "observation_id": observation_id,
                **value.payload(),
            }
        )
        columns: dict[str, object] = {
            "value_id": value_id,
            "observation_id": observation_id,
            "field_name": value.field_name,
            "ordinal": value.ordinal,
            "value_type": value.value_type,
            "decimal_value": None,
            "integer_value": None,
            "text_value": None,
            "boolean_value": None,
            "date_value": None,
            "timestamp_value": None,
            "unit": value.unit,
            "context_identity": value.context_identity,
            "fiscal_year": value.fiscal_year,
            "fiscal_period": value.fiscal_period,
            "period_start": value.period_start,
            "period_end": value.period_end,
        }
        columns[f"{value.value_type}_value"] = value.value
        result[value_id] = columns
    return result


def _assert_row(row: object, expected: Mapping[str, object], *, message: str) -> None:
    for name, value in expected.items():
        actual = getattr(row, name)
        if isinstance(value, datetime):
            actual = (
                actual.replace(tzinfo=UTC)
                if isinstance(actual, datetime) and actual.tzinfo is None
                else _utc(actual, field_name=name)
            )
        if actual != value:
            _invalid(message)


def _validate_scalar(value_type: str, value: object) -> None:
    expected = {
        "boolean": bool,
        "date": date,
        "decimal": Decimal,
        "integer": int,
        "text": str,
        "timestamp": datetime,
    }[value_type]
    if isinstance(value, bool) != (value_type == "boolean") or not isinstance(value, expected):
        _invalid("equity observation scalar type is inconsistent")
    if value_type == "date" and isinstance(value, datetime):
        _invalid("date evidence cannot contain a datetime")
    if isinstance(value, Decimal) and not value.is_finite():
        _invalid("decimal evidence must be finite")
    if isinstance(value, datetime):
        _utc(value, field_name="timestamp value")
    if isinstance(value, float) and not math.isfinite(value):
        _invalid("numeric evidence must be finite")


def _json_scalar(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return _utc(value, field_name="timestamp value").isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _invalid(f"{field_name} must be non-blank canonical text")
    return value


def _sha256(value: object, *, field_name: str) -> str:
    text = _text(value, field_name=field_name)
    if len(text) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in text
    ):
        _invalid(f"{field_name} must be a lowercase SHA-256 digest")
    return text


__all__ = [
    "EquityObservationSubmission",
    "EquityObservationValueInput",
    "EquityObservationWriteError",
    "persist_equity_observation",
]
