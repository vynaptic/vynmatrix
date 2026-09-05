"""Shared normalization helpers for metrics services."""

from __future__ import annotations

from typing import Any

_SIDE_NORMALIZATION: dict[str, str] = {
    "buy": "buy",
    "sell": "sell",
    "long": "buy",
    "short": "sell",
    "up": "buy",
    "down": "sell",
    "bid": "buy",
    "ask": "sell",
    "open_long": "buy",
    "close_long": "sell",
    "open_short": "sell",
    "close_short": "buy",
}

_POSITION_SIDE_NORMALIZATION: dict[str, str] = {
    "buy": "long",
    "long": "long",
    "sell": "short",
    "short": "short",
}


def normalize_side(side: Any) -> str:
    """Normalize trade side to ``buy`` or ``sell``, rejecting unknown economics."""
    if not isinstance(side, str):
        msg = f"Trade side must be a string, got {type(side).__name__}"
        raise TypeError(msg)

    normalized = _SIDE_NORMALIZATION.get(side.lower().strip())
    if normalized:
        return normalized

    msg = f"Unknown trade side: {side!r}"
    raise ValueError(msg)


def normalize_position_side(side: Any) -> str:
    """Normalize held-position direction, rejecting missing or ambiguous values.

    Position direction is intentionally narrower than trade direction: values
    such as ``close_short`` describe an order action, not the economic direction
    of the position currently held.
    """
    if not isinstance(side, str):
        msg = f"Position side must be a string, got {type(side).__name__}"
        raise TypeError(msg)

    normalized = _POSITION_SIDE_NORMALIZATION.get(side.lower().strip())
    if normalized:
        return normalized

    msg = f"Unknown position side: {side!r}"
    raise ValueError(msg)
