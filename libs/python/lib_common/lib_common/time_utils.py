"""Shared time parsing utilities."""

from __future__ import annotations

from datetime import UTC, datetime, time

_EXPECTED_HHMM_PARTS = 2


def now_utc() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Single source of truth for wall-clock UTC across the platform (and a single
    seam to inject a fixed clock in tests).
    """
    return datetime.now(tz=UTC)


def parse_hhmm(raw: str) -> time:
    """Parse an `HH:MM` time string."""
    parts = str(raw).strip().split(":")
    if len(parts) != _EXPECTED_HHMM_PARTS:
        msg = f"Expected HH:MM, got {raw!r}"
        raise ValueError(msg)
    return time(hour=int(parts[0]), minute=int(parts[1]))
