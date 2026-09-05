"""Fail-closed attestation for upstream strategy-selection trial ledgers."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

from dev_cli.validation.evidence import require_descendant
from dev_cli.validation.persistence.backtest_manifest_store import (
    manifest_sha256,
)

SELECTION_LEDGER_SCHEMA_VERSION = "1.0"
_FINITE_INCLUSION_POLICY = "all_nonblank_finite_sharpe_values"
_NORMALIZED_ROW_BASE_FIELDS = ("sequence",)
_MIN_DISPERSION_OBSERVATIONS = 2
_MIN_SUMMARY_DECIMAL_PLACES = 6
_MAX_SUMMARY_DECIMAL_PLACES = 18
_SHA256_LENGTH = 64
_PRIOR_DIAGNOSTIC_ATTEMPTS_FIELDS = frozenset(
    {
        "arm_ids",
        "attempt_count",
        "correctness_fixture_id",
        "raw_artifact_sha256",
        "content_addressed_artifact_sha256",
        "content_sha256_field",
        "selection_contaminated",
        "authoritative",
        "sharpe_dispersion_policy",
    }
)
_PRIOR_DIAGNOSTIC_SHARPE_POLICY = "count_only_no_finite_sharpe_dispersion"


def _validated_prior_diagnostic_arm_ids(
    raw_contract: Mapping[str, Any],
) -> tuple[str, ...]:
    raw_arm_ids = raw_contract.get("arm_ids")
    if isinstance(raw_arm_ids, (str, bytes)) or not isinstance(raw_arm_ids, Sequence):
        message = "prior_diagnostic_attempts.arm_ids must be an array"
        raise TypeError(message)
    arm_ids: list[str] = []
    for index, raw_arm_id in enumerate(raw_arm_ids):
        if not isinstance(raw_arm_id, str) or not raw_arm_id or raw_arm_id.strip() != raw_arm_id:
            message = f"prior_diagnostic_attempts.arm_ids[{index}] is invalid"
            raise ValueError(message)
        arm_ids.append(raw_arm_id)
    if not arm_ids or len(set(arm_ids)) != len(arm_ids):
        message = "prior_diagnostic_attempts.arm_ids must be non-empty and unique"
        raise ValueError(message)
    return tuple(arm_ids)


def _verify_prior_diagnostic_artifact_content(
    *,
    repo_root: Path,
    location: str,
    raw_sha256: str,
    content_sha256: str,
    content_sha256_field: str,
    expected_arm_ids: tuple[str, ...],
) -> None:
    raw_path = Path(location)
    if raw_path.is_absolute():
        message = "prior diagnostic artifact must use a repository-relative fixture"
        raise ValueError(message)
    artifact_path = repo_root.resolve() / raw_path
    if artifact_path.is_symlink():
        message = "prior diagnostic artifact must not be a symlink"
        raise ValueError(message)
    artifact_path = require_descendant(
        artifact_path,
        repo_root,
        field="prior diagnostic artifact",
    )
    if not artifact_path.is_file():
        message = f"prior diagnostic artifact is unavailable: {location}"
        raise FileNotFoundError(message)
    artifact_bytes = artifact_path.read_bytes()
    if hashlib.sha256(artifact_bytes).hexdigest() != raw_sha256:
        message = "prior diagnostic artifact raw SHA-256 differs"
        raise ValueError(message)
    artifact = json.loads(artifact_bytes)
    if not isinstance(artifact, dict):
        message = "prior diagnostic artifact root must be a JSON object"
        raise TypeError(message)
    if artifact.get(content_sha256_field) != content_sha256:
        message = "prior diagnostic artifact internal content SHA-256 differs"
        raise ValueError(message)
    artifact_arm_ids = artifact.get("arm_ids")
    if artifact_arm_ids is not None and (
        isinstance(artifact_arm_ids, (str, bytes))
        or not isinstance(artifact_arm_ids, Sequence)
        or tuple(artifact_arm_ids) != expected_arm_ids
    ):
        message = "prior diagnostic artifact arm IDs differ from the protocol"
        raise ValueError(message)
    unsigned = dict(artifact)
    del unsigned[content_sha256_field]
    if manifest_sha256(unsigned) != content_sha256:
        message = "prior diagnostic artifact canonical content SHA-256 differs"
        raise ValueError(message)


def _validate_prior_diagnostic_fixture_binding(
    protocol: Mapping[str, Any],
    raw_contract: Mapping[str, Any],
    *,
    repo_root: Path,
) -> None:
    fixture_id = raw_contract.get("correctness_fixture_id")
    if not isinstance(fixture_id, str) or not fixture_id.strip():
        message = "prior_diagnostic_attempts.correctness_fixture_id is invalid"
        raise ValueError(message)
    raw_sha256 = raw_contract.get("raw_artifact_sha256")
    content_sha256 = raw_contract.get("content_addressed_artifact_sha256")
    if (
        not isinstance(raw_sha256, str)
        or len(raw_sha256) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in raw_sha256)
    ):
        message = "prior_diagnostic_attempts.raw_artifact_sha256 is invalid"
        raise ValueError(message)
    if (
        not isinstance(content_sha256, str)
        or len(content_sha256) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in content_sha256)
    ):
        message = "prior_diagnostic_attempts.content_addressed_artifact_sha256 is invalid"
        raise ValueError(message)
    content_sha256_field = raw_contract.get("content_sha256_field")
    if (
        not isinstance(content_sha256_field, str)
        or not content_sha256_field
        or content_sha256_field.strip() != content_sha256_field
    ):
        message = "prior_diagnostic_attempts.content_sha256_field is invalid"
        raise ValueError(message)

    correctness = protocol.get(
        "correctness_attestation_contract",
        protocol.get("correctness_attestation"),
    )
    if not isinstance(correctness, Mapping):
        message = "correctness_attestation must bind prior diagnostic evidence"
        raise TypeError(message)
    raw_files = correctness.get("files")
    if isinstance(raw_files, (str, bytes)) or not isinstance(raw_files, Sequence):
        message = "correctness_attestation.files must be an array"
        raise TypeError(message)
    fixture_rows = [
        row for row in raw_files if isinstance(row, Mapping) and row.get("id") == fixture_id
    ]
    if len(fixture_rows) != 1:
        message = "prior diagnostic artifact has no exact correctness fixture binding"
        raise ValueError(message)
    fixture = fixture_rows[0]
    location = fixture.get("location")
    path_scope = fixture.get("path_scope")
    if (
        fixture.get("class") != "fixture"
        or fixture.get("sha256") != raw_sha256
        or not isinstance(location, str)
        or path_scope != "repository_relative"
        or content_sha256 not in Path(location).name
    ):
        message = "prior diagnostic artifact differs from its correctness fixture binding"
        raise ValueError(message)
    _verify_prior_diagnostic_artifact_content(
        repo_root=repo_root,
        location=location,
        raw_sha256=raw_sha256,
        content_sha256=content_sha256,
        content_sha256_field=content_sha256_field,
        expected_arm_ids=_validated_prior_diagnostic_arm_ids(raw_contract),
    )


def validated_prior_diagnostic_attempt_count(
    protocol: Mapping[str, Any],
    *,
    repo_root: Path | None = None,
) -> int:
    """Validate optional inspected diagnostics without treating them as observations."""

    raw_multiple_testing = protocol.get("multiple_testing")
    if not isinstance(raw_multiple_testing, Mapping):
        message = "multiple_testing must be a JSON object"
        raise TypeError(message)
    raw_contract = raw_multiple_testing.get("prior_diagnostic_attempts")
    if raw_contract is None:
        return 0
    if repo_root is None:
        message = "repo_root is required to verify prior diagnostic artifact bytes"
        raise ValueError(message)
    if not isinstance(raw_contract, Mapping):
        message = "prior_diagnostic_attempts must be a JSON object"
        raise TypeError(message)
    if set(raw_contract) != _PRIOR_DIAGNOSTIC_ATTEMPTS_FIELDS:
        message = "prior_diagnostic_attempts fields differ from the exact contract"
        raise ValueError(message)

    arm_ids = _validated_prior_diagnostic_arm_ids(raw_contract)
    attempt_count = raw_contract.get("attempt_count")
    if (
        isinstance(attempt_count, bool)
        or not isinstance(attempt_count, int)
        or attempt_count != len(arm_ids)
    ):
        message = "prior_diagnostic_attempts.attempt_count differs from its unique arm IDs"
        raise ValueError(message)
    if (
        raw_contract.get("selection_contaminated") is not True
        or raw_contract.get("authoritative") is not False
        or raw_contract.get("sharpe_dispersion_policy") != _PRIOR_DIAGNOSTIC_SHARPE_POLICY
    ):
        message = "prior_diagnostic_attempts evidence authority contract differs"
        raise ValueError(message)
    _validate_prior_diagnostic_fixture_binding(
        protocol,
        raw_contract,
        repo_root=repo_root,
    )
    return attempt_count


def load_upstream_selection_ledger(
    path: Path,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Read, hash, normalize, and validate one authoritative CSV ledger."""

    resolved = path.resolve()
    if not resolved.is_file():
        message = f"upstream selection ledger is not a readable file: {path}"
        raise FileNotFoundError(message)
    raw = resolved.read_bytes()
    expected = _validated_contract(contract)
    observed_sha256 = hashlib.sha256(raw).hexdigest()
    if observed_sha256 != expected["expected_sha256"]:
        message = "upstream selection ledger SHA-256 differs from the frozen protocol"
        raise ValueError(message)
    if resolved.name != expected["source_filename"]:
        message = "upstream selection ledger filename differs from the frozen protocol"
        raise ValueError(message)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        message = "upstream selection ledger must be strict UTF-8 CSV"
        raise ValueError(message) from exc
    rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    if not rows:
        message = "upstream selection ledger is empty"
        raise ValueError(message)
    columns = rows[0]
    if columns != expected["required_columns"]:
        message = "upstream selection ledger columns or column order differ from the protocol"
        raise ValueError(message)
    normalized = _normalize_rows(rows[1:], expected)
    payload = _build_payload(normalized, expected)
    validate_embedded_selection_ledger(payload, contract)
    return payload


def validate_embedded_selection_ledger(
    payload: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> None:
    """Recompute every embedded summary without consulting a mutable source path."""

    expected = _validated_contract(contract)
    expected_payload_keys = {
        "schema_version",
        "source",
        "normalization",
        "summary",
        "trials",
    }
    if set(payload) != expected_payload_keys:
        message = "embedded upstream selection ledger has unsupported fields"
        raise ValueError(message)
    if payload.get("schema_version") != SELECTION_LEDGER_SCHEMA_VERSION:
        message = "embedded upstream selection ledger schema version is unsupported"
        raise ValueError(message)

    source = _mapping(payload.get("source"), field="upstream_selection_ledger.source")
    if source != {
        "filename": expected["source_filename"],
        "sha256": expected["expected_sha256"],
        "format": "rfc4180_csv_utf8",
        "columns": expected["required_columns"],
    }:
        message = "embedded upstream selection ledger source attestation differs"
        raise ValueError(message)
    normalization = _mapping(
        payload.get("normalization"), field="upstream_selection_ledger.normalization"
    )
    if normalization != {
        "identity_fields": expected["identity_fields"],
        "status_field": expected["status_field"],
        "sharpe_field": expected["sharpe_field"],
        "finite_inclusion_policy": _FINITE_INCLUSION_POLICY,
        "non_ok_sharpe_must_be_blank": True,
        "sample_standard_deviation_ddof": 1,
        "summary_decimal_places": expected["summary_decimal_places"],
    }:
        message = "embedded upstream selection ledger normalization contract differs"
        raise ValueError(message)

    raw_trials = payload.get("trials")
    if isinstance(raw_trials, (str, bytes)) or not isinstance(raw_trials, Sequence):
        message = "embedded upstream selection ledger trials must be a list"
        raise TypeError(message)
    expected_row_fields = [
        *_NORMALIZED_ROW_BASE_FIELDS,
        *expected["identity_fields"],
        expected["status_field"],
        expected["sharpe_field"],
    ]
    normalized: list[dict[str, Any]] = []
    identities: set[tuple[str, ...]] = set()
    for sequence, raw_row in enumerate(raw_trials):
        row = _mapping(raw_row, field=f"upstream_selection_ledger.trials[{sequence}]")
        if set(row) != set(expected_row_fields) or row.get("sequence") != sequence:
            message = "embedded upstream selection ledger rows or sequence are malformed"
            raise ValueError(message)
        identity = tuple(
            _strict_cell(row.get(field), field=f"trials[{sequence}].{field}")
            for field in expected["identity_fields"]
        )
        if identity in identities:
            message = f"embedded upstream selection ledger contains duplicate identity: {identity}"
            raise ValueError(message)
        identities.add(identity)
        status = _strict_cell(
            row.get(expected["status_field"]),
            field=f"trials[{sequence}].{expected['status_field']}",
        )
        raw_sharpe = row.get(expected["sharpe_field"])
        if raw_sharpe is not None and not isinstance(raw_sharpe, str):
            message = "embedded upstream selection ledger Sharpe values must be strings or null"
            raise TypeError(message)
        canonical_sharpe = (
            None
            if raw_sharpe is None
            else _canonical_finite_decimal(
                raw_sharpe,
                field=f"trials[{sequence}].{expected['sharpe_field']}",
            )
        )
        if canonical_sharpe != raw_sharpe:
            message = "embedded upstream selection ledger Sharpe value is not canonical"
            raise ValueError(message)
        if status != "ok" and canonical_sharpe is not None:
            message = "non-ok upstream trials must not contribute Sharpe dispersion"
            raise ValueError(message)
        normalized.append(dict(row))

    recomputed = _summary(normalized, expected)
    summary = _mapping(payload.get("summary"), field="upstream_selection_ledger.summary")
    if summary != recomputed:
        message = "embedded upstream selection ledger summary does not reconcile to its rows"
        raise ValueError(message)


def validate_upstream_selection_ledger_contract(contract: Mapping[str, Any]) -> None:
    """Validate the protocol contract without reading an external ledger."""

    _validated_contract(contract)


def _normalize_rows(
    rows: Sequence[Sequence[str]],
    contract: Mapping[str, Any],
) -> list[dict[str, Any]]:
    columns = contract["required_columns"]
    index = {column: ordinal for ordinal, column in enumerate(columns)}
    normalized: list[dict[str, Any]] = []
    identities: set[tuple[str, ...]] = set()
    for sequence, cells in enumerate(rows):
        if len(cells) != len(columns):
            message = (
                f"upstream selection ledger row has the wrong number of columns: row={sequence + 2}"
            )
            raise ValueError(message)
        identity = tuple(
            _strict_cell(cells[index[field]], field=f"row[{sequence}].{field}")
            for field in contract["identity_fields"]
        )
        if identity in identities:
            message = f"upstream selection ledger contains duplicate identity: {identity}"
            raise ValueError(message)
        identities.add(identity)
        status = _strict_cell(
            cells[index[contract["status_field"]]],
            field=f"row[{sequence}].{contract['status_field']}",
        )
        raw_sharpe = cells[index[contract["sharpe_field"]]]
        sharpe = (
            _canonical_finite_decimal(
                raw_sharpe,
                field=f"row[{sequence}].{contract['sharpe_field']}",
            )
            if raw_sharpe != ""
            else None
        )
        if status != "ok" and sharpe is not None:
            message = "non-ok upstream trials must not contribute Sharpe dispersion"
            raise ValueError(message)
        normalized.append(
            {
                "sequence": sequence,
                **dict(zip(contract["identity_fields"], identity, strict=True)),
                contract["status_field"]: status,
                contract["sharpe_field"]: sharpe,
            }
        )
    return normalized


def _build_payload(
    rows: list[dict[str, Any]],
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SELECTION_LEDGER_SCHEMA_VERSION,
        "source": {
            "filename": contract["source_filename"],
            "sha256": contract["expected_sha256"],
            "format": "rfc4180_csv_utf8",
            "columns": contract["required_columns"],
        },
        "normalization": {
            "identity_fields": contract["identity_fields"],
            "status_field": contract["status_field"],
            "sharpe_field": contract["sharpe_field"],
            "finite_inclusion_policy": _FINITE_INCLUSION_POLICY,
            "non_ok_sharpe_must_be_blank": True,
            "sample_standard_deviation_ddof": 1,
            "summary_decimal_places": contract["summary_decimal_places"],
        },
        "summary": _summary(rows, contract),
        "trials": rows,
    }


def _summary(rows: Sequence[Mapping[str, Any]], contract: Mapping[str, Any]) -> dict[str, Any]:
    status_field = contract["status_field"]
    sharpe_field = contract["sharpe_field"]
    status_counts = dict(sorted(Counter(str(row[status_field]) for row in rows).items()))
    sharpes = [Decimal(str(row[sharpe_field])) for row in rows if row[sharpe_field] is not None]
    if len(sharpes) < _MIN_DISPERSION_OBSERVATIONS:
        message = "upstream selection ledger requires at least two finite Sharpe observations"
        raise ValueError(message)
    with localcontext() as context:
        context.prec = 50
        count = Decimal(len(sharpes))
        mean = sum(sharpes) / count
        variance = sum((value - mean) ** 2 for value in sharpes) / Decimal(len(sharpes) - 1)
        sample_std = variance.sqrt()
    places = int(contract["summary_decimal_places"])
    summary = {
        "row_count": len(rows),
        "status_counts": status_counts,
        "finite_sharpe_count": len(sharpes),
        "finite_sharpe_mean": _fixed_decimal(mean, places),
        "finite_sharpe_sample_std": _fixed_decimal(sample_std, places),
    }
    expected_summary = {
        "row_count": contract["expected_row_count"],
        "status_counts": contract["expected_status_counts"],
        "finite_sharpe_count": contract["expected_finite_sharpe_count"],
        "finite_sharpe_sample_std": contract["expected_finite_sharpe_sample_std"],
    }
    for field, expected in expected_summary.items():
        if summary[field] != expected:
            message = f"upstream selection ledger {field} differs from the frozen protocol"
            raise ValueError(message)
    return summary


def _validated_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    parsed = dict(contract)
    required = {
        "schema_version",
        "source_filename",
        "expected_sha256",
        "required_columns",
        "identity_fields",
        "status_field",
        "expected_status_counts",
        "sharpe_field",
        "expected_row_count",
        "expected_finite_sharpe_count",
        "finite_sharpe_inclusion_policy",
        "non_ok_sharpe_must_be_blank",
        "sample_standard_deviation_ddof",
        "summary_decimal_places",
        "expected_finite_sharpe_sample_std",
        "resume_source",
    }
    if set(parsed) != required:
        message = "upstream selection ledger protocol contract has unsupported fields"
        raise ValueError(message)
    if parsed["schema_version"] != SELECTION_LEDGER_SCHEMA_VERSION:
        message = "upstream selection ledger protocol schema is unsupported"
        raise ValueError(message)
    if parsed["finite_sharpe_inclusion_policy"] != _FINITE_INCLUSION_POLICY:
        message = "upstream selection ledger finite-value inclusion policy is unsupported"
        raise ValueError(message)
    if parsed["non_ok_sharpe_must_be_blank"] is not True:
        message = "upstream selection ledger must exclude non-ok Sharpe values"
        raise ValueError(message)
    if parsed["sample_standard_deviation_ddof"] != 1:
        message = "upstream selection ledger dispersion must use sample ddof=1"
        raise ValueError(message)
    if parsed["resume_source"] != "embedded_manifest_only":
        message = "resumed campaigns must use the immutable embedded upstream ledger"
        raise ValueError(message)
    _normalize_contract_field_lists(parsed)
    _validate_contract_accounting(parsed)
    _validate_contract_digest_and_decimal(parsed)
    return parsed


def _normalize_contract_field_lists(parsed: dict[str, Any]) -> None:
    for key in ("required_columns", "identity_fields"):
        value = parsed[key]
        if (
            isinstance(value, (str, bytes))
            or not isinstance(value, Sequence)
            or not value
            or any(not isinstance(item, str) or not item for item in value)
            or len(value) != len(set(value))
        ):
            message = f"upstream selection ledger {key} must be unique non-empty strings"
            raise ValueError(message)
        parsed[key] = list(value)
    for field in (*parsed["identity_fields"], parsed["status_field"], parsed["sharpe_field"]):
        if field not in parsed["required_columns"]:
            message = f"upstream selection ledger field is absent from required_columns: {field}"
            raise ValueError(message)
    if parsed["identity_fields"] != ["python_file", "class_name", "symbol", "timeframe"]:
        message = "upstream selection ledger identity fields differ from the catalogue contract"
        raise ValueError(message)
    if parsed["status_field"] != "status" or parsed["sharpe_field"] != "sharpe_daily_ann":
        message = "upstream selection ledger status or Sharpe field differs"
        raise ValueError(message)


def _validate_contract_accounting(parsed: dict[str, Any]) -> None:
    status_counts = _mapping(parsed["expected_status_counts"], field="expected_status_counts")
    if set(status_counts) != {"ok", "error", "timeout"} or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in status_counts.values()
    ):
        message = "upstream selection ledger expected status counts are invalid"
        raise ValueError(message)
    parsed["expected_status_counts"] = dict(sorted(status_counts.items()))
    for field in ("expected_row_count", "expected_finite_sharpe_count"):
        value = parsed[field]
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < _MIN_DISPERSION_OBSERVATIONS
        ):
            message = f"upstream selection ledger {field} must be an integer >= 2"
            raise ValueError(message)
    if sum(status_counts.values()) != parsed["expected_row_count"]:
        message = "upstream selection ledger status counts do not sum to expected rows"
        raise ValueError(message)
    if parsed["expected_finite_sharpe_count"] > status_counts["ok"]:
        message = "finite upstream Sharpes cannot exceed ok trials"
        raise ValueError(message)


def _validate_contract_digest_and_decimal(parsed: dict[str, Any]) -> None:
    places = parsed["summary_decimal_places"]
    if (
        not isinstance(places, int)
        or isinstance(places, bool)
        or not _MIN_SUMMARY_DECIMAL_PLACES <= places <= _MAX_SUMMARY_DECIMAL_PLACES
    ):
        message = "upstream selection ledger summary_decimal_places must be 6..18"
        raise ValueError(message)
    if (
        not isinstance(parsed["expected_sha256"], str)
        or len(parsed["expected_sha256"]) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in parsed["expected_sha256"])
    ):
        message = "upstream selection ledger expected SHA-256 is invalid"
        raise ValueError(message)
    for field in ("source_filename", "expected_finite_sharpe_sample_std"):
        parsed[field] = _strict_cell(parsed[field], field=field)
    canonical_std = _canonical_finite_decimal(
        parsed["expected_finite_sharpe_sample_std"],
        field="expected_finite_sharpe_sample_std",
    )
    if canonical_std != parsed["expected_finite_sharpe_sample_std"]:
        message = "expected upstream Sharpe dispersion must use canonical decimal text"
        raise ValueError(message)


def _canonical_finite_decimal(value: str, *, field: str) -> str:
    if value != value.strip() or not value:
        message = f"{field} must be a non-empty canonical decimal"
        raise ValueError(message)
    try:
        numeric = Decimal(value)
    except InvalidOperation as exc:
        message = f"{field} must be a finite decimal"
        raise ValueError(message) from exc
    if not numeric.is_finite():
        message = f"{field} must be finite"
        raise ValueError(message)
    canonical = format(numeric, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if canonical in {"-0", "+0", ""}:
        canonical = "0"
    return canonical


def _fixed_decimal(value: Decimal, places: int) -> str:
    quantum = Decimal(1).scaleb(-places)
    return format(value.quantize(quantum), f".{places}f")


def _strict_cell(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        message = f"{field} must be a non-empty string without surrounding whitespace"
        raise ValueError(message)
    return value


def _mapping(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        message = f"{field} must be an object"
        raise TypeError(message)
    return dict(value)


__all__ = [
    "SELECTION_LEDGER_SCHEMA_VERSION",
    "load_upstream_selection_ledger",
    "validate_embedded_selection_ledger",
    "validate_upstream_selection_ledger_contract",
    "validated_prior_diagnostic_attempt_count",
]
