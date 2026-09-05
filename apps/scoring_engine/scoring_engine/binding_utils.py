"""Shared helpers for user binding evaluation and execution selection."""

from __future__ import annotations

from collections.abc import Iterable


def resolve_execution_mode(
    preferred: str | None,
    allowed: Iterable[str] | None,
    policy: str | None,
) -> str | None:
    """Resolve a binding's execution mode from preferred/allowed/policy inputs.

    Precedence:
    1. ``preferred`` — an explicit single mode always wins (fixed).
    2. A ranking ``policy`` (anything other than ``fixed``) → ``"best"`` so
       ``evaluate_bindings`` ranks the permitted modes by ``ModePerformance``.
       This is checked BEFORE falling back to the first allowed mode, otherwise
       a multi-mode allow-list would always collapse to its first entry and
       policy-based ranking could never trigger (BD-3).
    3. The first allowed mode (single-mode allow-list with no policy).
    """
    if preferred:
        return preferred
    if policy and policy != "fixed":
        return "best"
    allowed_list = list(allowed or [])
    if allowed_list:
        return allowed_list[0]
    return None
