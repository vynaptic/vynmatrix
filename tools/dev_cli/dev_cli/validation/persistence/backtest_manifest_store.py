"""Canonical, content-addressed storage for immutable validation manifests."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dev_cli.validation.evidence import atomic_create_bytes, canonical_json_bytes, evidence_sha256

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_SUBDIRECTORY = Path("research") / "manifests" / "sha256"


def canonical_manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """Serialize a manifest to one deterministic UTF-8 JSON representation."""

    if not isinstance(manifest, Mapping):
        msg = "manifest must be a mapping"
        raise TypeError(msg)
    return canonical_json_bytes(dict(manifest))


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """Return the digest of the canonical manifest representation."""

    if not isinstance(manifest, Mapping):
        msg = "manifest must be a mapping"
        raise TypeError(msg)
    return evidence_sha256(dict(manifest))


def validate_sha256_digest(digest: object, *, field: str = "manifest digest") -> None:
    """Validate the lowercase content-address used by files and trial rows."""

    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        msg = f"{field} must be a lowercase SHA-256 hex string"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ManifestReference:
    """Content-addressed reference returned after immutable storage."""

    sha256: str
    path: Path
    size_bytes: int


class BacktestManifestStore:
    """Exclusively publish canonical manifests beneath an artifact root.

    Pass the repository's ``.artifacts`` directory in production. Tests and
    isolated runners can provide their own artifact root without changing the
    deterministic relative layout.
    """

    def __init__(self, artifact_root: Path) -> None:
        self._root = artifact_root.resolve()

    def path_for(self, digest: str) -> Path:
        """Resolve a validated digest to its canonical content path."""

        validate_sha256_digest(digest)
        return self._root / _MANIFEST_SUBDIRECTORY / digest[:2] / f"{digest}.json"

    def store(self, manifest: Mapping[str, Any]) -> ManifestReference:
        """Atomically create or verify an immutable content-addressed manifest."""

        payload = canonical_manifest_bytes(manifest)
        digest = hashlib.sha256(payload).hexdigest()
        destination = self.path_for(digest)
        try:
            atomic_create_bytes(destination, payload, mode=0o444)
        except RuntimeError:
            self._verify_payload(destination, digest, payload)

        return ManifestReference(digest, destination, len(payload))

    def read(self, digest: str) -> dict[str, Any]:
        """Read a stored manifest after verifying its path, hash, and encoding."""

        path = self.path_for(digest)
        payload = path.read_bytes()
        actual = hashlib.sha256(payload).hexdigest()
        if actual != digest:
            msg = f"manifest content hash mismatch: expected {digest}, found {actual}"
            raise ValueError(msg)
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            msg = "stored manifest root must be a JSON object"
            raise TypeError(msg)
        if canonical_manifest_bytes(decoded) != payload:
            msg = "stored manifest is not canonically serialized"
            raise ValueError(msg)
        return decoded

    @staticmethod
    def _verify_payload(path: Path, digest: str, expected: bytes) -> None:
        existing = path.read_bytes()
        actual = hashlib.sha256(existing).hexdigest()
        if actual != digest or existing != expected:
            msg = f"immutable manifest artifact is corrupt: {path}"
            raise ValueError(msg)


__all__ = [
    "BacktestManifestStore",
    "ManifestReference",
    "canonical_manifest_bytes",
    "manifest_sha256",
    "validate_sha256_digest",
]
