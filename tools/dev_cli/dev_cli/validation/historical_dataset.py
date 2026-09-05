"""Deterministic historical-dataset quality and content-digest contracts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from lib_data.bars import Bar, ohlcv_invariant_error
from lib_data.dataset import timeframe_interval

_MISSING_SAMPLE_LIMIT = 20


@dataclass(frozen=True)
class DatasetAudit:
    """Quality summary for one symbol/source/timeframe and half-open interval."""

    symbol: str
    source: str
    timeframe: str
    start: datetime
    end: datetime
    interval_seconds: int
    expected_rows: int
    observed_rows: int
    unique_in_range_rows: int
    duplicate_timestamps: int
    missing_timestamps: int
    unexpected_timestamps: int
    malformed_rows: int
    coverage: float
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    missing_timestamp_sample: tuple[datetime, ...]
    ohlcv_sha256: str

    @property
    def is_well_formed(self) -> bool:
        return (
            self.duplicate_timestamps == 0
            and self.unexpected_timestamps == 0
            and self.malformed_rows == 0
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "source": self.source,
            "timeframe": self.timeframe,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "interval_seconds": self.interval_seconds,
            "expected_rows": self.expected_rows,
            "observed_rows": self.observed_rows,
            "unique_in_range_rows": self.unique_in_range_rows,
            "duplicate_timestamps": self.duplicate_timestamps,
            "missing_timestamps": self.missing_timestamps,
            "unexpected_timestamps": self.unexpected_timestamps,
            "malformed_rows": self.malformed_rows,
            "coverage": self.coverage,
            "first_timestamp": (
                self.first_timestamp.isoformat() if self.first_timestamp is not None else None
            ),
            "last_timestamp": (
                self.last_timestamp.isoformat() if self.last_timestamp is not None else None
            ),
            "missing_timestamp_sample": [ts.isoformat() for ts in self.missing_timestamp_sample],
            "ohlcv_sha256": self.ohlcv_sha256,
        }


class DatasetValidationError(ValueError):
    """Raised when a dataset cannot satisfy its frozen quality contract."""

    def __init__(self, message: str, *, audit: DatasetAudit) -> None:
        super().__init__(message)
        self.audit = audit


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"Dataset {field} must be timezone-aware"
        raise ValueError(msg)
    return value.astimezone(UTC)


def _valid_bar(bar: Bar) -> bool:
    return (
        ohlcv_invariant_error(
            open_price=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        is None
    )


def _digest(bars: list[Bar]) -> str:
    digest = hashlib.sha256()
    records: list[str] = []
    for bar in bars:
        timestamp = (
            bar.timestamp.astimezone(UTC).isoformat(timespec="microseconds")
            if bar.timestamp.tzinfo is not None and bar.timestamp.utcoffset() is not None
            else f"naive:{bar.timestamp.isoformat(timespec='microseconds')}"
        )
        values = (
            bar.symbol,
            timestamp,
            bar.timeframe,
            bar.source or "",
            format(bar.open, ".17g"),
            format(bar.high, ".17g"),
            format(bar.low, ".17g"),
            format(bar.close, ".17g"),
            format(bar.volume, ".17g"),
        )
        records.append("\x1f".join(values))
    for record in sorted(records):
        digest.update(record.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def audit_bars(
    bars: list[Bar],
    *,
    symbol: str,
    source: str,
    timeframe: str,
    start: datetime,
    end: datetime,
) -> DatasetAudit:
    """Audit bars against an exact 24/7 interval without mutating or filling data."""

    start_utc = _utc(start, field="start")
    end_utc = _utc(end, field="end")
    if start_utc >= end_utc:
        msg = "Dataset interval requires start < end"
        raise ValueError(msg)
    interval = timeframe_interval(timeframe)
    interval_seconds = int(interval.total_seconds())
    span_seconds = int((end_utc - start_utc).total_seconds())
    if span_seconds % interval_seconds:
        msg = "Dataset interval must contain a whole number of timeframe periods"
        raise ValueError(msg)
    expected_rows = span_seconds // interval_seconds
    expected_timestamp_set: set[datetime] = set()
    cursor = start_utc
    while cursor < end_utc:
        expected_timestamp_set.add(cursor)
        cursor += interval

    timestamps: list[datetime] = []
    malformed = 0
    unexpected = 0
    expected_symbol = symbol.strip().upper()
    expected_source = source.strip()
    expected_timeframe = timeframe.strip().lower()
    for bar in bars:
        try:
            timestamp = _utc(bar.timestamp, field="bar timestamp")
        except ValueError:
            malformed += 1
            continue
        timestamps.append(timestamp)
        if timestamp not in expected_timestamp_set:
            unexpected += 1
        if (
            bar.symbol.strip().upper() != expected_symbol
            or (bar.source or "").strip() != expected_source
            or bar.timeframe.strip().lower() != expected_timeframe
            or not _valid_bar(bar)
        ):
            malformed += 1

    unique_timestamps = set(timestamps)
    duplicate_count = len(timestamps) - len(unique_timestamps)
    in_range = unique_timestamps & expected_timestamp_set
    missing = sorted(expected_timestamp_set - unique_timestamps)
    return DatasetAudit(
        symbol=expected_symbol,
        source=expected_source,
        timeframe=expected_timeframe,
        start=start_utc,
        end=end_utc,
        interval_seconds=interval_seconds,
        expected_rows=expected_rows,
        observed_rows=len(bars),
        unique_in_range_rows=len(in_range),
        duplicate_timestamps=duplicate_count,
        missing_timestamps=len(missing),
        unexpected_timestamps=unexpected,
        malformed_rows=malformed,
        coverage=(len(in_range) / expected_rows if expected_rows else 0.0),
        first_timestamp=min(timestamps) if timestamps else None,
        last_timestamp=max(timestamps) if timestamps else None,
        missing_timestamp_sample=tuple(missing[:_MISSING_SAMPLE_LIMIT]),
        ohlcv_sha256=_digest(bars),
    )


def validate_bars(
    bars: list[Bar],
    *,
    symbol: str,
    source: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    minimum_coverage: float = 1.0,
) -> DatasetAudit:
    """Return an audit or fail closed on malformed data and insufficient coverage."""

    if not 0.0 <= minimum_coverage <= 1.0:
        msg = "minimum_coverage must be between 0 and 1"
        raise ValueError(msg)
    audit = audit_bars(
        bars,
        symbol=symbol,
        source=source,
        timeframe=timeframe,
        start=start,
        end=end,
    )
    if not audit.is_well_formed or audit.coverage < minimum_coverage:
        msg = (
            "Dataset quality gate failed: "
            f"coverage={audit.coverage:.6f} required={minimum_coverage:.6f} "
            f"duplicates={audit.duplicate_timestamps} "
            f"unexpected={audit.unexpected_timestamps} malformed={audit.malformed_rows}"
        )
        raise DatasetValidationError(msg, audit=audit)
    return audit
