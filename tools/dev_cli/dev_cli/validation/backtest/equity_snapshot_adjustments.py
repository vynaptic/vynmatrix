"""Corporate-action completeness checks for immutable equity snapshots."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import date
from pathlib import Path


class SnapshotAdjustmentEvidenceError(ValueError):
    """Snapshot bars or actions cannot prove safe adjustment inputs."""


def read_bar_sessions(paths: Sequence[Path]) -> set[date]:
    """Read unique ISO session labels from verified bar artifacts."""

    sessions: set[date] = set()
    for path in paths:
        with path.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None or "session" not in reader.fieldnames:
                msg = f"bar artifact lacks session column: {path}"
                raise SnapshotAdjustmentEvidenceError(msg)
            for row in reader:
                session = date.fromisoformat(row["session"])
                if session in sessions:
                    msg = f"duplicate session across bar artifacts: {session}"
                    raise SnapshotAdjustmentEvidenceError(msg)
                sessions.add(session)
    return sessions


def unanchored_dividend_dates(
    *,
    bar_paths: Sequence[Path],
    action_paths: Sequence[Path],
) -> tuple[date, ...]:
    """Return dividend ex-dates lacking a strictly earlier raw close."""

    sessions = read_bar_sessions(bar_paths)
    dividends: set[date] = set()
    for path in action_paths:
        with path.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            required = {"action_type", "ex_date"}
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                msg = f"action artifact lacks required columns: {path}"
                raise SnapshotAdjustmentEvidenceError(msg)
            dividends.update(
                date.fromisoformat(row["ex_date"])
                for row in reader
                if row["action_type"] == "dividend"
            )
    return tuple(sorted(ex_date for ex_date in dividends if not any(s < ex_date for s in sessions)))


__all__ = [
    "SnapshotAdjustmentEvidenceError",
    "read_bar_sessions",
    "unanchored_dividend_dates",
]
