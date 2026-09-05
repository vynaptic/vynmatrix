"""Canonical execution-mode-performance horizon buckets.

``mode_performance.horizon`` is constrained to ``intraday`` / ``swing`` /
``long_term``. Both the scoring store (which reads ModePerformance to rank
best/auto execution modes) and the feedback writer (which populates it from
realised outcomes) must bucket a horizon the SAME way, or a row written under one
bucket is never found by a query for another. This is the single source of truth
for that mapping.
"""

from __future__ import annotations

INTRADAY_MAX_HORIZON_DAYS = 1
SWING_MAX_HORIZON_DAYS = 7
MODE_HORIZON_BUCKETS = ("intraday", "swing", "long_term")
_DEFAULT_BUCKET = "swing"


def _bucket_for_days(days: float) -> str:
    if days <= INTRADAY_MAX_HORIZON_DAYS:
        return "intraday"
    if days > SWING_MAX_HORIZON_DAYS:
        return "long_term"
    return "swing"


def normalize_mode_horizon(horizon: str | None) -> str:
    """Map any horizon label to a ModePerformance bucket.

    Accepts the buckets themselves, numeric-day strings (``"1d"``, ``"14d"``),
    and the feedback evaluation horizons
    (``"15min"``/``"1h"``/``"4h"``/``"1d"``/``"1w"``/``"2w"``/``"1m"``).
    Anything unrecognised falls back to ``swing``.
    """
    if not horizon:
        return _DEFAULT_BUCKET
    token = horizon.strip().lower()
    explicit = {
        "intraday": "intraday",
        "swing": "swing",
        "long_term": "long_term",
        "15min": "intraday",
    }
    if token in explicit:
        return explicit[token]
    suffix, number = token[-1:], token[:-1]
    # Suffixes with a fixed bucket regardless of the count.
    fixed = {"h": "intraday", "m": "long_term"}  # hours -> intraday; months -> long_term
    if suffix in fixed:
        return fixed[suffix]
    if suffix not in ("d", "w"):
        return _DEFAULT_BUCKET
    try:
        days = int(number) * (7 if suffix == "w" else 1)
    except ValueError:
        return _DEFAULT_BUCKET
    return _bucket_for_days(days)
