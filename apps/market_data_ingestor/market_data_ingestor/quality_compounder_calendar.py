"""Immutable official XNYS calendar persistence for the US Quality Compounder.

The retained NYSE compiler owns source acquisition and schedule compilation.
This module accepts only its completed in-memory artifact contract, validates
the canonical JSON, and persists the evidence and shared calendar in a
caller-owned transaction.  It performs no HTTP or file I/O.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, NoReturn, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import EquityObservation, MarketCalendar, MarketSession
from lib_application.services.equity_observation_writer import (
    EquityObservationSubmission,
    EquityObservationValueInput,
    persist_equity_observation,
)
from lib_common.hashing import canonical_json_bytes, canonical_json_hash

_SCHEMA = "vynmatrix.historical-market-sessions.v1"
_VENUE = "XNYS"
_SOURCE_KIND = "exchange"
_SOURCE_PROVIDER = "Intercontinental Exchange / NYSE Group"
_PERSISTED_PROVIDER = "ice_nyse"
_SOURCE_REFERENCE = "https://www.nyse.com/markets/hours-calendars"
_SOURCE_ENTITLEMENT = "public official exchange publications"
_PERSISTED_ENTITLEMENT = "public-official-exchange-publications"
_DATASET = "ICE/NYSE cash-equity holidays, early closes, and special closures"
_PRODUCT = "official-cash-equity-sessions"
_SOURCE_IDENTITY = "XNYS/regular-session-calendar"
_SOURCE_RECORD_IDENTITY = _SOURCE_REFERENCE
_WRITER_VERSION = "vynmatrix-quality-compounder-calendar-v1"
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_SHA256_LENGTH = 64
QUALITY_COMPOUNDER_CALENDAR_MAX_ARTIFACT_BYTES = _MAX_ARTIFACT_BYTES


class QualityCompounderCalendarError(RuntimeError):
    """Official calendar evidence cannot be validated or replayed exactly."""


def _invalid(message: str) -> NoReturn:
    raise QualityCompounderCalendarError(message)


class CompiledNYSEOfficialSessionArtifactInput(Protocol):
    """Runtime-independent structural view of the retained compiler result."""

    @property
    def content(self) -> bytes: ...

    @property
    def content_sha256(self) -> str: ...

    @property
    def coverage_from(self) -> date: ...

    @property
    def coverage_to(self) -> date: ...

    @property
    def session_count(self) -> int: ...

    @property
    def source_revision(self) -> str: ...

    @property
    def retrieved_at(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class QualityCompounderOfficialSession:
    """One exact official XNYS regular-session interval."""

    session_date: date
    opens_at: datetime
    closes_at: datetime


@dataclass(frozen=True, slots=True)
class QualityCompounderCalendarArtifact:
    """Validated in-memory projection of one retained compiler artifact."""

    content: bytes
    content_sha256: str
    coverage_from: date
    coverage_to: date
    source_revision: str
    retrieved_at: datetime
    source_reference: str
    dataset_version: str
    timestamp_semantics: str
    sessions: tuple[QualityCompounderOfficialSession, ...]


@dataclass(frozen=True, slots=True)
class _LoadedCompiledArtifact:
    content: bytes
    content_sha256: str
    coverage_from: date
    coverage_to: date
    session_count: int
    source_revision: str
    retrieved_at: datetime


def load_quality_compounder_calendar_artifact(
    content: bytes,
    *,
    expected_sha256: str,
) -> QualityCompounderCalendarArtifact:
    """Load exact pinned compiler bytes into the runtime-independent contract."""

    digest = _sha256(expected_sha256, field_name="expected artifact SHA-256")
    if (
        not isinstance(content, bytes)
        or not content
        or len(content) > _MAX_ARTIFACT_BYTES
        or hashlib.sha256(content).hexdigest() != digest
    ):
        _invalid("pinned XNYS artifact bytes differ from the expected SHA-256")
    try:
        decoded = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        message = "pinned XNYS artifact must be valid UTF-8 JSON"
        raise QualityCompounderCalendarError(message) from exc
    root = _mapping(decoded, field_name="pinned XNYS artifact")
    compiled = _LoadedCompiledArtifact(
        content=content,
        content_sha256=digest,
        coverage_from=_iso_date(root.get("coverage_from"), field_name="coverage_from"),
        coverage_to=_iso_date(root.get("coverage_to"), field_name="coverage_to"),
        session_count=len(_sequence(root.get("sessions"), field_name="sessions")),
        source_revision=_sha256(root.get("source_revision"), field_name="source_revision"),
        retrieved_at=_timestamp(root.get("retrieved_at"), field_name="retrieved_at"),
    )
    return parse_quality_compounder_calendar_artifact(compiled)


def parse_quality_compounder_calendar_artifact(
    compiled: CompiledNYSEOfficialSessionArtifactInput,
) -> QualityCompounderCalendarArtifact:
    """Validate canonical compiler bytes without HTTP or filesystem access."""

    content = compiled.content
    if not isinstance(content, bytes) or not content or len(content) > _MAX_ARTIFACT_BYTES:
        _invalid("compiled XNYS artifact is empty or exceeds the byte limit")
    content_sha256 = _sha256(compiled.content_sha256, field_name="artifact content_sha256")
    if hashlib.sha256(content).hexdigest() != content_sha256:
        _invalid("compiled XNYS artifact differs from its content SHA-256")
    try:
        decoded = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        message = "compiled XNYS artifact must be valid UTF-8 JSON"
        raise QualityCompounderCalendarError(message) from exc
    root = _mapping(decoded, field_name="compiled XNYS artifact")
    if canonical_json_bytes(root) != content:
        _invalid("compiled XNYS artifact is not canonical JSON")
    _validate_authority(root)

    coverage_from = _iso_date(root.get("coverage_from"), field_name="coverage_from")
    coverage_to = _iso_date(root.get("coverage_to"), field_name="coverage_to")
    if coverage_to < coverage_from:
        _invalid("compiled XNYS artifact coverage is inverted")
    if (coverage_from, coverage_to) != (compiled.coverage_from, compiled.coverage_to):
        _invalid("compiled XNYS coverage metadata differs from its canonical content")

    source_revision = _sha256(root.get("source_revision"), field_name="source_revision")
    if source_revision != compiled.source_revision:
        _invalid("compiled XNYS source revision differs from its canonical content")
    retrieved_at = _timestamp(root.get("retrieved_at"), field_name="retrieved_at")
    if retrieved_at != _utc(compiled.retrieved_at, field_name="compiled retrieved_at"):
        _invalid("compiled XNYS retrieval timestamp differs from its canonical content")
    dataset_version = _text(root.get("dataset_version"), field_name="dataset_version")
    if dataset_version != f"official-source-set-{source_revision[:16]}":
        _invalid("compiled XNYS dataset version differs from its source revision")
    timestamp_semantics = _text(
        root.get("timestamp_semantics"),
        field_name="timestamp_semantics",
    )
    sessions = _parse_sessions(
        root.get("sessions"),
        coverage_from=coverage_from,
        coverage_to=coverage_to,
    )
    if (
        isinstance(compiled.session_count, bool)
        or not isinstance(compiled.session_count, int)
        or compiled.session_count != len(sessions)
    ):
        _invalid("compiled XNYS session count differs from its canonical content")
    return QualityCompounderCalendarArtifact(
        content=content,
        content_sha256=content_sha256,
        coverage_from=coverage_from,
        coverage_to=coverage_to,
        source_revision=source_revision,
        retrieved_at=retrieved_at,
        source_reference=_SOURCE_REFERENCE,
        dataset_version=dataset_version,
        timestamp_semantics=timestamp_semantics,
        sessions=sessions,
    )


def persist_quality_compounder_calendar(
    session: Session,
    artifact: QualityCompounderCalendarArtifact,
) -> MarketCalendar:
    """Persist one immutable observation and create or exactly replay XNYS."""

    if not isinstance(artifact, QualityCompounderCalendarArtifact):
        _invalid("calendar persistence requires a validated XNYS artifact")
    observation = persist_equity_observation(session, _calendar_submission(artifact))
    calendar = session.scalar(
        select(MarketCalendar).where(MarketCalendar.code == _VENUE).with_for_update()
    )
    coverage_start = datetime.combine(artifact.coverage_from, time.min, tzinfo=UTC)
    coverage_end = datetime.combine(
        artifact.coverage_to + timedelta(days=1),
        time.min,
        tzinfo=UTC,
    )
    if calendar is None:
        calendar = MarketCalendar(
            code=_VENUE,
            source_kind=_SOURCE_KIND,
            provider=_PERSISTED_PROVIDER,
            source_reference=artifact.source_reference,
            observation_id=str(observation.observation_id),
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            observed_at=artifact.retrieved_at,
        )
        session.add(calendar)
        session.flush()
        session.add_all(
            MarketSession(
                calendar_id=int(calendar.calendar_id),
                opens_at=item.opens_at,
                closes_at=item.closes_at,
            )
            for item in artifact.sessions
        )
        session.flush()
        return calendar
    _assert_calendar_replay(
        session,
        calendar=calendar,
        artifact=artifact,
        observation=observation,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
    )
    return calendar


def _validate_authority(root: Mapping[str, Any]) -> None:
    expected: Mapping[str, object] = {
        "coverage_complete": True,
        "dataset": _DATASET,
        "entitlement_scope": _SOURCE_ENTITLEMENT,
        "provider": _SOURCE_PROVIDER,
        "schema": _SCHEMA,
        "source_kind": _SOURCE_KIND,
        "source_reference": _SOURCE_REFERENCE,
        "venue": _VENUE,
    }
    if any(root.get(field) != value for field, value in expected.items()):
        _invalid("compiled XNYS artifact source authority is incompatible")
    evidence = _mapping(root.get("calendar_evidence"), field_name="calendar_evidence")
    crosscheck = _mapping(
        evidence.get("regular_session_cross_check"),
        field_name="regular_session_cross_check",
    )
    if crosscheck.get("library") != "exchange_calendars" or crosscheck.get("mic") != _VENUE:
        _invalid("compiled XNYS regular-session cross-check is incompatible")


def _parse_sessions(
    value: object,
    *,
    coverage_from: date,
    coverage_to: date,
) -> tuple[QualityCompounderOfficialSession, ...]:
    raw_sessions = _sequence(value, field_name="sessions")
    if not raw_sessions:
        _invalid("compiled XNYS artifact contains no sessions")
    sessions: list[QualityCompounderOfficialSession] = []
    previous_date: date | None = None
    previous_close: datetime | None = None
    for index, raw_session in enumerate(raw_sessions):
        row = _mapping(raw_session, field_name=f"session {index}")
        if set(row) != {"session_date", "opens_at", "closes_at"}:
            _invalid("compiled XNYS session row fields are incompatible")
        session_date = _iso_date(row.get("session_date"), field_name=f"session {index} date")
        opens_at = _timestamp(row.get("opens_at"), field_name=f"session {index} opens_at")
        closes_at = _timestamp(row.get("closes_at"), field_name=f"session {index} closes_at")
        if (
            not coverage_from <= session_date <= coverage_to
            or opens_at.date() != session_date
            or closes_at.date() != session_date
            or closes_at <= opens_at
        ):
            _invalid("compiled XNYS session date or interval is inconsistent")
        if (
            previous_date is not None
            and previous_close is not None
            and (session_date <= previous_date or opens_at <= previous_close)
        ):
            _invalid("compiled XNYS sessions overlap or are not strictly ordered")
        sessions.append(
            QualityCompounderOfficialSession(
                session_date=session_date,
                opens_at=opens_at,
                closes_at=closes_at,
            )
        )
        previous_date = session_date
        previous_close = closes_at
    return tuple(sessions)


def _calendar_submission(
    artifact: QualityCompounderCalendarArtifact,
) -> EquityObservationSubmission:
    values = (
        EquityObservationValueInput(
            field_name="artifact_content_sha256",
            value_type="text",
            value=artifact.content_sha256,
        ),
        EquityObservationValueInput(
            field_name="coverage_from",
            value_type="date",
            value=artifact.coverage_from,
        ),
        EquityObservationValueInput(
            field_name="coverage_to",
            value_type="date",
            value=artifact.coverage_to,
        ),
        EquityObservationValueInput(
            field_name="session_count",
            value_type="integer",
            value=len(artifact.sessions),
        ),
        EquityObservationValueInput(
            field_name="source_revision",
            value_type="text",
            value=artifact.source_revision,
        ),
        EquityObservationValueInput(field_name="venue", value_type="text", value=_VENUE),
    )
    normalized_content_sha256 = canonical_json_hash(
        {
            "schema": "quality-compounder-official-calendar-observation-v1",
            "source_record_identity": _SOURCE_RECORD_IDENTITY,
            "values": [item.payload() for item in values],
        }
    )
    return EquityObservationSubmission(
        provider=_PERSISTED_PROVIDER,
        product=_PRODUCT,
        endpoint=artifact.source_reference,
        dataset_version=artifact.dataset_version,
        tool_version=_WRITER_VERSION,
        source_identity=_SOURCE_IDENTITY,
        source_revision=artifact.source_revision,
        retrieved_at=artifact.retrieved_at,
        timestamp_semantics={
            "artifact": artifact.timestamp_semantics,
            "available_at": "exact retained compiler retrieval timestamp",
            "event_at": "exact retained compiler retrieval timestamp",
            "sessions": "official regular-session UTC open and close",
        },
        adjustment_policy="not-applicable",
        entitlement_scope=_PERSISTED_ENTITLEMENT,
        entitlement_owner_user_id=None,
        missing_data_policy="fail-closed-complete-coverage",
        artifact_content_sha256=artifact.content_sha256,
        instrument_id=None,
        observation_kind="calendar",
        source_record_identity=_SOURCE_RECORD_IDENTITY,
        event_at=artifact.retrieved_at,
        available_at=artifact.retrieved_at,
        disposition="observed",
        normalized_content_sha256=normalized_content_sha256,
        values=values,
    )


def _assert_calendar_replay(
    session: Session,
    *,
    calendar: MarketCalendar,
    artifact: QualityCompounderCalendarArtifact,
    observation: EquityObservation,
    coverage_start: datetime,
    coverage_end: datetime,
) -> None:
    expected = (
        _VENUE,
        _SOURCE_KIND,
        _PERSISTED_PROVIDER,
        artifact.source_reference,
        str(observation.observation_id),
        coverage_start,
        coverage_end,
        artifact.retrieved_at,
    )
    actual = (
        str(calendar.code),
        str(calendar.source_kind),
        str(calendar.provider),
        str(calendar.source_reference),
        str(calendar.observation_id or ""),
        _db_utc(calendar.coverage_start),
        _db_utc(calendar.coverage_end),
        _db_utc(calendar.observed_at),
    )
    if actual != expected:
        _invalid("existing XNYS calendar differs from the pinned official artifact")
    rows = tuple(
        session.execute(
            select(MarketSession.opens_at, MarketSession.closes_at)
            .where(MarketSession.calendar_id == int(calendar.calendar_id))
            .order_by(MarketSession.opens_at, MarketSession.closes_at)
        ).all()
    )
    actual_sessions = tuple((_db_utc(opens_at), _db_utc(closes_at)) for opens_at, closes_at in rows)
    expected_sessions = tuple((item.opens_at, item.closes_at) for item in artifact.sessions)
    if actual_sessions != expected_sessions:
        _invalid("existing XNYS sessions differ from the pinned official artifact")


def _mapping(value: object, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        _invalid(f"{field_name} must be an object with string keys")
    return value


def _sequence(value: object, *, field_name: str) -> Sequence[Any]:
    if not isinstance(value, list):
        _invalid(f"{field_name} must be a JSON array")
    return value


def _text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _invalid(f"{field_name} must be canonical non-empty text")
    return value


def _sha256(value: object, *, field_name: str) -> str:
    digest = _text(value, field_name=field_name)
    if len(digest) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in digest
    ):
        _invalid(f"{field_name} must be a lowercase SHA-256 digest")
    return digest


def _iso_date(value: object, *, field_name: str) -> date:
    text_value = _text(value, field_name=field_name)
    try:
        parsed = date.fromisoformat(text_value)
    except ValueError as exc:
        message = f"{field_name} must be an ISO date"
        raise QualityCompounderCalendarError(message) from exc
    if parsed.isoformat() != text_value:
        _invalid(f"{field_name} must be a canonical ISO date")
    return parsed


def _timestamp(value: object, *, field_name: str) -> datetime:
    text_value = _text(value, field_name=field_name)
    try:
        parsed = datetime.fromisoformat(text_value)
    except ValueError as exc:
        message = f"{field_name} must be an ISO timestamp"
        raise QualityCompounderCalendarError(message) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(0)
        or parsed.isoformat() != text_value
    ):
        _invalid(f"{field_name} must be a canonical UTC timestamp")
    return parsed.astimezone(UTC)


def _utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _db_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = [
    "QUALITY_COMPOUNDER_CALENDAR_MAX_ARTIFACT_BYTES",
    "CompiledNYSEOfficialSessionArtifactInput",
    "QualityCompounderCalendarArtifact",
    "QualityCompounderCalendarError",
    "QualityCompounderOfficialSession",
    "load_quality_compounder_calendar_artifact",
    "parse_quality_compounder_calendar_artifact",
    "persist_quality_compounder_calendar",
]
