"""File hashing utilities.

Provides deterministic content hashing for artifact attestation
(e.g. paper-promotion evidence bundles).

Usage:
    from lib_common.hashing import sha256_file

    file_hash = sha256_file(Path("/path/to/model.pkl"))
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn


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
    """Return strict, deterministic UTF-8 JSON for identity material."""

    _validate_json(value, "identity")
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_json_hash(value: object) -> str:
    """Return the SHA-256 identity of strict canonical JSON material."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_file(path: Path, chunk_size: int = 8192) -> str:
    """Compute SHA-256 hash of a file.

    Reads file in chunks to handle large files efficiently without
    loading entire file into memory.

    Args:
        path: Path to file to hash
        chunk_size: Size of chunks to read (default 8KB)

    Returns:
        64-character hexadecimal hash string

    Raises:
        FileNotFoundError: If file does not exist
        PermissionError: If file cannot be read

    Example:
        >>> sha256_file(Path("/path/to/model.pkl"))
        'a1b2c3d4e5f6...'
    """
    sha256 = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


__all__ = ["canonical_json_bytes", "canonical_json_hash", "sha256_file"]
