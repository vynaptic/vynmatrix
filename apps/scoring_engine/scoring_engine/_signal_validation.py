"""Pure helpers for signal-ingest validation and conversion.

Extracted from ``api.py`` so the route handlers can stay focused on
orchestration. Everything here is a pure function with no FastAPI or
``ScoreEngine`` dependencies — easy to unit-test in isolation.

Validation operations used by the scoring HTTP routes:

* :func:`validate_signal_for_persistence` — convert ``SignalAction`` to the
  lowercase string the DB ``CHECK`` constraint accepts.
* :func:`insight_direction_to_action` — translate insight direction
  strings to canonical ``SignalAction`` enums.
* :func:`expected_return_from_insight_payload` — derive ``expected_return`` for
  the strategy wire payload without masking TP/SL-derived estimates.
"""

from __future__ import annotations

from typing import Any

from lib_strategy.signals.normalization import normalize_scoring_action
from lib_strategy.signals.signal import SignalAction

from .schemas import VALID_DB_ACTIONS, InsightSignalPayload


def validate_signal_for_persistence(
    action: SignalAction,
) -> str:
    """Validate and convert ``SignalAction`` to a DB-safe action string.

    Args:
        action: The ``SignalAction`` enum value.
    Returns:
        Lowercase action string safe for DB persistence.

    Raises:
        ValueError: If ``strict=True`` and the action is not persistable.
    """
    db_action = normalize_scoring_action(action)
    if db_action not in VALID_DB_ACTIONS:
        msg = (
            f"Action '{action}' normalizes to '{db_action}' "
            f"which is not in valid DB actions: {VALID_DB_ACTIONS}"
        )
        raise ValueError(msg)
    return str(db_action)


def insight_direction_to_action(direction: str) -> SignalAction:
    """Convert an insight direction string to a canonical action.

    ``Up`` → ``LONG``, ``Down`` → ``SHORT``, ``Flat`` → ``CLOSE``.
    """
    mapping = {
        "Up": SignalAction.LONG,
        "Down": SignalAction.SHORT,
        "Flat": SignalAction.CLOSE,
    }
    try:
        return mapping[direction]
    except KeyError as exc:
        msg = f"Unknown insight direction: {direction!r}"
        raise ValueError(msg) from exc


def expected_return_from_insight_payload(
    payload: InsightSignalPayload,
    context: dict[str, Any],
) -> float | None:
    """Resolve ``expected_return`` from a strategy insight payload.

    ``HttpSignalEmitter`` emits ``insight.magnitude=0.0`` when a strategy
    does not explicitly set ``expected_return`` — which is the entire
    deployable indicator fleet (the convenience emit helpers expose no
    ``expected_return`` parameter). Persisting that placeholder zero collapses
    ``s_raw`` and the asset score to 0, so the signal can never clear a binding
    threshold and autopilot never triggers.

    Therefore: prefer ``context['expected_return']`` when present; otherwise,
    a placeholder ``insight.magnitude == 0.0`` always returns ``None`` so the
    scoring adapter derives ``mu``/``sigma`` itself — from the price ladder
    (full or entry+stop) when available, else a confidence-based synthesis.
    Only a genuinely non-zero magnitude is taken at face value.
    """
    explicit = context.get("expected_return")
    if explicit is not None:
        return float(explicit)

    magnitude = float(payload.insight.magnitude)
    if magnitude == 0.0:
        return None
    return magnitude


__all__ = [
    "expected_return_from_insight_payload",
    "insight_direction_to_action",
    "validate_signal_for_persistence",
]
