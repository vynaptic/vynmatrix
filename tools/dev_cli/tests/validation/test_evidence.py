"""Contracts for the shared validation-evidence codec."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

import dev_cli.validation.evidence as evidence_module
from dev_cli.validation.evidence import (
    atomic_create_bytes,
    atomic_replace_bytes,
    canonical_json_bytes,
    evidence_sha256,
    parse_utc_datetime,
    utc_iso,
)


def test_canonical_json_is_order_independent_and_rejects_nonfinite_values() -> None:
    left = {"z": [1, 2], "a": {"value": 3.0}}
    right = {"a": {"value": 3.0}, "z": [1, 2]}

    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert evidence_sha256(left) == evidence_sha256(right)
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json_bytes({"value": float("nan")})
    with pytest.raises(TypeError, match="non-string object key"):
        canonical_json_bytes({1: "value"})


def test_utc_codec_normalizes_offsets_and_rejects_naive_values() -> None:
    offset = timezone(timedelta(hours=2))
    observed = parse_utc_datetime("2026-07-22T12:30:00+02:00", field="observed")

    assert observed == datetime(2026, 7, 22, 10, 30, tzinfo=UTC)
    assert utc_iso(datetime(2026, 7, 22, 12, 30, tzinfo=offset), field="observed") == (
        "2026-07-22T10:30:00Z"
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_utc_datetime("2026-07-22T12:30:00", field="observed")
    with pytest.raises(ValueError, match="must use UTC"):
        parse_utc_datetime(
            "2026-07-22T12:30:00+02:00",
            field="observed",
            strict_utc=True,
        )


def test_atomic_create_is_idempotent_and_rejects_different_bytes(tmp_path: Path) -> None:
    path = tmp_path / "immutable.json"

    atomic_create_bytes(path, b"first")
    atomic_create_bytes(path, b"first")

    assert path.read_bytes() == b"first"
    with pytest.raises(RuntimeError, match="different bytes"):
        atomic_create_bytes(path, b"second")


def test_atomic_create_detects_a_racing_different_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "raced.json"

    def race(_source: Path, destination: Path) -> None:
        destination.write_bytes(b"rival")
        raise FileExistsError

    monkeypatch.setattr(evidence_module.os, "link", race)

    with pytest.raises(RuntimeError, match="raced with different bytes"):
        atomic_create_bytes(path, b"candidate")
    assert path.read_bytes() == b"rival"


def test_atomic_replace_overwrites_mutable_artifacts(tmp_path: Path) -> None:
    path = tmp_path / "mutable.json"
    path.write_bytes(b"old")

    atomic_replace_bytes(path, b"new")

    assert path.read_bytes() == b"new"
