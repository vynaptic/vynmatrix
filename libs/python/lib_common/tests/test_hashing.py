"""Tests for strict canonical identity hashing."""

from __future__ import annotations

import math

import pytest

from lib_common.hashing import canonical_json_bytes, canonical_json_hash


def test_canonical_json_identity_is_mapping_order_invariant() -> None:
    left = {"b": [2, 3], "a": {"value": 1}}
    right = {"a": {"value": 1}, "b": [2, 3]}

    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert canonical_json_hash(left) == canonical_json_hash(right)
    assert len(canonical_json_hash(left)) == 64


def test_canonical_json_identity_rejects_nonfinite_and_nonstring_keys() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json_hash({"value": math.nan})
    with pytest.raises(TypeError, match="non-string"):
        canonical_json_hash({1: "not-canonical"})
