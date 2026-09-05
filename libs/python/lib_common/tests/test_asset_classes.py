from __future__ import annotations

import pytest

from lib_common.asset_classes import (
    CANONICAL_ASSET_CLASSES,
    REFERENCE_ONLY_ASSET_CLASSES,
    SESSION_BASED_ASSET_CLASSES,
    TRADABLE_ASSET_CLASSES,
    normalize_asset_class,
    normalize_optional_asset_class,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("FX", "fx"),
        ("forex", "fx"),
        ("commodity", "commodities"),
        ("commodities", "commodities"),
        ("ETF", "etf"),
        ("indices", "index"),
        ("index", "index"),
    ],
)
def test_asset_class_normalization_preserves_semantic_types(
    value: str,
    expected: str,
) -> None:
    assert normalize_asset_class(value) == expected


def test_asset_class_sets_express_reference_and_session_boundaries() -> None:
    assert {"index"} == REFERENCE_ONLY_ASSET_CLASSES
    assert CANONICAL_ASSET_CLASSES - {"index"} == TRADABLE_ASSET_CLASSES
    assert CANONICAL_ASSET_CLASSES - {"crypto"} == SESSION_BASED_ASSET_CLASSES


def test_asset_class_normalization_fails_closed() -> None:
    with pytest.raises(ValueError, match="Unsupported asset_class"):
        normalize_asset_class("stock")
    with pytest.raises(ValueError, match="must be non-empty"):
        normalize_asset_class("")
    assert normalize_optional_asset_class(None) is None
