"""Strict, bounded CSV and content identity for isolated research datasets.

The basic header is ``ts,open,high,low,close,volume``. The only extended header
appends ``bar_open,bar_close``. Timestamps require an explicit UTC offset.
Decimals are stored as strings; neither binary floats nor runtime prices own
the canonical content. Calendar authority is resolved by the application.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from lib_data.bars import ohlcv_invariant_error
from lib_data.dataset import timeframe_interval

MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_IMPORT_BARS = 100_000
MAX_VALIDATION_ERRORS = 20
MAX_TIMESTAMP_LENGTH = 40
UUID_LENGTH = 36
MAX_DECIMAL_LENGTH = 80
MAX_INTEGER_DIGITS = 20
MAX_FRACTION_DIGITS = 18
CSV_HEADER = ("ts", "open", "high", "low", "close", "volume")
TIMED_CSV_HEADER = (*CSV_HEADER, "bar_open", "bar_close")
_FIELDS = {
    "instrument_id",
    "source",
    "provenance",
    "currency",
    "timeframe",
    "timestamp_semantics",
    "adjustment_policy",
    "supersedes_id",
}
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


def validated_text(value: object, field: str, maximum: int) -> str:
    """Require bounded UTF-8 text that can be retained in PostgreSQL text/JSON."""
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        msg = f"{field} must be nonempty UTF-8 text of at most {maximum} characters"
        raise ValueError(msg)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        msg = f"{field} must be valid UTF-8 text"
        raise ValueError(msg) from exc
    return value.strip()


def utc_timestamp(value: str) -> datetime:
    """Parse only ISO-8601 timestamps with explicit timezone authority."""
    if not isinstance(value, str) or len(value) > MAX_TIMESTAMP_LENGTH or "T" not in value:
        msg = "timestamp must be ISO-8601 with an explicit timezone"
        raise ValueError(msg)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        msg = "timestamp must be ISO-8601 with an explicit timezone"
        raise ValueError(msg) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        msg = "timestamp timezone is required"
        raise ValueError(msg)
    try:
        return parsed.astimezone(UTC)
    except OverflowError as exc:
        msg = "timestamp exceeds the supported UTC timestamp range"
        raise ValueError(msg) from exc


def iso_timestamp(value: datetime) -> str:
    """Render a known UTC timestamp, including legacy UTC-naive price rows."""
    return (
        value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
        if value.tzinfo is None
        else value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    )


def canonical_json(value: Any) -> bytes:
    """Versioned research encoding; existing dataset digests remain unchanged."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def content_hash(metadata: Mapping[str, Any], bars: Sequence[dict[str, str]]) -> str:
    return hashlib.sha256(
        canonical_json({"schema": "research-bars-v1", "metadata": dict(metadata), "bars": bars})
    ).hexdigest()


def normalize_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Reject unknown identity and authority selectors rather than ignore them."""
    unknown = set(metadata) - _FIELDS
    if unknown:
        msg = f"Unknown metadata fields: {', '.join(sorted(unknown))}"
        raise ValueError(msg)
    missing = _FIELDS - {"supersedes_id"} - set(metadata)
    if missing:
        msg = f"Missing metadata fields: {', '.join(sorted(missing))}"
        raise ValueError(msg)
    result = dict(metadata)
    if type(result["instrument_id"]) is not int or result["instrument_id"] <= 0:
        msg = "instrument_id must be a positive integer"
        raise ValueError(msg)
    for field, limit in (("source", 50), ("provenance", 1000), ("currency", 10), ("timeframe", 20)):
        result[field] = validated_text(result[field], field, limit)
    if not re.fullmatch(r"[A-Z][A-Z0-9]{2,9}", result["currency"]):
        msg = "currency must be an explicit uppercase currency code"
        raise ValueError(msg)
    if not re.fullmatch(r"[1-9][0-9]*[mhd]", result["timeframe"]):
        msg = "timeframe must use canonical positive m/h/d units"
        raise ValueError(msg)
    try:
        timeframe_interval(result["timeframe"])
    except OverflowError as exc:
        msg = "timeframe exceeds supported duration"
        raise ValueError(msg) from exc
    if result["timestamp_semantics"] not in ("bar_open", "bar_close"):
        msg = "timestamp_semantics must be bar_open or bar_close"
        raise ValueError(msg)
    if result["adjustment_policy"] not in ("raw", "adjusted"):
        msg = "adjustment_policy must be raw or adjusted"
        raise ValueError(msg)
    if "supersedes_id" in result and (
        not isinstance(result["supersedes_id"], str) or len(result["supersedes_id"]) != UUID_LENGTH
    ):
        msg = "supersedes_id must identify an existing dataset"
        raise ValueError(msg)
    return result


def decimal_string(value: str) -> str:
    """Normalize a bounded exact decimal without rounding its significant digits."""
    if not value or len(value) > MAX_DECIMAL_LENGTH or _DECIMAL.fullmatch(value) is None:
        msg = "OHLCV must be finite decimal values"
        raise ValueError(msg)
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        msg = "OHLCV must be finite decimal values"
        raise ValueError(msg) from exc
    if not parsed.is_finite():
        msg = "OHLCV must be finite decimal values"
        raise ValueError(msg)
    exponent = parsed.as_tuple().exponent
    if (
        not isinstance(exponent, int)
        or parsed.adjusted() >= MAX_INTEGER_DIGITS
        or exponent < -MAX_FRACTION_DIGITS
    ):
        msg = "OHLCV exceeds 20 integer or 18 fractional digits"
        raise ValueError(msg)
    rendered = format(parsed, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if parsed == 0 else rendered


def _bar(row: list[str], header: tuple[str, ...], semantics: str) -> dict[str, str]:
    if len(row) != len(header):
        msg = "row field count differs from the exact header"
        raise ValueError(msg)
    values = dict(zip(header, row, strict=True))
    values["ts"] = iso_timestamp(utc_timestamp(values["ts"]))
    for name in CSV_HEADER[1:]:
        values[name] = decimal_string(values[name])
    error = ohlcv_invariant_error(
        open_price=float(values["open"]),
        high=float(values["high"]),
        low=float(values["low"]),
        close=float(values["close"]),
        volume=float(values["volume"]),
    )
    if error:
        raise ValueError(error)
    op, high, low, close = (Decimal(values[key]) for key in ("open", "high", "low", "close"))
    if (
        min(op, high, low, close) <= 0
        or low > min(op, close)
        or high < max(op, close)
        or low > high
    ):
        msg = "violates exact OHLC bounds"
        raise ValueError(msg)
    if "bar_open" in values:
        start, end = (utc_timestamp(values[key]) for key in ("bar_open", "bar_close"))
        if start >= end or utc_timestamp(values["ts"]) != (
            start if semantics == "bar_open" else end
        ):
            msg = "bar_open/bar_close contradict timestamp semantics"
            raise ValueError(msg)
        values.update(bar_open=iso_timestamp(start), bar_close=iso_timestamp(end))
    return values


def _chronology(
    ts: datetime, previous: datetime | None, timeframe: str, asset_class: str | None
) -> None:
    if previous is not None and ts <= previous:
        msg = "timestamps must be strictly increasing without duplicates"
        raise ValueError(msg)
    if (
        asset_class == "crypto"
        and previous is not None
        and ts - previous != timeframe_interval(timeframe)
    ):
        msg = "continuous-market cadence differs from timeframe"
        raise ValueError(msg)


def require_continuous_cadence(bars: Sequence[dict[str, str]], timeframe: str) -> None:
    """Audit parsed rows after application catalogue identity is resolved."""
    previous = None
    for bar in bars:
        ts = utc_timestamp(bar["ts"])
        _chronology(ts, previous, timeframe, "crypto")
        previous = ts


def parse_research_csv(
    content: bytes, metadata: Mapping[str, Any], *, asset_class: str | None = None
) -> dict[str, Any]:
    """Return a failed validation receipt for malformed rows, bounded before decode."""
    normalized = normalize_metadata(metadata)
    if len(content) > MAX_IMPORT_BYTES:
        msg = f"Import byte limit is {MAX_IMPORT_BYTES}"
        raise ValueError(msg)
    original_hash = hashlib.sha256(content).hexdigest()
    errors: list[dict[str, Any]] = []
    count = 0
    row_count = 0
    bars: list[dict[str, str]] = []

    def record(line: int, message: str) -> None:
        nonlocal count
        count += 1
        if len(errors) < MAX_VALIDATION_ERRORS:
            errors.append({"line": line, "error": message})

    try:
        reader = csv.reader(io.StringIO(content.decode("utf-8"), newline=""), strict=True)
        header = tuple(next(reader, []))
        if header not in {CSV_HEADER, TIMED_CSV_HEADER}:
            record(
                1,
                "CSV header must exactly match ts,open,high,low,close,volume[,bar_open,bar_close]",
            )
        else:
            previous: datetime | None = None
            for row_count, row in enumerate(reader, 1):
                if row_count > MAX_IMPORT_BARS:
                    record(reader.line_num, f"Import bar limit is {MAX_IMPORT_BARS}")
                    break
                try:
                    bar = _bar(row, header, normalized["timestamp_semantics"])
                    ts = utc_timestamp(bar["ts"])
                    _chronology(ts, previous, normalized["timeframe"], asset_class)
                    previous = ts
                    bars.append(bar)
                except ValueError as exc:
                    record(reader.line_num, str(exc))
            if row_count == 0:
                record(2, "CSV must contain at least one bar")
    except UnicodeDecodeError:
        record(1, "CSV must be valid UTF-8")
    except csv.Error:
        record(row_count + 2, "Malformed CSV quoting or oversized field")
    valid = count == 0
    return {
        "valid": valid,
        "original_hash": original_hash,
        "content_hash": content_hash(normalized, bars) if valid else None,
        "metadata": normalized,
        "bars": bars if valid else [],
        "report": {
            "bar_count": len(bars) if valid else 0,
            "row_count": row_count,
            "error_count": count,
            "errors": errors,
        },
    }
