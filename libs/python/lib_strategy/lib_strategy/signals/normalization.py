"""Signal action normalization helpers.

Keeps execution-facing actions canonical (SignalAction) while allowing
lowercase scoring actions for persistence constraints.
"""

from __future__ import annotations

from typing import Any

from lib_strategy.signals.signal import SignalAction

_CANONICAL_ACTIONS = {
    "long": SignalAction.LONG,
    "buy": SignalAction.LONG,
    "up": SignalAction.LONG,
    "bull": SignalAction.LONG,
    "open": SignalAction.LONG,
    "open_long": SignalAction.LONG,
    "open_spread": SignalAction.LONG,
    "short": SignalAction.SHORT,
    "sell": SignalAction.SHORT,
    "down": SignalAction.SHORT,
    "bear": SignalAction.SHORT,
    "open_short": SignalAction.SHORT,
    "close": SignalAction.CLOSE,
    "close_signal": SignalAction.CLOSE,
    "exit": SignalAction.CLOSE,
    "flat": SignalAction.CLOSE,
    "close_long": SignalAction.CLOSE,
    "close_short": SignalAction.CLOSE,
    "close_spread": SignalAction.CLOSE,
    "hold": SignalAction.HOLD,
    "neutral": SignalAction.HOLD,
    "none": SignalAction.HOLD,
}


_SCORING_ACTIONS = {
    "long",
    "short",
    "flat",
    "hold",
    "open_spread",
    "close_spread",
}

_SCORING_ACTION_ALIASES = {
    "buy": "long",
    "up": "long",
    "bull": "long",
    "open": "long",
    "open_long": "long",
    "sell": "short",
    "down": "short",
    "bear": "short",
    "open_short": "short",
    "exit": "flat",
    "close": "flat",
    "close_signal": "flat",
    "close_long": "flat",
    "close_short": "flat",
    "neutral": "hold",
    "none": "hold",
}


def normalize_signal_action(value: Any) -> SignalAction:
    """Normalize input into canonical SignalAction values."""
    if isinstance(value, SignalAction):
        return value

    if value is None:
        return SignalAction.HOLD

    raw = getattr(value, "value", value)
    if not isinstance(raw, str):
        raw = str(raw)

    normalized = raw.strip().lower()
    return _CANONICAL_ACTIONS.get(normalized, SignalAction.HOLD)


def normalize_scoring_action(value: Any) -> str:
    """Normalize input into lowercase scoring actions allowed by the DB."""
    if isinstance(value, SignalAction):
        return {
            SignalAction.LONG: "long",
            SignalAction.SHORT: "short",
            SignalAction.CLOSE: "flat",
            SignalAction.HOLD: "hold",
        }[value]

    if value is None:
        return "hold"

    raw = getattr(value, "value", value)
    if not isinstance(raw, str):
        raw = str(raw)

    normalized = raw.strip().lower()
    if normalized in _SCORING_ACTIONS:
        return normalized

    mapped = _SCORING_ACTION_ALIASES.get(normalized)

    return mapped if mapped in _SCORING_ACTIONS else "hold"


def is_known_signal_action(value: Any) -> bool:
    """Return True if the value maps to a known signal action."""
    if isinstance(value, SignalAction):
        return True
    if value is None:
        return False
    raw = getattr(value, "value", value)
    if not isinstance(raw, str):
        raw = str(raw)
    normalized = raw.strip().lower()
    return normalized in _CANONICAL_ACTIONS
