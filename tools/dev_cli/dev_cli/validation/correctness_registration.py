"""Protocol registration and artifact paths for correctness attestations."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from dev_cli.validation.correctness import (
    CorrectnessContentClassification,
    CorrectnessFileBinding,
    CorrectnessFileClass,
    CorrectnessFindingClass,
    CorrectnessFindingSeverity,
    CorrectnessFindingStatus,
    CorrectnessReviewer,
    CorrectnessSemanticFinding,
    build_strategy_correctness_attestation,
    require_exact_keys,
    require_mapping,
    require_sequence,
    verify_registered_strategy_correctness_attestation,
    verify_strategy_correctness_attestation,
)
from dev_cli.validation.evidence import (
    atomic_create_bytes,
    canonical_json_bytes,
    file_sha256,
    load_json_object,
    parse_utc_datetime,
    require_descendant,
    resolve_strategy_validation_artifact,
)
from dev_cli.validation.persistence.backtest_manifest_store import validate_sha256_digest

_STRATEGY_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_CONTRACT_SCHEMA_ID = "vynmatrix.strategy-correctness-contract"
_CONTRACT_SCHEMA_VERSION = 1
_ATTESTATION_SCHEMA_ID = "vynmatrix.strategy-correctness-attestation"
_ATTESTATION_SCHEMA_VERSION = 1
_REPOSITORY_SCOPE = "repository_relative"
_EXTERNAL_SCOPE = "explicit_absolute_external"


def resolve_indicator_strategy_path(repo_root: Path, strategy_name: str) -> Path:
    """Resolve one named indicator strategy without permitting path traversal."""

    if not _STRATEGY_NAME_RE.fullmatch(strategy_name):
        message = f"invalid strategy name: {strategy_name!r}"
        raise ValueError(message)
    strategy_root = (repo_root / "strategies" / "indicator").resolve()
    strategy_path = (strategy_root / strategy_name).resolve()
    require_descendant(strategy_path, strategy_root, field="strategy path")
    if not strategy_path.is_dir():
        message = f"indicator strategy does not exist: {strategy_name}"
        raise FileNotFoundError(message)
    return strategy_path


def _parse_file_values(values: tuple[str, ...]) -> dict[str, Path]:
    parsed: dict[str, Path] = {}
    for value in values:
        binding_id, separator, raw_path = value.partition("=")
        if not separator or not binding_id or not raw_path:
            message = f"correctness file binding must use ID=PATH: {value!r}"
            raise ValueError(message)
        if binding_id in parsed:
            message = f"duplicate correctness file binding ID: {binding_id}"
            raise ValueError(message)
        parsed[binding_id] = Path(raw_path)
    return parsed


def _resolve_binding_path(
    raw_path: Path,
    *,
    path_scope: str,
    location: str,
    repo_root: Path,
    binding_id: str,
) -> Path:
    if raw_path.is_symlink():
        message = f"correctness file binding {binding_id} must not be a symlink"
        raise ValueError(message)
    if path_scope == _REPOSITORY_SCOPE:
        candidate = raw_path if raw_path.is_absolute() else repo_root / raw_path
        resolved = candidate.resolve()
        require_descendant(
            resolved,
            repo_root,
            field=f"correctness file binding {binding_id}",
        )
        if resolved.relative_to(repo_root).as_posix() != location:
            message = (
                f"correctness file binding {binding_id} does not resolve to its "
                f"registered repository location {location}"
            )
            raise ValueError(message)
    elif path_scope == _EXTERNAL_SCOPE:
        if not raw_path.is_absolute():
            message = (
                f"external correctness file binding {binding_id} requires an explicit absolute path"
            )
            raise ValueError(message)
        resolved = raw_path.resolve()
    else:
        message = f"correctness file binding {binding_id} has unknown path_scope"
        raise ValueError(message)
    if not resolved.is_file():
        message = f"correctness file binding does not exist: {resolved}"
        raise FileNotFoundError(message)
    return resolved


def _required_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        message = f"{field} must be a non-blank string without surrounding whitespace"
        raise ValueError(message)
    return value


def _registered_contract(protocol: Mapping[str, object]) -> Mapping[str, object]:
    contract = require_mapping(
        protocol.get("correctness_attestation"),
        field="validation protocol correctness_attestation",
    )
    require_exact_keys(
        contract,
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
        field="validation protocol correctness_attestation",
    )
    expected_header = {
        "required": True,
        "schema_id": _CONTRACT_SCHEMA_ID,
        "schema_version": _CONTRACT_SCHEMA_VERSION,
        "attestation_schema_id": _ATTESTATION_SCHEMA_ID,
        "attestation_schema_version": _ATTESTATION_SCHEMA_VERSION,
    }
    if any(contract.get(key) != value for key, value in expected_header.items()):
        message = "correctness attestation protocol identity or requirement differs"
        raise ValueError(message)
    return contract


def _registered_file_row(
    raw_row: object,
    *,
    index: int,
    supplied_paths: Mapping[str, Path],
    repo_root: Path,
) -> tuple[CorrectnessFileBinding, Path]:
    row = require_mapping(raw_row, field=f"correctness_attestation.files[{index}]")
    require_exact_keys(
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
        field=f"correctness_attestation.files[{index}]",
    )
    binding_id = _required_string(
        row.get("id"),
        field=f"correctness_attestation.files[{index}].id",
    )
    file_class = _required_string(
        row.get("class"),
        field=f"correctness_attestation.files[{binding_id}].class",
    )
    content_classification = _required_string(
        row.get("content_classification"),
        field=f"correctness_attestation.files[{binding_id}].content_classification",
    )
    location = _required_string(
        row.get("location"),
        field=f"correctness_attestation.files[{binding_id}].location",
    )
    path_scope = _required_string(
        row.get("path_scope"),
        field=f"correctness_attestation.files[{binding_id}].path_scope",
    )
    digest = _required_string(
        row.get("sha256"),
        field=f"correctness_attestation.files[{binding_id}].sha256",
    )
    byte_count = row.get("byte_count")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count <= 0:
        message = f"correctness_attestation.files[{binding_id}].byte_count must be positive"
        raise ValueError(message)
    validate_sha256_digest(
        digest,
        field=f"correctness_attestation.files[{binding_id}].sha256",
    )
    if binding_id not in supplied_paths:
        message = f"missing correctness file binding ID: {binding_id}"
        raise ValueError(message)
    path = _resolve_binding_path(
        supplied_paths[binding_id],
        path_scope=path_scope,
        location=location,
        repo_root=repo_root,
        binding_id=binding_id,
    )
    if path.stat().st_size != byte_count or file_sha256(path) != digest:
        message = f"correctness file binding {binding_id} differs from the protocol"
        raise ValueError(message)
    try:
        parsed_file_class = CorrectnessFileClass(file_class)
        parsed_content_classification = CorrectnessContentClassification(content_classification)
    except ValueError as exc:
        message = f"correctness file binding {binding_id} has unknown class"
        raise ValueError(message) from exc
    return (
        CorrectnessFileBinding(
            binding_id=binding_id,
            file_class=parsed_file_class,
            content_classification=parsed_content_classification,
            path=path,
            location=location,
        ),
        path,
    )


def _registered_files(
    contract: Mapping[str, object],
    *,
    file_values: tuple[str, ...],
    repo_root: Path,
) -> tuple[list[CorrectnessFileBinding], dict[str, Path]]:
    supplied_paths = _parse_file_values(file_values)
    rows = require_sequence(contract.get("files"), field="correctness_attestation.files")
    if not rows:
        message = "correctness_attestation.files must not be empty"
        raise ValueError(message)
    bindings: list[CorrectnessFileBinding] = []
    bound_files: dict[str, Path] = {}
    for index, raw_row in enumerate(rows):
        binding, path = _registered_file_row(
            raw_row,
            index=index,
            supplied_paths=supplied_paths,
            repo_root=repo_root,
        )
        if binding.binding_id in bound_files:
            message = f"duplicate correctness protocol file ID: {binding.binding_id}"
            raise ValueError(message)
        bindings.append(binding)
        bound_files[binding.binding_id] = path
    extra_file_ids = sorted(set(supplied_paths) - set(bound_files))
    if extra_file_ids:
        message = f"unknown correctness file binding IDs: {extra_file_ids}"
        raise ValueError(message)
    return bindings, bound_files


def _registered_finding(raw_row: object, *, index: int) -> CorrectnessSemanticFinding:
    row = require_mapping(raw_row, field=f"correctness_attestation.findings[{index}]")
    require_exact_keys(
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
        field=f"correctness_attestation.findings[{index}]",
    )
    finding_id = _required_string(
        row.get("id"),
        field=f"correctness_attestation.findings[{index}].id",
    )
    text_fields = {
        field: _required_string(
            row.get(field),
            field=f"correctness_attestation.findings[{finding_id}].{field}",
        )
        for field in (
            "authority",
            "current_behavior",
            "authoritative_behavior",
            "remediation",
        )
    }
    severity = _required_string(
        row.get("severity"),
        field=f"correctness_attestation.findings[{finding_id}].severity",
    )
    status = _required_string(
        row.get("status"),
        field=f"correctness_attestation.findings[{finding_id}].status",
    )
    finding_class = _required_string(
        row.get("finding_class"),
        field=f"correctness_attestation.findings[{finding_id}].finding_class",
    )
    evidence_ids = tuple(
        _required_string(
            value,
            field=f"correctness_attestation.findings[{finding_id}].evidence_file_ids[]",
        )
        for value in require_sequence(
            row.get("evidence_file_ids"),
            field=f"correctness_attestation.findings[{finding_id}].evidence_file_ids",
        )
    )
    try:
        return CorrectnessSemanticFinding(
            finding_id=finding_id,
            authority=text_fields["authority"],
            current_behavior=text_fields["current_behavior"],
            authoritative_behavior=text_fields["authoritative_behavior"],
            severity=CorrectnessFindingSeverity(severity),
            status=CorrectnessFindingStatus(status),
            finding_class=CorrectnessFindingClass(finding_class),
            remediation=text_fields["remediation"],
            evidence_file_ids=evidence_ids,
        )
    except ValueError as exc:
        message = f"correctness_attestation.findings[{index}] has an unknown enum"
        raise ValueError(message) from exc


def _registered_findings(contract: Mapping[str, object]) -> list[CorrectnessSemanticFinding]:
    rows = require_sequence(
        contract.get("findings"),
        field="correctness_attestation.findings",
    )
    return [_registered_finding(raw_row, index=index) for index, raw_row in enumerate(rows)]


def _registered_reviewer(contract: Mapping[str, object]) -> CorrectnessReviewer:
    row = require_mapping(
        contract.get("reviewer"),
        field="correctness_attestation.reviewer",
    )
    require_exact_keys(
        row,
        {"identity", "role", "independent", "reviewed_at_utc"},
        field="correctness_attestation.reviewer",
    )
    identity = _required_string(
        row.get("identity"), field="correctness_attestation.reviewer.identity"
    )
    role = _required_string(row.get("role"), field="correctness_attestation.reviewer.role")
    independent = row.get("independent")
    if not isinstance(independent, bool):
        message = "correctness_attestation.reviewer.independent must be a bool"
        raise TypeError(message)
    reviewed_at = _required_string(
        row.get("reviewed_at_utc"),
        field="correctness_attestation.reviewer.reviewed_at_utc",
    )
    return CorrectnessReviewer(
        identity=identity,
        role=role,
        independent=independent,
        reviewed_at_utc=parse_utc_datetime(
            reviewed_at,
            field="correctness_attestation.reviewer.reviewed_at_utc",
        ),
    )


def build_registered_correctness_attestation(
    protocol: Mapping[str, object],
    *,
    file_values: tuple[str, ...],
    repo_root: Path,
) -> tuple[dict[str, object], dict[str, Path]]:
    """Build and verify the exact correctness contract registered by a protocol."""

    contract = _registered_contract(protocol)
    file_bindings, bound_files = _registered_files(
        contract,
        file_values=file_values,
        repo_root=repo_root,
    )
    findings = _registered_findings(contract)
    reviewer = _registered_reviewer(contract)
    subject = require_mapping(
        contract.get("subject"),
        field="correctness_attestation.subject",
    )
    require_exact_keys(
        subject,
        {"strategy_id", "strategy_version"},
        field="correctness_attestation.subject",
    )
    payload = build_strategy_correctness_attestation(
        strategy_id=_required_string(
            subject.get("strategy_id"),
            field="correctness_attestation.subject.strategy_id",
        ),
        strategy_version=_required_string(
            subject.get("strategy_version"),
            field="correctness_attestation.subject.strategy_version",
        ),
        files=tuple(file_bindings),
        findings=tuple(findings),
        reviewer=reviewer,
    )
    verify_strategy_correctness_attestation(payload, files=bound_files)
    verify_registered_strategy_correctness_attestation(payload, contract)
    return payload, bound_files


def resolve_correctness_attestation_output(
    repo_root: Path,
    strategy_name: str,
    attestation_hash: str,
    output: Path | None,
) -> Path:
    """Resolve the immutable, content-addressed correctness output path."""

    if not re.fullmatch(r"[0-9a-f]{64}", attestation_hash):
        message = "attestation_hash must be a lowercase SHA-256 digest"
        raise ValueError(message)
    expected_name = f"{strategy_name}-correctness-attestation-{attestation_hash}.json"
    return resolve_strategy_validation_artifact(
        repo_root,
        expected_name=expected_name,
        output=output,
        field="correctness attestation output",
    )


def create_registered_correctness_attestation(
    *,
    repo_root: Path,
    strategy_name: str,
    file_values: tuple[str, ...],
    output: Path | None,
) -> tuple[dict[str, object], Path]:
    """Create and reverify one immutable protocol-registered attestation."""

    strategy_path = resolve_indicator_strategy_path(repo_root, strategy_name)
    protocol = load_json_object(strategy_path / "validation_protocol.json")
    payload, bound_files = build_registered_correctness_attestation(
        protocol,
        file_values=file_values,
        repo_root=repo_root,
    )
    digest = str(payload["attestation_sha256"])
    destination = resolve_correctness_attestation_output(
        repo_root,
        strategy_name,
        digest,
        output,
    )
    atomic_create_bytes(destination, canonical_json_bytes(payload))

    stored_payload = load_json_object(destination)
    if stored_payload != payload:
        message = "stored correctness attestation differs from generated payload"
        raise RuntimeError(message)
    verify_strategy_correctness_attestation(stored_payload, files=bound_files)
    destination.chmod(0o444)
    return payload, destination


__all__ = [
    "build_registered_correctness_attestation",
    "create_registered_correctness_attestation",
    "resolve_correctness_attestation_output",
    "resolve_indicator_strategy_path",
]
