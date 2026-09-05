"""Production bar-timeframe and market-session timing primitives."""

from __future__ import annotations

from datetime import timedelta

from lib_common.asset_classes import (
    SESSION_BASED_ASSET_CLASSES,
    normalize_optional_asset_class,
)


class UnsupportedSessionTimingError(ValueError):
    """Raised when fixed elapsed time cannot represent a market-session bar."""


def timeframe_interval(timeframe: str) -> timedelta:
    """Resolve a canonical timeframe token to its exact duration."""

    token = timeframe.strip().lower()
    suffix = token[-1:] if token else ""
    if suffix not in {"m", "h", "d"}:
        msg = f"Unsupported timeframe: {timeframe}"
        raise ValueError(msg)
    try:
        value = int(token[:-1])
    except ValueError as exc:
        msg = f"Invalid timeframe: {timeframe}"
        raise ValueError(msg) from exc
    if value <= 0:
        msg = f"Timeframe interval must be positive: {timeframe}"
        raise ValueError(msg)
    if suffix == "m":
        return timedelta(minutes=value)
    if suffix == "h":
        return timedelta(hours=value)
    return timedelta(days=value)


def fixed_duration_interval(timeframe: str, *, asset_class: str | None) -> timedelta:
    """Return an interval only when elapsed time is a valid close-time model.

    Intraday bars can use fixed elapsed time. Daily and longer bars for
    session-based instruments require an authoritative open/close supplied by
    the provider or exchange calendar; a 24-hour substitution is not valid.
    """

    interval = timeframe_interval(timeframe)
    normalized_asset_class = normalize_optional_asset_class(asset_class)
    if interval >= timedelta(days=1) and normalized_asset_class in SESSION_BASED_ASSET_CLASSES:
        msg = (
            "Unsupported fixed-duration timing for session-based "
            f"asset_class={asset_class!r}, timeframe={timeframe!r}; "
            "authoritative bar open/close timestamps are required"
        )
        raise UnsupportedSessionTimingError(msg)
    return interval
