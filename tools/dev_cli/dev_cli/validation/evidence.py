"""Canonical evidence encoding, UTC normalization, hashing, and atomic persistence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn


def _invalid_json(path: str, value: object) -> NoReturn:
    message = f"{path} contains unsupported JSON value {type(value).__name__}"
    raise TypeError(message)


def _validate_json(value: object, path: str) -> None:
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            message = f"{path} contains a non-finite float"
            raise ValueError(message)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                message = f"{path} contains a non-string object key"
                raise TypeError(message)
            _validate_json(item, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _validate_json(item, f"{path}[{index}]")
        return
    _invalid_json(path, value)


def canonical_json_bytes(value: object) -> bytes:
    """Return strict deterministic UTF-8 JSON bytes for one evidence value."""

    _validate_json(value, "evidence")
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def load_json_object(path: Path) -> dict[str, Any]:
    """Load one UTF-8 JSON object and reject every other root shape."""

    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        message = f"JSON root must be an object: {path}"
        raise TypeError(message)
    return decoded


def require_descendant(path: Path, root: Path, *, field: str) -> Path:
    """Resolve a path strictly below its required root or fail closed."""

    resolved_path = path.resolve()
    resolved_root = root.resolve()
    if resolved_path == resolved_root or resolved_root not in resolved_path.parents:
        message = f"{field} escapes its required root: {path}"
        raise ValueError(message)
    return resolved_path


def resolve_strategy_validation_artifact(
    repo_root: Path,
    *,
    expected_name: str,
    output: Path | None,
    field: str,
) -> Path:
    """Confine one named strategy-validation artifact below ``.artifacts``."""

    if not expected_name or Path(expected_name).name != expected_name:
        message = f"{field} expected filename is invalid: {expected_name!r}"
        raise ValueError(message)
    resolved_repo = repo_root.resolve()
    artifact_root = require_descendant(
        resolved_repo / ".artifacts",
        resolved_repo,
        field="artifact root",
    )
    candidate = (
        artifact_root / "research" / "strategy-validation" / expected_name
        if output is None
        else output
        if output.is_absolute()
        else resolved_repo / output
    )
    candidate = require_descendant(candidate, artifact_root, field=field)
    if candidate.name != expected_name:
        message = f"{field} must use its registered filename: {expected_name}"
        raise ValueError(message)
    return candidate


def evidence_sha256(value: object) -> str:
    """Hash the canonical evidence representation."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    """Hash a file without loading an unbounded payload into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def nonblank_string(value: object, *, field: str) -> str:
    """Return a stripped evidence string or fail closed."""

    if not isinstance(value, str) or not value.strip():
        message = f"{field} must be a non-blank string"
        raise ValueError(message)
    return value.strip()


def parse_utc_datetime(
    value: object,
    *,
    field: str,
    strict_utc: bool = False,
) -> datetime:
    """Parse an aware ISO-8601 value and normalize it to UTC.

    ``strict_utc`` rejects equivalent non-UTC offsets when an evidence contract
    requires the input itself, rather than only the represented instant, to be
    UTC.
    """

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            message = f"{field} must be an ISO-8601 timestamp"
            raise ValueError(message) from exc
    else:
        message = f"{field} must be a datetime or non-blank ISO-8601 timestamp"
        raise TypeError(message)
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None:
        message = f"{field} must be timezone-aware"
        raise ValueError(message)
    if strict_utc and offset.total_seconds() != 0:
        message = f"{field} must use UTC"
        raise ValueError(message)
    return parsed.astimezone(UTC)


def utc_iso(value: object, *, field: str) -> str:
    """Return canonical UTC ``Z`` notation for an aware timestamp."""

    return parse_utc_datetime(value, field=field).isoformat().replace("+00:00", "Z")


def atomic_replace_bytes(path: Path, payload: bytes) -> None:
    """Atomically replace a mutable artifact with exact bytes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_create_bytes(path: Path, payload: bytes, *, mode: int | None = None) -> None:
    """Create content once; optionally set its inode mode before publication."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            message = f"immutable artifact already exists with different bytes: {path}"
            raise RuntimeError(message)
        return
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            temporary.chmod(mode)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                message = f"immutable artifact raced with different bytes: {path}"
                raise RuntimeError(message) from None
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class ContentAddressedArtifact:
    """One immutable object and its manifest context."""

    role: str
    path: str
    sha256: str
    media_type: str
    row_count: int | None
    context: Mapping[str, Any]

    def as_manifest(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "path": self.path,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "row_count": self.row_count,
            "context": dict(self.context),
        }

    @classmethod
    def from_manifest(cls, value: Mapping[str, Any]) -> ContentAddressedArtifact:
        context = value.get("context")
        if not isinstance(context, Mapping):
            message = "artifact context must be an object"
            raise TypeError(message)
        row_count = value.get("row_count")
        if row_count is not None and (not isinstance(row_count, int) or row_count < 0):
            message = "artifact row_count must be a non-negative integer or null"
            raise ValueError(message)
        return cls(
            role=nonblank_string(value.get("role"), field="artifact.role"),
            path=nonblank_string(value.get("path"), field="artifact.path"),
            sha256=sha256_digest(value.get("sha256"), field="artifact.sha256"),
            media_type=nonblank_string(value.get("media_type"), field="artifact.media_type"),
            row_count=row_count,
            context=dict(context),
        )


def store_content_object(
    root: Path,
    content: bytes,
    *,
    suffix: str,
    role: str,
    media_type: str,
    row_count: int | None,
    context: Mapping[str, Any],
    object_directory: str = "objects",
) -> ContentAddressedArtifact:
    """Store immutable bytes below a digest-derived path."""

    digest = hashlib.sha256(content).hexdigest()
    relative = Path(object_directory) / digest[:2] / f"{digest}{suffix}"
    destination = root / relative
    atomic_create_bytes(destination, content)
    return ContentAddressedArtifact(
        role=role,
        path=relative.as_posix(),
        sha256=digest,
        media_type=media_type,
        row_count=row_count,
        context=dict(context),
    )


def verified_content_path(
    root: Path,
    artifact: ContentAddressedArtifact,
    *,
    object_directory: str = "objects",
) -> Path:
    """Resolve and hash-verify one content object below its declared root."""

    object_root = (root / object_directory).resolve()
    resolved = require_descendant(root / artifact.path, object_root, field="artifact")
    if not resolved.is_file() or file_sha256(resolved) != artifact.sha256:
        message = f"artifact hash differs: {artifact.path}"
        raise ValueError(message)
    if resolved.stem != artifact.sha256:
        message = f"artifact filename is not content-addressed: {artifact.path}"
        raise ValueError(message)
    return resolved


def content_manifest_artifacts(
    manifest: Mapping[str, Any],
    *,
    role: str | None = None,
) -> tuple[ContentAddressedArtifact, ...]:
    """Return typed artifact records, optionally filtered by exact role."""

    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, list):
        message = "manifest artifacts must be an array"
        raise TypeError(message)
    if not all(isinstance(value, Mapping) for value in raw_artifacts):
        message = "manifest contains a non-object artifact"
        raise TypeError(message)
    artifacts = tuple(ContentAddressedArtifact.from_manifest(value) for value in raw_artifacts)
    return tuple(artifact for artifact in artifacts if role is None or artifact.role == role)


def write_content_addressed_manifest(
    root: Path,
    payload: Mapping[str, Any],
    *,
    manifest_directory: str = "manifests",
) -> tuple[Path, str]:
    """Create one immutable manifest envelope named by its payload digest."""

    digest = evidence_sha256(payload)
    path = root / manifest_directory / f"{digest}.json"
    envelope = {"manifest_sha256": digest, "manifest": dict(payload)}
    atomic_create_bytes(path, canonical_json_bytes(envelope) + b"\n")
    return path, digest


def load_content_addressed_manifest(
    root: Path,
    manifest_path: Path,
    *,
    schema: str,
    manifest_directory: str = "manifests",
    object_directory: str = "objects",
    verify_artifacts: bool = True,
) -> dict[str, Any]:
    """Verify one exact manifest envelope and, optionally, all referenced objects."""

    manifest_root = (root / manifest_directory).resolve()
    resolved = require_descendant(manifest_path, manifest_root, field="manifest")
    envelope = load_json_object(resolved)
    payload = envelope.get("manifest")
    if not isinstance(payload, Mapping):
        message = "manifest payload must be an object"
        raise TypeError(message)
    if payload.get("schema") != schema:
        message = f"unsupported manifest schema: {payload.get('schema')!r}"
        raise ValueError(message)
    expected = sha256_digest(envelope.get("manifest_sha256"), field="manifest_sha256")
    if evidence_sha256(payload) != expected or resolved.stem != expected:
        message = "manifest content address differs"
        raise ValueError(message)
    if verify_artifacts:
        for artifact in content_manifest_artifacts(payload):
            verified_content_path(
                root,
                artifact,
                object_directory=object_directory,
            )
    return dict(payload)


def sha256_digest(value: object, *, field: str) -> str:
    """Return one validated lowercase SHA-256 digest."""

    digest = nonblank_string(value, field=field)
    if len(digest) != hashlib.sha256().digest_size * 2 or any(
        char not in "0123456789abcdef" for char in digest
    ):
        message = f"{field} must be a lowercase SHA-256 digest"
        raise ValueError(message)
    return digest
