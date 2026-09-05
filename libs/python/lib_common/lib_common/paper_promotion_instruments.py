"""Content-addressed instrument authority for paper portfolio promotion."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from lib_common.hashing import canonical_json_hash, sha256_file

_REFERENCE_KEYS = frozenset({"path", "sha256"})
_DOCUMENT_KEYS = frozenset(
    {
        "schema_version",
        "strategy_id",
        "strategy_version",
        "model_configuration_sha256",
        "data_use_scope",
        "instruments",
    }
)
_ITEM_KEYS = frozenset({"instrument_id", "canonical_symbol"})


def _execution_symbol_key(value: str) -> str:
    return value.replace("-", "").replace("/", "").replace("_", "").strip().upper()


def _nonempty(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        msg = f"{field} must be a string"
        raise TypeError(msg)
    normalized = value.strip()
    if not normalized:
        msg = f"{field} must be non-empty"
        raise ValueError(msg)
    return normalized


def parse_instrument_items(raw: Any, *, field: str) -> dict[int, str]:
    if not isinstance(raw, list) or not raw:
        msg = f"{field} requires a non-empty instruments array"
        raise ValueError(msg)
    instruments: dict[int, str] = {}
    raw_symbols: set[str] = set()
    execution_symbols: set[str] = set()
    observed_order: list[int] = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != _ITEM_KEYS:
            msg = f"{field} entries require instrument_id and canonical_symbol"
            raise ValueError(msg)
        instrument_id = item.get("instrument_id")
        if isinstance(instrument_id, bool) or not isinstance(instrument_id, int):
            msg = f"{field} instrument_id must be a positive integer"
            raise TypeError(msg)
        if instrument_id <= 0:
            msg = f"{field} instrument_id must be a positive integer"
            raise ValueError(msg)
        raw_symbol = item.get("canonical_symbol")
        symbol = _nonempty(raw_symbol, field=f"instrument[{instrument_id}]")
        execution_symbol = _execution_symbol_key(symbol)
        if (
            symbol != raw_symbol
            or instrument_id in instruments
            or symbol in raw_symbols
            or execution_symbol in execution_symbols
        ):
            msg = f"{field} identities must be trimmed and unique"
            raise ValueError(msg)
        instruments[instrument_id] = symbol
        raw_symbols.add(symbol)
        execution_symbols.add(execution_symbol)
        observed_order.append(instrument_id)
    if observed_order != sorted(observed_order):
        msg = f"{field} entries must be ordered by instrument_id"
        raise ValueError(msg)
    return instruments


def paper_promotion_instrument_set_sha256(instruments: Mapping[int, str]) -> str:
    """Hash one exact, normalized-collision-free ID/symbol allowlist."""

    items = [
        {"instrument_id": instrument_id, "canonical_symbol": symbol}
        for instrument_id, symbol in sorted(instruments.items())
    ]
    validated = parse_instrument_items(items, field="instrument allowlist")
    rows = [
        {"instrument_id": instrument_id, "canonical_instrument": symbol}
        for instrument_id, symbol in sorted(validated.items())
    ]
    return canonical_json_hash({"schema": "paper-promotion-instrument-set-v1", "instruments": rows})


def resolve_promotion_artifact(
    artifact_root: Path,
    artifact_path: Path,
    *,
    field: str,
) -> tuple[Path, str]:
    try:
        resolved_root = artifact_root.resolve(strict=True)
        candidate = artifact_path if artifact_path.is_absolute() else artifact_root / artifact_path
        resolved_path = candidate.resolve(strict=True)
        relative = resolved_path.relative_to(resolved_root)
    except (FileNotFoundError, OSError, ValueError) as exc:
        msg = f"{field} must be an existing file below {artifact_root}"
        raise ValueError(msg) from exc
    if not resolved_path.is_file():
        msg = f"{field} must be a regular file"
        raise ValueError(msg)
    return resolved_path, relative.as_posix()


def load_instrument_set_document(
    path: Path,
    *,
    strategy_id: str,
    strategy_version: str,
    model_configuration_sha256: str,
) -> dict[int, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = "instrument-set artifact must be readable JSON"
        raise ValueError(msg) from exc
    if not isinstance(payload, Mapping) or set(payload) != _DOCUMENT_KEYS:
        msg = "instrument-set artifact fields do not match schema version 1"
        raise ValueError(msg)
    expected = {
        "schema_version": "1",
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "model_configuration_sha256": model_configuration_sha256,
        "data_use_scope": "paper_forward",
    }
    mismatches = sorted(key for key, value in expected.items() if payload.get(key) != value)
    if mismatches:
        msg = f"instrument-set artifact scope mismatch: {mismatches}"
        raise ValueError(msg)
    return parse_instrument_items(payload.get("instruments"), field="instrument-set artifact")


def validate_instrument_authority(  # noqa: PLR0911 - exact authority ledger
    payload: Mapping[str, Any],
    *,
    manifest_path: Path,
) -> tuple[dict[int, str], list[str]]:
    errors: list[str] = []
    raw_instruments = payload.get("authorized_instruments")
    reference = payload.get("instrument_set_artifact")
    if payload.get("model_scope") == "single_instrument":
        if raw_instruments != []:
            errors.append("single_instrument scope cannot claim authorized_instruments")
        if reference is not None:
            errors.append("single_instrument scope cannot claim instrument_set_artifact")
        return {}, errors

    try:
        instruments = parse_instrument_items(raw_instruments, field="authorized_instruments")
    except (TypeError, ValueError) as exc:
        errors.append(str(exc))
        instruments = {}
    if instruments and payload.get(
        "instrument_set_sha256"
    ) != paper_promotion_instrument_set_sha256(instruments):
        errors.append("instrument_set_sha256 does not match authorized_instruments")

    if not isinstance(reference, Mapping) or set(reference) != _REFERENCE_KEYS:
        errors.append("instrument_set_artifact must contain only path and sha256")
        return instruments, errors
    path_value = reference.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        errors.append("instrument_set_artifact.path must be non-empty")
        return instruments, errors
    relative_path = PurePosixPath(path_value)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        errors.append("instrument_set_artifact.path must remain below the manifest directory")
        return instruments, errors
    manifest_root = manifest_path.resolve().parent
    try:
        artifact_path = (manifest_root / Path(*relative_path.parts)).resolve(strict=True)
        artifact_path.relative_to(manifest_root)
    except (FileNotFoundError, OSError, ValueError):
        errors.append("instrument_set_artifact.path is missing or escapes the artifact root")
        return instruments, errors
    if not artifact_path.is_file():
        errors.append("instrument_set_artifact.path is not a regular file")
        return instruments, errors
    try:
        observed_digest = sha256_file(artifact_path)
    except OSError:
        errors.append("instrument_set_artifact.path cannot be hashed")
        return instruments, errors
    if reference.get("sha256") != observed_digest:
        errors.append("instrument_set_artifact.sha256 does not match the artifact")
    try:
        artifact_instruments = load_instrument_set_document(
            artifact_path,
            strategy_id=str(payload.get("strategy_id", "")),
            strategy_version=str(payload.get("strategy_version", "")),
            model_configuration_sha256=str(payload.get("model_configuration_sha256", "")),
        )
    except (TypeError, ValueError) as exc:
        errors.append(str(exc))
    else:
        if artifact_instruments != instruments:
            errors.append("instrument_set_artifact instruments do not match authorized_instruments")
    return instruments, errors


__all__ = [
    "load_instrument_set_document",
    "paper_promotion_instrument_set_sha256",
    "resolve_promotion_artifact",
    "validate_instrument_authority",
]
