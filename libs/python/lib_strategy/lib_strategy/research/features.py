"""Versioned, prefix-invariant features over already observed OHLCV bars.

``ts`` must be the dataset's established bar-availability time. This module
does not invent session calendars, fill gaps or certify uploaded provenance.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

FEATURE_SCHEMA = "ohlcv_causal_v1"
FEATURE_NAMES = (
    "log_return_1",
    "log_return_5",
    "log_return_20",
    "candle_body",
    "candle_range",
    "trailing_volatility_20",
    "relative_volume_20",
)
WARMUP_BARS = 20
MAX_BARS = 100_000
MAX_NUMERIC_TEXT = 128
MAX_TIMESTAMP_TEXT = 64
_BAR_FIELDS = frozenset({"ts", "open", "high", "low", "close", "volume"})
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


@dataclass(frozen=True)
class ResearchBar:
    ts: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def utc_timestamp(value: object) -> datetime:
    """Refuse timezone-free or non-UTC timestamps rather than guessing."""
    if not isinstance(value, str) or len(value) > MAX_TIMESTAMP_TEXT:
        message = "timestamp must be a UTC ISO string"
        raise ValueError(message)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        message = "timestamp must be a UTC ISO string"
        raise ValueError(message) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        message = "timestamp must explicitly use UTC"
        raise ValueError(message)
    return parsed.astimezone(UTC)


def _number(value: object) -> Decimal:
    if not isinstance(value, str) or len(value) > MAX_NUMERIC_TEXT or not _DECIMAL.fullmatch(value):
        message = "OHLCV values must be finite decimal strings"
        raise ValueError(message)
    try:
        decimal = Decimal(value)
        result = float(decimal)
    except (InvalidOperation, OverflowError, ValueError) as exc:
        message = "OHLCV values must be finite decimal strings"
        raise ValueError(message) from exc
    if not decimal.is_finite() or not math.isfinite(result) or (decimal != 0 and result == 0):
        message = "OHLCV values must be finite decimal strings"
        raise ValueError(message)
    return decimal


def parse_bars(bars: Sequence[Mapping[str, Any]]) -> list[ResearchBar]:
    """Validate bounded chronological bars without mutating input values."""
    if not isinstance(bars, (list, tuple)) or len(bars) > MAX_BARS:
        message = f"bars must be a sequence of at most {MAX_BARS} observations"
        raise ValueError(message)
    result: list[ResearchBar] = []
    for index, row in enumerate(bars):
        if not isinstance(row, Mapping) or set(row) != _BAR_FIELDS:
            message = f"bar {index} has an invalid OHLCV schema"
            raise ValueError(message)
        timestamp = utc_timestamp(row["ts"])
        if result and timestamp <= result[-1].timestamp:
            message = "bar timestamps must be strictly increasing"
            raise ValueError(message)
        values = {key: _number(row[key]) for key in _BAR_FIELDS if key != "ts"}
        valid_prices = min(values[key] for key in ("open", "high", "low", "close")) > 0
        valid_bounds = values["low"] <= min(values["open"], values["close"]) and values[
            "high"
        ] >= max(values["open"], values["close"])
        if not valid_prices or not valid_bounds or values["volume"] < 0:
            message = f"bar {index} violates OHLCV price, volume or bounds invariants"
            raise ValueError(message)
        result.append(
            ResearchBar(
                ts=timestamp.isoformat(),
                timestamp=timestamp,
                **{key: float(value) for key, value in values.items()},
            )
        )
    return result


def features_from_bars(bars: Sequence[ResearchBar]) -> list[dict[str, Any]]:
    """Use current/past bars only; zero-volume windows have zero relative volume."""
    logs = [math.log(bar.close) for bar in bars]
    returns = [0.0, *(logs[index] - logs[index - 1] for index in range(1, len(bars)))]
    result = []
    for index in range(WARMUP_BARS, len(bars)):
        bar = bars[index]
        trailing = returns[index - WARMUP_BARS + 1 : index + 1]
        mean = math.fsum(trailing) / WARMUP_BARS
        volume_mean = math.fsum(
            item.volume / WARMUP_BARS for item in bars[index - WARMUP_BARS + 1 : index + 1]
        )
        values = [
            returns[index],
            logs[index] - logs[index - 5],
            logs[index] - logs[index - WARMUP_BARS],
            (bar.close - bar.open) / bar.open,
            (bar.high - bar.low) / bar.close,
            math.sqrt(math.fsum((item - mean) ** 2 for item in trailing) / WARMUP_BARS),
            bar.volume / volume_mean - 1.0 if volume_mean else 0.0,
        ]
        if not all(math.isfinite(item) for item in values):
            message = f"bar {index} OHLCV features exceed finite numerical range"
            raise ValueError(message)
        result.append({"bar_index": index, "ts": bar.ts, "values": values})
    return result


def causal_features(bars: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return ordered feature rows after twenty warmup bars."""
    return features_from_bars(parse_bars(bars))
