"""Self-hashed strategy source and correctness attestations.

The attestation binds a reviewed strategy to exact source, package-source, and
fixture bytes.  Semantic findings remain distinct from evidence-integrity
findings: an unresolved strategy behavior may be carried forward for a campaign
decision, but an unresolved evidence-integrity finding makes the attestation
invalid and therefore unusable as frozen evidence.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from dev_cli.validation.evidence import file_sha256 as _file_sha256
from dev_cli.validation.evidence import parse_utc_datetime, utc_iso
from dev_cli.validation.persistence.backtest_manifest_store import (
    canonical_manifest_bytes,
    manifest_sha256,
    validate_sha256_digest,
)

_ATTESTATION_SCHEMA_ID = "vynmatrix.strategy-correctness-attestation"
_ATTESTATION_SCHEMA_VERSION = 1
_ATTESTATION_STATUS = "verified_no_unresolved_evidence_integrity"
_CONTRACT_SCHEMA_ID = "vynmatrix.strategy-correctness-contract"
_CONTRACT_SCHEMA_VERSION = 1
_FROZEN_CONTRACT_STATUS = "frozen_pre_outcome_source_reverification"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ASCII_CONTROL_BOUNDARY = 32
_ASCII_DELETE = 127
_MAX_EMBEDDED_FILE_BYTES = 1_048_576
_MAX_EMBEDDED_TOTAL_BYTES = 4_194_304
_REQUIRED_FILE_CLASSES = frozenset(
    {
        "strategy_source",
        "package_source",
        "fixture",
    }
)


class CorrectnessFileClass(str, Enum):
    """Role played by one exact file in correctness evidence."""

    STRATEGY_SOURCE = "strategy_source"
    PACKAGE_SOURCE = "package_source"
    FIXTURE = "fixture"


class CorrectnessPathScope(str, Enum):
    """How a CLI must resolve a contract file before building an attestation."""

    REPOSITORY_RELATIVE = "repository_relative"
    EXPLICIT_ABSOLUTE_EXTERNAL = "explicit_absolute_external"


class CorrectnessContentClassification(str, Enum):
    """Explicitly non-sensitive content classes eligible for embedding."""

    NON_SENSITIVE_SOURCE = "non_sensitive_source"
    NON_SENSITIVE_FIXTURE = "non_sensitive_fixture"


class CorrectnessFindingSeverity(str, Enum):
    """Pre-outcome materiality assigned to a correctness finding."""

    INFORMATIONAL = "informational"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class CorrectnessFindingStatus(str, Enum):
    """Review state of a semantic or evidence-integrity finding."""

    VERIFIED_EQUIVALENT = "verified_equivalent"
    ACCEPTED_DEVIATION = "accepted_deviation"
    REMEDIATION_REQUIRED = "remediation_required"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class CorrectnessFindingClass(str, Enum):
    """Separates strategy behavior from trustworthiness of its evidence."""

    STRATEGY_SEMANTIC = "strategy_semantic"
    EVIDENCE_INTEGRITY = "evidence_integrity"


@dataclass(frozen=True, slots=True)
class CorrectnessFileBinding:
    """One declared evidence file whose exact bytes are hashed by the builder."""

    binding_id: str
    file_class: CorrectnessFileClass
    content_classification: CorrectnessContentClassification
    path: Path
    location: str | None = None


@dataclass(frozen=True, slots=True)
class CorrectnessSemanticFinding:
    """A typed, evidence-linked statement about reviewed strategy behavior."""

    finding_id: str
    authority: str
    current_behavior: str
    authoritative_behavior: str
    severity: CorrectnessFindingSeverity
    status: CorrectnessFindingStatus
    finding_class: CorrectnessFindingClass
    remediation: str
    evidence_file_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CorrectnessReviewer:
    """Identity and time of the human or controlled review function."""

    identity: str
    role: str
    independent: bool
    reviewed_at_utc: datetime


@dataclass(frozen=True, slots=True)
class RegisteredCorrectnessAttestationDecision:
    """Deterministic result of reconciling an attestation with its contract."""

    strategy_id: str
    strategy_version: str
    attestation_sha256: str
    contract_status: str
    file_ids: tuple[str, ...]
    file_count: int
    finding_count: int
    strategy_semantic_finding_count: int
    evidence_integrity_finding_count: int
    remediation_required_finding_ids: tuple[str, ...]
    accepted_deviation_finding_ids: tuple[str, ...]
    resolved_evidence_integrity_finding_ids: tuple[str, ...]
    unresolved_evidence_integrity_finding_count: int
    reviewer_independent: bool

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON-safe representation used by campaign manifests."""

        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "attestation_sha256": self.attestation_sha256,
            "contract_status": self.contract_status,
            "file_ids": list(self.file_ids),
            "file_count": self.file_count,
            "finding_count": self.finding_count,
            "strategy_semantic_finding_count": self.strategy_semantic_finding_count,
            "evidence_integrity_finding_count": self.evidence_integrity_finding_count,
            "remediation_required_finding_ids": list(self.remediation_required_finding_ids),
            "accepted_deviation_finding_ids": list(self.accepted_deviation_finding_ids),
            "resolved_evidence_integrity_finding_ids": list(
                self.resolved_evidence_integrity_finding_ids
            ),
            "unresolved_evidence_integrity_finding_count": (
                self.unresolved_evidence_integrity_finding_count
            ),
            "reviewer_independent": self.reviewer_independent,
        }


def _safe_identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        message = (
            f"{field} must be a non-empty identifier containing only letters, numbers, "
            "periods, underscores, colons, or hyphens"
        )
        raise ValueError(message)
    return value


def _safe_text(value: object, *, field: str, maximum_length: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        message = f"{field} must be a non-blank string"
        raise ValueError(message)
    normalized = value.strip()
    if value != normalized:
        message = f"{field} must not contain surrounding whitespace"
        raise ValueError(message)
    if len(normalized) > maximum_length:
        message = f"{field} exceeds {maximum_length} characters"
        raise ValueError(message)
    if any(
        ord(character) < _ASCII_CONTROL_BOUNDARY or ord(character) == _ASCII_DELETE
        for character in normalized
    ):
        message = f"{field} contains control characters"
        raise ValueError(message)
    return normalized


def _utc_iso(value: object, *, field: str) -> str:
    if not isinstance(value, datetime):
        message = f"{field} must be a datetime"
        raise TypeError(message)
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None:
        message = f"{field} must be timezone-aware"
        raise ValueError(message)
    if offset.total_seconds() != 0:
        message = f"{field} must use UTC"
        raise ValueError(message)
    return utc_iso(value, field=field)


def _parse_utc_iso(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        message = f"{field} must be a non-blank UTC timestamp"
        raise ValueError(message)
    parsed = parse_utc_datetime(value, field=field)
    normalized = _utc_iso(parsed, field=field)
    if value != normalized:
        message = f"{field} must use canonical UTC Z notation"
        raise ValueError(message)
    return normalized


def _enum_value(value: object, enum_type: type[Enum], *, field: str) -> str:
    if not isinstance(value, enum_type):
        allowed = ", ".join(str(item.value) for item in enum_type)
        message = f"{field} must be one of: {allowed}"
        raise TypeError(message)
    result = value.value
    if not isinstance(result, str):  # pragma: no cover - every public enum is string-valued
        message = f"{field} enum must have string values"
        raise TypeError(message)
    return result


def _parse_enum(value: object, enum_type: type[Enum], *, field: str) -> str:
    if not isinstance(value, str):
        message = f"{field} must be a string"
        raise TypeError(message)
    try:
        parsed = enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(str(item.value) for item in enum_type)
        message = f"{field} must be one of: {allowed}"
        raise ValueError(message) from exc
    result = parsed.value
    if not isinstance(result, str):  # pragma: no cover - every public enum is string-valued
        message = f"{field} enum must have string values"
        raise TypeError(message)
    return result


def _exact_keys(value: Mapping[str, object], expected: set[str], *, field: str) -> None:
    observed = set(value)
    if observed != expected:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        message = f"{field} fields differ (missing={missing}, extra={extra})"
        raise ValueError(message)


def _mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        message = f"{field} must be an object"
        raise TypeError(message)
    if any(not isinstance(key, str) for key in value):
        message = f"{field} keys must be strings"
        raise TypeError(message)
    return value


def _sequence(value: object, *, field: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        message = f"{field} must be an array"
        raise TypeError(message)
    return value


def _require_file_class_coverage(file_classes: set[str]) -> None:
    if file_classes != _REQUIRED_FILE_CLASSES:
        missing = sorted(_REQUIRED_FILE_CLASSES - file_classes)
        extra = sorted(file_classes - _REQUIRED_FILE_CLASSES)
        message = (
            "correctness attestation must bind strategy source, package source, and fixture "
            f"files exactly (missing={missing}, extra={extra})"
        )
        raise ValueError(message)


def _require_integrity_resolution(
    *,
    finding_id: str,
    finding_class: str,
    status: str,
) -> None:
    if finding_class != CorrectnessFindingClass.EVIDENCE_INTEGRITY.value:
        return
    resolved_statuses = {
        CorrectnessFindingStatus.RESOLVED.value,
        CorrectnessFindingStatus.VERIFIED_EQUIVALENT.value,
    }
    if status not in resolved_statuses:
        message = (
            f"evidence-integrity finding {finding_id} is unresolved; only resolved or "
            "verified-equivalent evidence can be attested"
        )
        raise ValueError(message)


def _file_row(binding: CorrectnessFileBinding) -> dict[str, object]:
    binding_id = _safe_identifier(binding.binding_id, field="files[].id")
    file_class = _enum_value(
        binding.file_class,
        CorrectnessFileClass,
        field=f"files[{binding_id}].class",
    )
    content_classification = _enum_value(
        binding.content_classification,
        CorrectnessContentClassification,
        field=f"files[{binding_id}].content_classification",
    )
    if not isinstance(binding.path, Path):
        message = f"files[{binding_id}].path must be a pathlib.Path"
        raise TypeError(message)
    if not binding.path.is_file():
        message = f"attested file does not exist: {binding.path}"
        raise FileNotFoundError(message)
    byte_count = binding.path.stat().st_size
    if byte_count <= 0:
        message = f"attested file must not be empty: {binding.path}"
        raise ValueError(message)
    if byte_count > _MAX_EMBEDDED_FILE_BYTES:
        message = (
            f"attested file exceeds the {_MAX_EMBEDDED_FILE_BYTES}-byte embedding limit: "
            f"{binding.path}"
        )
        raise ValueError(message)
    content = binding.path.read_bytes()
    location_value = binding.location if binding.location is not None else binding.path.as_posix()
    location = _safe_text(
        location_value,
        field=f"files[{binding_id}].location",
        maximum_length=2048,
    )
    return {
        "id": binding_id,
        "class": file_class,
        "content_classification": content_classification,
        "location": location,
        "sha256": hashlib.sha256(content).hexdigest(),
        "byte_count": byte_count,
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


def _finding_row(
    finding: CorrectnessSemanticFinding,
    *,
    file_ids: set[str],
) -> dict[str, object]:
    finding_id = _safe_identifier(finding.finding_id, field="findings[].id")
    severity = _enum_value(
        finding.severity,
        CorrectnessFindingSeverity,
        field=f"findings[{finding_id}].severity",
    )
    status = _enum_value(
        finding.status,
        CorrectnessFindingStatus,
        field=f"findings[{finding_id}].status",
    )
    finding_class = _enum_value(
        finding.finding_class,
        CorrectnessFindingClass,
        field=f"findings[{finding_id}].finding_class",
    )
    evidence_file_ids = tuple(
        sorted(
            _safe_identifier(value, field=f"findings[{finding_id}].evidence_file_ids[]")
            for value in finding.evidence_file_ids
        )
    )
    if not evidence_file_ids:
        message = f"findings[{finding_id}].evidence_file_ids must not be empty"
        raise ValueError(message)
    if len(set(evidence_file_ids)) != len(evidence_file_ids):
        message = f"findings[{finding_id}].evidence_file_ids must be unique"
        raise ValueError(message)
    unknown_files = sorted(set(evidence_file_ids) - file_ids)
    if unknown_files:
        message = f"findings[{finding_id}] references unknown evidence files: {unknown_files}"
        raise ValueError(message)
    _require_integrity_resolution(
        finding_id=finding_id,
        finding_class=finding_class,
        status=status,
    )
    return {
        "id": finding_id,
        "authority": _safe_text(
            finding.authority,
            field=f"findings[{finding_id}].authority",
        ),
        "current_behavior": _safe_text(
            finding.current_behavior,
            field=f"findings[{finding_id}].current_behavior",
        ),
        "authoritative_behavior": _safe_text(
            finding.authoritative_behavior,
            field=f"findings[{finding_id}].authoritative_behavior",
        ),
        "severity": severity,
        "status": status,
        "finding_class": finding_class,
        "remediation": _safe_text(
            finding.remediation,
            field=f"findings[{finding_id}].remediation",
        ),
        "evidence_file_ids": list(evidence_file_ids),
    }


def build_strategy_correctness_attestation(
    *,
    strategy_id: str,
    strategy_version: str,
    files: Sequence[CorrectnessFileBinding],
    findings: Sequence[CorrectnessSemanticFinding],
    reviewer: CorrectnessReviewer,
) -> dict[str, object]:
    """Build a canonical self-hashed correctness attestation from exact files."""

    normalized_strategy_id = _safe_identifier(strategy_id, field="subject.strategy_id")
    normalized_version = _safe_text(
        strategy_version,
        field="subject.strategy_version",
        maximum_length=128,
    )
    file_rows = tuple(
        sorted(
            (_file_row(binding) for binding in files),
            key=lambda row: str(row["id"]),
        )
    )
    if not file_rows:
        message = "correctness attestation files must not be empty"
        raise ValueError(message)
    file_ids = tuple(str(row["id"]) for row in file_rows)
    if len(set(file_ids)) != len(file_ids):
        message = "correctness attestation file IDs must be unique"
        raise ValueError(message)
    _require_file_class_coverage({str(row["class"]) for row in file_rows})
    total_embedded_bytes = 0
    for row in file_rows:
        byte_count = row["byte_count"]
        assert isinstance(byte_count, int)
        total_embedded_bytes += byte_count
    if total_embedded_bytes > _MAX_EMBEDDED_TOTAL_BYTES:
        message = (
            "correctness attestation files exceed the aggregate "
            f"{_MAX_EMBEDDED_TOTAL_BYTES}-byte embedding limit"
        )
        raise ValueError(message)

    finding_rows = tuple(
        sorted(
            (_finding_row(finding, file_ids=set(file_ids)) for finding in findings),
            key=lambda row: str(row["id"]),
        )
    )
    if not finding_rows:
        message = "correctness attestation findings must not be empty"
        raise ValueError(message)
    finding_ids = tuple(str(row["id"]) for row in finding_rows)
    if len(set(finding_ids)) != len(finding_ids):
        message = "correctness attestation finding IDs must be unique"
        raise ValueError(message)
    if not isinstance(reviewer, CorrectnessReviewer):
        message = "reviewer must be CorrectnessReviewer"
        raise TypeError(message)
    if not isinstance(reviewer.independent, bool):
        message = "reviewer.independent must be a bool"
        raise TypeError(message)

    unsigned: dict[str, object] = {
        "schema_id": _ATTESTATION_SCHEMA_ID,
        "schema_version": _ATTESTATION_SCHEMA_VERSION,
        "attestation_status": _ATTESTATION_STATUS,
        "subject": {
            "strategy_id": normalized_strategy_id,
            "strategy_version": normalized_version,
        },
        "files": [dict(row) for row in file_rows],
        "findings": [dict(row) for row in finding_rows],
        "reviewer": {
            "identity": _safe_text(
                reviewer.identity,
                field="reviewer.identity",
                maximum_length=256,
            ),
            "role": _safe_text(reviewer.role, field="reviewer.role", maximum_length=256),
            "independent": reviewer.independent,
            "reviewed_at_utc": _utc_iso(
                reviewer.reviewed_at_utc,
                field="reviewer.reviewed_at_utc",
            ),
        },
    }
    payload = dict(unsigned)
    payload["attestation_sha256"] = manifest_sha256(unsigned)
    verify_strategy_correctness_attestation(
        payload,
        files={binding.binding_id: binding.path for binding in files},
    )
    return payload


def _verify_attestation_header(
    payload: Mapping[str, object],
) -> Mapping[str, object]:
    artifact = _mapping(payload, field="attestation")
    _exact_keys(
        artifact,
        {
            "schema_id",
            "schema_version",
            "attestation_status",
            "subject",
            "files",
            "findings",
            "reviewer",
            "attestation_sha256",
        },
        field="attestation",
    )
    observed_hash = artifact.get("attestation_sha256")
    if not isinstance(observed_hash, str):
        message = "attestation_sha256 must be a string"
        raise TypeError(message)
    validate_sha256_digest(observed_hash, field="attestation_sha256")
    unsigned = dict(artifact)
    del unsigned["attestation_sha256"]
    if manifest_sha256(unsigned) != observed_hash:
        message = "strategy correctness attestation content hash differs"
        raise ValueError(message)
    if artifact.get("schema_id") != _ATTESTATION_SCHEMA_ID:
        message = "strategy correctness attestation schema_id is unsupported"
        raise ValueError(message)
    if artifact.get("schema_version") != _ATTESTATION_SCHEMA_VERSION:
        message = "strategy correctness attestation schema_version is unsupported"
        raise ValueError(message)
    if artifact.get("attestation_status") != _ATTESTATION_STATUS:
        message = "strategy correctness attestation status is invalid"
        raise ValueError(message)
    return artifact


def _verify_subject(value: object) -> None:
    subject = _mapping(value, field="subject")
    _exact_keys(subject, {"strategy_id", "strategy_version"}, field="subject")
    _safe_identifier(subject.get("strategy_id"), field="subject.strategy_id")
    _safe_text(
        subject.get("strategy_version"),
        field="subject.strategy_version",
        maximum_length=128,
    )


def _verify_file_row(raw_row: object, *, index: int) -> tuple[str, str, str, int]:
    row = _mapping(raw_row, field=f"files[{index}]")
    _exact_keys(
        row,
        {
            "id",
            "class",
            "content_classification",
            "location",
            "sha256",
            "byte_count",
            "content_base64",
        },
        field=f"files[{index}]",
    )
    binding_id = _safe_identifier(row.get("id"), field=f"files[{index}].id")
    file_class = _parse_enum(
        row.get("class"),
        CorrectnessFileClass,
        field=f"files[{binding_id}].class",
    )
    content_classification = _parse_enum(
        row.get("content_classification"),
        CorrectnessContentClassification,
        field=f"files[{binding_id}].content_classification",
    )
    expected_content_classification = (
        CorrectnessContentClassification.NON_SENSITIVE_FIXTURE.value
        if file_class == CorrectnessFileClass.FIXTURE.value
        else CorrectnessContentClassification.NON_SENSITIVE_SOURCE.value
    )
    if content_classification != expected_content_classification:
        message = f"files[{binding_id}] content classification conflicts with its file class"
        raise ValueError(message)
    _safe_text(
        row.get("location"),
        field=f"files[{binding_id}].location",
        maximum_length=2048,
    )
    digest = row.get("sha256")
    if not isinstance(digest, str):
        message = f"files[{binding_id}].sha256 must be a string"
        raise TypeError(message)
    validate_sha256_digest(digest, field=f"files[{binding_id}].sha256")
    byte_count = row.get("byte_count")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count <= 0:
        message = f"files[{binding_id}].byte_count must be a positive integer"
        raise ValueError(message)
    if byte_count > _MAX_EMBEDDED_FILE_BYTES:
        message = f"files[{binding_id}] exceeds the embedded-file size limit"
        raise ValueError(message)
    encoded_content = row.get("content_base64")
    if not isinstance(encoded_content, str) or not encoded_content:
        message = f"files[{binding_id}].content_base64 must be a non-empty string"
        raise TypeError(message)
    try:
        content = base64.b64decode(encoded_content, validate=True)
    except (ValueError, binascii.Error) as exc:
        message = f"files[{binding_id}].content_base64 is malformed"
        raise ValueError(message) from exc
    if len(content) != byte_count:
        message = f"files[{binding_id}] embedded byte count differs"
        raise ValueError(message)
    if hashlib.sha256(content).hexdigest() != digest:
        message = f"files[{binding_id}] embedded digest differs"
        raise ValueError(message)
    return binding_id, file_class, digest, byte_count


def _verify_file_rows(value: object) -> dict[str, tuple[str, int]]:
    rows = _sequence(value, field="files")
    if not rows:
        message = "correctness attestation files must not be empty"
        raise ValueError(message)
    normalized: dict[str, tuple[str, int]] = {}
    observed_ids: list[str] = []
    file_classes: set[str] = set()
    for index, raw_row in enumerate(rows):
        binding_id, file_class, digest, byte_count = _verify_file_row(
            raw_row,
            index=index,
        )
        observed_ids.append(binding_id)
        if binding_id in normalized:
            message = "correctness attestation file IDs must be unique"
            raise ValueError(message)
        normalized[binding_id] = (digest, byte_count)
        file_classes.add(file_class)
    if observed_ids != sorted(observed_ids):
        message = "correctness attestation files must be ordered by ID"
        raise ValueError(message)
    _require_file_class_coverage(file_classes)
    if sum(byte_count for _digest, byte_count in normalized.values()) > _MAX_EMBEDDED_TOTAL_BYTES:
        message = "correctness attestation exceeds the aggregate embedded-file size limit"
        raise ValueError(message)
    return normalized


def _verify_finding_row(
    raw_row: object,
    *,
    index: int,
    known_file_ids: set[str],
) -> str:
    row = _mapping(raw_row, field=f"findings[{index}]")
    _exact_keys(
        row,
        {
            "id",
            "authority",
            "current_behavior",
            "authoritative_behavior",
            "severity",
            "status",
            "finding_class",
            "remediation",
            "evidence_file_ids",
        },
        field=f"findings[{index}]",
    )
    finding_id = _safe_identifier(row.get("id"), field=f"findings[{index}].id")
    _safe_text(row.get("authority"), field=f"findings[{finding_id}].authority")
    _safe_text(
        row.get("current_behavior"),
        field=f"findings[{finding_id}].current_behavior",
    )
    _safe_text(
        row.get("authoritative_behavior"),
        field=f"findings[{finding_id}].authoritative_behavior",
    )
    _parse_enum(
        row.get("severity"),
        CorrectnessFindingSeverity,
        field=f"findings[{finding_id}].severity",
    )
    status = _parse_enum(
        row.get("status"),
        CorrectnessFindingStatus,
        field=f"findings[{finding_id}].status",
    )
    finding_class = _parse_enum(
        row.get("finding_class"),
        CorrectnessFindingClass,
        field=f"findings[{finding_id}].finding_class",
    )
    _safe_text(row.get("remediation"), field=f"findings[{finding_id}].remediation")
    evidence_ids = _sequence(
        row.get("evidence_file_ids"),
        field=f"findings[{finding_id}].evidence_file_ids",
    )
    normalized_evidence_ids = [
        _safe_identifier(value, field=f"findings[{finding_id}].evidence_file_ids[]")
        for value in evidence_ids
    ]
    if not normalized_evidence_ids:
        message = f"findings[{finding_id}].evidence_file_ids must not be empty"
        raise ValueError(message)
    if normalized_evidence_ids != sorted(set(normalized_evidence_ids)):
        message = f"findings[{finding_id}].evidence_file_ids must be sorted and unique"
        raise ValueError(message)
    unknown_files = sorted(set(normalized_evidence_ids) - known_file_ids)
    if unknown_files:
        message = f"findings[{finding_id}] references unknown evidence files: {unknown_files}"
        raise ValueError(message)
    _require_integrity_resolution(
        finding_id=finding_id,
        finding_class=finding_class,
        status=status,
    )
    return finding_id


def _verify_finding_rows(value: object, *, known_file_ids: set[str]) -> None:
    rows = _sequence(value, field="findings")
    if not rows:
        message = "correctness attestation findings must not be empty"
        raise ValueError(message)
    observed_ids = [
        _verify_finding_row(raw_row, index=index, known_file_ids=known_file_ids)
        for index, raw_row in enumerate(rows)
    ]
    if len(set(observed_ids)) != len(observed_ids):
        message = "correctness attestation finding IDs must be unique"
        raise ValueError(message)
    if observed_ids != sorted(observed_ids):
        message = "correctness attestation findings must be ordered by ID"
        raise ValueError(message)


def _verify_reviewer(value: object) -> None:
    reviewer = _mapping(value, field="reviewer")
    _exact_keys(
        reviewer,
        {"identity", "role", "independent", "reviewed_at_utc"},
        field="reviewer",
    )
    _safe_text(reviewer.get("identity"), field="reviewer.identity", maximum_length=256)
    _safe_text(reviewer.get("role"), field="reviewer.role", maximum_length=256)
    if not isinstance(reviewer.get("independent"), bool):
        message = "reviewer.independent must be a bool"
        raise TypeError(message)
    _parse_utc_iso(reviewer.get("reviewed_at_utc"), field="reviewer.reviewed_at_utc")


def _verify_bound_files(
    files: Mapping[str, Path],
    *,
    expected: Mapping[str, tuple[str, int]],
) -> None:
    supplied_ids = set(files)
    expected_ids = set(expected)
    if supplied_ids != expected_ids:
        missing = sorted(expected_ids - supplied_ids)
        extra = sorted(supplied_ids - expected_ids)
        message = f"attested file binding IDs differ (missing={missing}, extra={extra})"
        raise ValueError(message)
    for binding_id, path in files.items():
        if not isinstance(path, Path):
            message = f"attested file binding {binding_id} must resolve to pathlib.Path"
            raise TypeError(message)
        if not path.is_file():
            message = f"attested file does not exist: {path}"
            raise FileNotFoundError(message)
        expected_digest, expected_size = expected[binding_id]
        if path.stat().st_size != expected_size:
            message = f"attested file size differs for {binding_id}"
            raise ValueError(message)
        if _file_sha256(path) != expected_digest:
            message = f"attested file digest differs for {binding_id}"
            raise ValueError(message)


def verify_strategy_correctness_attestation(
    payload: Mapping[str, object],
    *,
    files: Mapping[str, Path] | None = None,
) -> None:
    """Verify schema, self-hash, semantics, and optionally the current file bytes."""

    artifact = _verify_attestation_header(payload)
    _verify_subject(artifact.get("subject"))
    normalized_files = _verify_file_rows(artifact.get("files"))

    _verify_finding_rows(
        artifact.get("findings"),
        known_file_ids=set(normalized_files),
    )
    _verify_reviewer(artifact.get("reviewer"))

    # Reject values that cannot be represented without loss when a caller passes
    # a Python mapping directly instead of decoding canonical JSON first.
    canonical_manifest_bytes(artifact)
    if files is not None:
        _verify_bound_files(files, expected=normalized_files)


def summarize_strategy_correctness_attestation(
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Return a deterministic decision-relevant summary of a valid attestation."""

    verify_strategy_correctness_attestation(payload)
    artifact = _mapping(payload, field="attestation")
    file_rows = _sequence(artifact.get("files"), field="files")
    finding_rows = [
        _mapping(row, field=f"findings[{index}]")
        for index, row in enumerate(_sequence(artifact.get("findings"), field="findings"))
    ]
    strategy_findings = [
        row
        for row in finding_rows
        if row.get("finding_class") == CorrectnessFindingClass.STRATEGY_SEMANTIC.value
    ]
    integrity_findings = [
        row
        for row in finding_rows
        if row.get("finding_class") == CorrectnessFindingClass.EVIDENCE_INTEGRITY.value
    ]

    def finding_ids(status: CorrectnessFindingStatus) -> list[str]:
        return sorted(str(row["id"]) for row in finding_rows if row.get("status") == status.value)

    resolved_integrity = sorted(
        str(row["id"])
        for row in integrity_findings
        if row.get("status")
        in {
            CorrectnessFindingStatus.RESOLVED.value,
            CorrectnessFindingStatus.VERIFIED_EQUIVALENT.value,
        }
    )
    return {
        "file_count": len(file_rows),
        "finding_count": len(finding_rows),
        "strategy_semantic_finding_count": len(strategy_findings),
        "evidence_integrity_finding_count": len(integrity_findings),
        "remediation_required_finding_ids": finding_ids(
            CorrectnessFindingStatus.REMEDIATION_REQUIRED
        ),
        "accepted_deviation_finding_ids": finding_ids(CorrectnessFindingStatus.ACCEPTED_DEVIATION),
        "resolved_evidence_integrity_finding_ids": resolved_integrity,
        "unresolved_evidence_integrity_finding_count": 0,
    }


def _contract_file_rows(value: object) -> list[dict[str, object]]:
    rows = _sequence(value, field="correctness contract files")
    if not rows:
        message = "correctness contract files must not be empty"
        raise ValueError(message)
    normalized: list[dict[str, object]] = []
    observed_ids: list[str] = []
    total_bytes = 0
    for index, raw_row in enumerate(rows):
        row = _mapping(raw_row, field=f"correctness contract files[{index}]")
        _exact_keys(
            row,
            {
                "id",
                "class",
                "content_classification",
                "location",
                "path_scope",
                "sha256",
                "byte_count",
            },
            field=f"correctness contract files[{index}]",
        )
        binding_id = _safe_identifier(
            row.get("id"), field=f"correctness contract files[{index}].id"
        )
        file_class = _parse_enum(
            row.get("class"),
            CorrectnessFileClass,
            field=f"correctness contract files[{binding_id}].class",
        )
        content_classification = _parse_enum(
            row.get("content_classification"),
            CorrectnessContentClassification,
            field=(f"correctness contract files[{binding_id}].content_classification"),
        )
        expected_classification = (
            CorrectnessContentClassification.NON_SENSITIVE_FIXTURE.value
            if file_class == CorrectnessFileClass.FIXTURE.value
            else CorrectnessContentClassification.NON_SENSITIVE_SOURCE.value
        )
        if content_classification != expected_classification:
            message = (
                f"correctness contract files[{binding_id}] content classification "
                "conflicts with its file class"
            )
            raise ValueError(message)
        _safe_text(
            row.get("location"),
            field=f"correctness contract files[{binding_id}].location",
            maximum_length=2048,
        )
        _parse_enum(
            row.get("path_scope"),
            CorrectnessPathScope,
            field=f"correctness contract files[{binding_id}].path_scope",
        )
        digest = row.get("sha256")
        if not isinstance(digest, str):
            message = f"correctness contract files[{binding_id}].sha256 must be a string"
            raise TypeError(message)
        validate_sha256_digest(
            digest,
            field=f"correctness contract files[{binding_id}].sha256",
        )
        byte_count = row.get("byte_count")
        if (
            isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count <= 0
            or byte_count > _MAX_EMBEDDED_FILE_BYTES
        ):
            message = (
                f"correctness contract files[{binding_id}].byte_count is outside "
                "the embedding limit"
            )
            raise ValueError(message)
        total_bytes += byte_count
        observed_ids.append(binding_id)
        normalized.append(dict(row))
    if len(set(observed_ids)) != len(observed_ids):
        message = "correctness contract file IDs must be unique"
        raise ValueError(message)
    if observed_ids != sorted(observed_ids):
        message = "correctness contract files must be ordered by ID"
        raise ValueError(message)
    if total_bytes > _MAX_EMBEDDED_TOTAL_BYTES:
        message = "correctness contract exceeds the aggregate embedded-file size limit"
        raise ValueError(message)
    return normalized


def _contract_summary(value: object) -> dict[str, object]:
    summary = _mapping(value, field="correctness contract exact_attestation.summary")
    expected_keys = {
        "file_count",
        "finding_count",
        "strategy_semantic_finding_count",
        "evidence_integrity_finding_count",
        "remediation_required_finding_ids",
        "accepted_deviation_finding_ids",
        "resolved_evidence_integrity_finding_ids",
        "unresolved_evidence_integrity_finding_count",
    }
    _exact_keys(
        summary,
        expected_keys,
        field="correctness contract exact_attestation.summary",
    )
    return dict(summary)


def _decision_integer(summary: Mapping[str, object], field: str) -> int:
    value = summary.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        message = f"correctness contract summary {field} must be a non-negative integer"
        raise ValueError(message)
    return value


def _decision_ids(summary: Mapping[str, object], field: str) -> tuple[str, ...]:
    values = _sequence(summary.get(field), field=f"correctness contract summary {field}")
    normalized = tuple(
        _safe_identifier(value, field=f"correctness contract summary {field}[]") for value in values
    )
    if normalized != tuple(sorted(set(normalized))):
        message = f"correctness contract summary {field} must be sorted and unique"
        raise ValueError(message)
    return normalized


def _verify_contract_files(
    artifact: Mapping[str, object],
    contract_files: Sequence[Mapping[str, object]],
) -> tuple[str, ...]:
    artifact_file_rows = {
        str(row["id"]): row
        for row in (
            _mapping(raw_row, field=f"attestation files[{index}]")
            for index, raw_row in enumerate(_sequence(artifact.get("files"), field="files"))
        )
    }
    contract_file_ids = tuple(str(row["id"]) for row in contract_files)
    if set(artifact_file_rows) != set(contract_file_ids):
        missing = sorted(set(contract_file_ids) - set(artifact_file_rows))
        extra = sorted(set(artifact_file_rows) - set(contract_file_ids))
        message = (
            "correctness attestation file IDs differ from its contract "
            f"(missing={missing}, extra={extra})"
        )
        raise ValueError(message)
    compared_fields = (
        "id",
        "class",
        "content_classification",
        "location",
        "sha256",
        "byte_count",
    )
    for contract_row in contract_files:
        binding_id = str(contract_row["id"])
        artifact_row = artifact_file_rows[binding_id]
        if any(artifact_row.get(field) != contract_row.get(field) for field in compared_fields):
            message = f"correctness attestation file {binding_id} differs from its contract"
            raise ValueError(message)
    return contract_file_ids


def _verify_contract_findings(
    artifact: Mapping[str, object],
    contract_value: object,
    *,
    known_file_ids: set[str],
) -> None:
    contract_findings = [
        dict(_mapping(row, field=f"correctness contract findings[{index}]"))
        for index, row in enumerate(
            _sequence(contract_value, field="correctness contract findings")
        )
    ]
    _verify_finding_rows(contract_findings, known_file_ids=known_file_ids)
    artifact_findings = [
        dict(_mapping(row, field=f"attestation findings[{index}]"))
        for index, row in enumerate(_sequence(artifact.get("findings"), field="findings"))
    ]
    if artifact_findings != contract_findings:
        message = "correctness attestation findings differ from its contract"
        raise ValueError(message)


def _verify_contract_reviewer(
    artifact: Mapping[str, object], contract_value: object
) -> Mapping[str, object]:
    reviewer = _mapping(contract_value, field="correctness contract reviewer")
    _verify_reviewer(reviewer)
    if artifact.get("reviewer") != dict(reviewer):
        message = "correctness attestation reviewer differs from its contract"
        raise ValueError(message)
    return reviewer


def verify_registered_strategy_correctness_attestation(
    payload: Mapping[str, object],
    contract: Mapping[str, object],
) -> RegisteredCorrectnessAttestationDecision:
    """Reconcile a self-contained attestation with its exact protocol contract.

    File resolution and artifact construction intentionally remain outside this
    verifier.  This function is the shared trust boundary for CLI generation and
    campaign manifest admission.
    """

    verify_strategy_correctness_attestation(payload)
    registered = _mapping(contract, field="correctness contract")
    _exact_keys(
        registered,
        {
            "required",
            "schema_id",
            "schema_version",
            "attestation_schema_id",
            "attestation_schema_version",
            "subject",
            "files",
            "findings",
            "reviewer",
            "exact_attestation",
        },
        field="correctness contract",
    )
    if registered.get("required") is not True:
        message = "correctness contract must be required"
        raise ValueError(message)
    if (
        registered.get("schema_id") != _CONTRACT_SCHEMA_ID
        or registered.get("schema_version") != _CONTRACT_SCHEMA_VERSION
        or registered.get("attestation_schema_id") != _ATTESTATION_SCHEMA_ID
        or registered.get("attestation_schema_version") != _ATTESTATION_SCHEMA_VERSION
    ):
        message = "correctness contract schema identity is unsupported"
        raise ValueError(message)

    artifact = _mapping(payload, field="attestation")
    subject = _mapping(registered.get("subject"), field="correctness contract subject")
    _verify_subject(subject)
    if artifact.get("subject") != dict(subject):
        message = "correctness attestation subject differs from its contract"
        raise ValueError(message)

    contract_file_ids = _verify_contract_files(
        artifact, _contract_file_rows(registered.get("files"))
    )
    _verify_contract_findings(
        artifact,
        registered.get("findings"),
        known_file_ids=set(contract_file_ids),
    )
    contract_reviewer = _verify_contract_reviewer(artifact, registered.get("reviewer"))

    exact = _mapping(
        registered.get("exact_attestation"),
        field="correctness contract exact_attestation",
    )
    _exact_keys(
        exact,
        {"status", "attestation_sha256", "summary"},
        field="correctness contract exact_attestation",
    )
    if exact.get("status") != _FROZEN_CONTRACT_STATUS:
        message = "correctness contract is not frozen pre-outcome"
        raise ValueError(message)
    expected_hash = exact.get("attestation_sha256")
    if not isinstance(expected_hash, str):
        message = "correctness contract attestation_sha256 must be a string"
        raise TypeError(message)
    validate_sha256_digest(
        expected_hash,
        field="correctness contract exact_attestation.attestation_sha256",
    )
    if artifact.get("attestation_sha256") != expected_hash:
        message = "correctness attestation hash differs from its contract"
        raise ValueError(message)
    observed_summary = summarize_strategy_correctness_attestation(artifact)
    expected_summary = _contract_summary(exact.get("summary"))
    if observed_summary != expected_summary:
        message = "correctness attestation summary differs from its contract"
        raise ValueError(message)
    canonical_manifest_bytes(registered)

    strategy_id = subject.get("strategy_id")
    strategy_version = subject.get("strategy_version")
    reviewer_independent = contract_reviewer.get("independent")
    assert isinstance(strategy_id, str)
    assert isinstance(strategy_version, str)
    assert isinstance(reviewer_independent, bool)
    return RegisteredCorrectnessAttestationDecision(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        attestation_sha256=expected_hash,
        contract_status=_FROZEN_CONTRACT_STATUS,
        file_ids=contract_file_ids,
        file_count=_decision_integer(observed_summary, "file_count"),
        finding_count=_decision_integer(observed_summary, "finding_count"),
        strategy_semantic_finding_count=_decision_integer(
            observed_summary, "strategy_semantic_finding_count"
        ),
        evidence_integrity_finding_count=_decision_integer(
            observed_summary, "evidence_integrity_finding_count"
        ),
        remediation_required_finding_ids=_decision_ids(
            observed_summary, "remediation_required_finding_ids"
        ),
        accepted_deviation_finding_ids=_decision_ids(
            observed_summary, "accepted_deviation_finding_ids"
        ),
        resolved_evidence_integrity_finding_ids=_decision_ids(
            observed_summary, "resolved_evidence_integrity_finding_ids"
        ),
        unresolved_evidence_integrity_finding_count=_decision_integer(
            observed_summary, "unresolved_evidence_integrity_finding_count"
        ),
        reviewer_independent=reviewer_independent,
    )


require_exact_keys = _exact_keys
require_mapping = _mapping
require_sequence = _sequence
safe_identifier = _safe_identifier


__all__ = [
    "CorrectnessContentClassification",
    "CorrectnessFileBinding",
    "CorrectnessFileClass",
    "CorrectnessFindingClass",
    "CorrectnessFindingSeverity",
    "CorrectnessFindingStatus",
    "CorrectnessPathScope",
    "CorrectnessReviewer",
    "CorrectnessSemanticFinding",
    "RegisteredCorrectnessAttestationDecision",
    "build_strategy_correctness_attestation",
    "require_exact_keys",
    "require_mapping",
    "require_sequence",
    "safe_identifier",
    "summarize_strategy_correctness_attestation",
    "verify_registered_strategy_correctness_attestation",
    "verify_strategy_correctness_attestation",
]
