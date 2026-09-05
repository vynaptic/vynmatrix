"""Canonical platform asset-class taxonomy.

The values in this module are persistence and event-contract values. Venue
wire types (for example Saxo ``StockIndexOption`` or IBKR ``IND``) remain
adapter concerns and must be mapped to one of these canonical values at the
boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

CANONICAL_ASSET_CLASS_VALUES: Final[tuple[str, ...]] = (
    "crypto",
    "equity",
    "etf",
    "index",
    "futures",
    "options",
    "fx",
    "commodities",
)
CANONICAL_ASSET_CLASSES: Final[frozenset[str]] = frozenset(CANONICAL_ASSET_CLASS_VALUES)

# These are lexical aliases only. An ETF or index is not an equity alias:
# preserving that distinction is required for reference-only index admission
# and correct instrument-level accounting.
ASSET_CLASS_ALIASES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "commodity": "commodities",
        "forex": "fx",
        "indices": "index",
    }
)

REFERENCE_ONLY_ASSET_CLASSES: Final[frozenset[str]] = frozenset({"index"})
TRADABLE_ASSET_CLASSES: Final[frozenset[str]] = (
    CANONICAL_ASSET_CLASSES - REFERENCE_ONLY_ASSET_CLASSES
)
SESSION_BASED_ASSET_CLASSES: Final[frozenset[str]] = CANONICAL_ASSET_CLASSES - {"crypto"}

_SQL_VALUES = ", ".join(f"'{value}'" for value in CANONICAL_ASSET_CLASS_VALUES)
ASSET_CLASS_CHECK_SQL: Final[str] = f"asset_class IN ({_SQL_VALUES})"
NULLABLE_ASSET_CLASS_CHECK_SQL: Final[str] = (
    f"asset_class IS NULL OR asset_class IN ({_SQL_VALUES})"
)


def normalize_asset_class(value: str, *, field_name: str = "asset_class") -> str:
    """Return one canonical asset class, accepting only documented aliases."""

    normalized = str(value or "").strip().lower()
    if not normalized:
        msg = f"{field_name} must be non-empty"
        raise ValueError(msg)
    canonical = ASSET_CLASS_ALIASES.get(normalized, normalized)
    if canonical not in CANONICAL_ASSET_CLASSES:
        expected = ", ".join(CANONICAL_ASSET_CLASS_VALUES)
        msg = f"Unsupported {field_name}: {value!r}; expected one of: {expected}"
        raise ValueError(msg)
    return canonical


def normalize_optional_asset_class(
    value: str | None,
    *,
    field_name: str = "asset_class",
) -> str | None:
    """Normalize an optional asset class without inventing a default."""

    if value is None:
        return None
    return normalize_asset_class(value, field_name=field_name)


__all__ = [
    "ASSET_CLASS_ALIASES",
    "ASSET_CLASS_CHECK_SQL",
    "CANONICAL_ASSET_CLASSES",
    "CANONICAL_ASSET_CLASS_VALUES",
    "NULLABLE_ASSET_CLASS_CHECK_SQL",
    "REFERENCE_ONLY_ASSET_CLASSES",
    "SESSION_BASED_ASSET_CLASSES",
    "TRADABLE_ASSET_CLASSES",
    "normalize_asset_class",
    "normalize_optional_asset_class",
]
