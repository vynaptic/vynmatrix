"""Bar consolidation with period-close emission semantics.

Consolidates minute bars into N-minute bars stamped at the period CLOSE
boundary (e.g. 09:15, 09:30, 09:45 for 15-minute bars) — the single
production convention shared by the live SignalWorker and the backtest harness.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from .bars import Bar, ohlcv_invariant_error
from .dataset import timeframe_interval


def _period_end(ts: datetime, period_minutes: int) -> datetime:
    """Compute the inclusive period-close boundary for an end-time bar timestamp."""
    total_minutes = ts.hour * 60 + ts.minute
    remainder = total_minutes % period_minutes
    period_end_minutes = (
        total_minutes if remainder == 0 else total_minutes + (period_minutes - remainder)
    )
    base = ts.replace(hour=0, minute=0, second=0, microsecond=0)
    return base + timedelta(minutes=period_end_minutes)


def _bar_close_time(bar: Bar) -> datetime:
    """Return the logical close timestamp for a bar stored at start time."""
    return bar.timestamp + timeframe_interval(bar.timeframe)


def _timeframe_minutes(timeframe: str | None) -> int:
    """Return source-bar minutes, defaulting only a blank timeframe to one."""
    if not timeframe or not timeframe.strip():
        return 1
    interval_seconds = timeframe_interval(timeframe).total_seconds()
    if interval_seconds % 60:
        msg = f"source timeframe must resolve to whole minutes: {timeframe!r}"
        raise ValueError(msg)
    return int(interval_seconds // 60)


def _source_metadata(bar: Bar) -> dict[str, object]:
    """Return provenance for the exact last persisted constituent bar."""
    metadata: dict[str, object] = dict(bar.metadata or {})
    timestamp = bar.timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    else:
        timestamp = timestamp.astimezone(UTC)
    metadata["source_price_ts"] = timestamp.isoformat()
    metadata["source_timeframe"] = bar.timeframe
    for key in ("price_id", "content_revision"):
        if key not in bar.metadata:
            metadata.pop(key, None)
    return metadata


def _checkpoint_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        msg = "Checkpoint timestamp must be a timezone-bearing ISO string"
        raise TypeError(msg)
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        msg = "Checkpoint timestamp requires an explicit timezone"
        raise ValueError(msg)
    return timestamp


class BarConsolidator:
    """Consolidate minute bars into N-minute bars stamped at the period close.

    A consolidated bar is emitted at the *close* of the period. For example,
    a 15-minute consolidator receiving 1-minute bars stamped at their start
    times from 09:00..09:14 emits one bar with ``timestamp = 09:15`` when
    the 09:14-09:15 candle is processed.

    Usage::

        consolidator = BarConsolidator(period_minutes=15, on_bar=my_handler)
        for minute_bar in stream:
            consolidator.update(minute_bar)
    """

    def __init__(
        self,
        period_minutes: int = 15,
        on_bar: Callable[[Bar], None] | None = None,
        *,
        min_coverage: float = 0.0,
    ) -> None:
        if period_minutes < 1:
            msg = f"period_minutes must be >= 1, got {period_minutes}"
            raise ValueError(msg)
        if not 0.0 <= min_coverage <= 1.0:
            msg = f"min_coverage must be in [0, 1], got {min_coverage}"
            raise ValueError(msg)
        self._period_minutes = period_minutes
        self._on_bar = on_bar
        # Under-covered periods (a mid-period source gap left fewer constituent
        # bars than expected) are SUPPRESSED when their coverage is below this
        # threshold, so a strategy never sees an incomplete bar as if it were a
        # full one (IND-2). The zero default emits every completed interval;
        # every emitted bar is still stamped with its coverage either way.
        self._min_coverage = min_coverage
        self._current_period_end: datetime | None = None
        self._working_bar: Bar | None = None
        self._bar_count: int = 0  # minute bars accumulated in current period
        self._source_minutes: int = 1  # minutes per source bar in this period

    @property
    def period_minutes(self) -> int:
        return self._period_minutes

    def replace_callback(
        self, on_bar: Callable[[Bar], None] | None
    ) -> Callable[[Bar], None] | None:
        """Swap the emission callback and return the previous callback."""
        previous = self._on_bar
        self._on_bar = on_bar
        return previous

    def snapshot_state(self) -> dict[str, Any]:
        """Capture the unfinished bucket without flushing or serializing callbacks."""
        working = asdict(self._working_bar) if self._working_bar is not None else None
        if working is not None and self._working_bar is not None:
            working["timestamp"] = self._working_bar.timestamp.isoformat()
        snapshot = {
            "schema_version": 1,
            "period_minutes": self._period_minutes,
            "min_coverage": self._min_coverage,
            "current_period_end": (
                self._current_period_end.isoformat() if self._current_period_end else None
            ),
            "working_bar": working,
            "bar_count": self._bar_count,
            "source_minutes": self._source_minutes,
        }
        return cast(dict[str, Any], json.loads(json.dumps(snapshot, allow_nan=False)))

    def restore_state(self, snapshot: Mapping[str, Any]) -> None:
        """Validate a JSON bucket completely before replacing state; never emit."""
        try:
            period_end, working, count, source_minutes = self._decode_checkpoint(snapshot)
        except (KeyError, TypeError, ValueError) as exc:
            msg = f"Invalid consolidation checkpoint: {exc}"
            raise ValueError(msg) from exc
        self._current_period_end = period_end
        self._working_bar = working
        self._bar_count = count
        self._source_minutes = source_minutes

    def _decode_checkpoint(
        self, snapshot: Mapping[str, Any]
    ) -> tuple[datetime | None, Bar | None, int, int]:
        payload = json.loads(json.dumps(dict(snapshot), allow_nan=False))
        if set(payload) != {
            "schema_version",
            "period_minutes",
            "min_coverage",
            "current_period_end",
            "working_bar",
            "bar_count",
            "source_minutes",
        }:
            msg = "Malformed consolidation checkpoint"
            raise ValueError(msg)
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != 1
            or type(payload["period_minutes"]) is not int
            or payload["period_minutes"] != self._period_minutes
            or isinstance(payload["min_coverage"], bool)
            or payload["min_coverage"] != self._min_coverage
        ):
            msg = "Incompatible consolidation checkpoint configuration"
            raise ValueError(msg)
        count, source_minutes = payload["bar_count"], payload["source_minutes"]
        if (
            type(count) is not int
            or count < 0
            or type(source_minutes) is not int
            or source_minutes < 1
        ):
            msg = "Invalid consolidation checkpoint counts"
            raise ValueError(msg)
        raw = payload["working_bar"]
        working = None
        period_end = None
        if raw is None:
            if payload["current_period_end"] is not None or count != 0:
                msg = "Empty consolidation checkpoint retains a bucket"
                raise ValueError(msg)
        else:
            if not isinstance(raw, dict) or set(raw) != {
                "symbol",
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "timeframe",
                "source",
                "metadata",
            }:
                msg = "Malformed consolidation checkpoint bar"
                raise ValueError(msg)
            timestamp = _checkpoint_timestamp(raw["timestamp"])
            metadata = raw["metadata"]
            if not isinstance(metadata, dict):
                msg = "Consolidation checkpoint metadata must be an object"
                raise TypeError(msg)
            source_timeframe = metadata["source_timeframe"]
            source_timestamp = _checkpoint_timestamp(metadata["source_price_ts"])
            if (
                not isinstance(source_timeframe, str)
                or _timeframe_minutes(source_timeframe) != source_minutes
                or source_timestamp + timedelta(minutes=source_minutes) != timestamp
            ):
                msg = "Consolidation checkpoint duration/timestamp disagrees with provenance"
                raise ValueError(msg)
            period_end = _checkpoint_timestamp(payload["current_period_end"])
            error = ohlcv_invariant_error(
                open_price=raw["open"],
                high=raw["high"],
                low=raw["low"],
                close=raw["close"],
                volume=raw["volume"],
            )
            if (
                error is not None
                or not isinstance(raw["symbol"], str)
                or not raw["symbol"]
                or raw["symbol"] != raw["symbol"].strip()
                or not isinstance(raw["metadata"], dict)
                or (raw["source"] is not None and not isinstance(raw["source"], str))
                or raw["timeframe"] != f"{self._period_minutes}m"
                or count == 0
                or period_end != _period_end(timestamp, self._period_minutes)
            ):
                msg = "Invalid consolidation checkpoint bucket or OHLCV"
                raise ValueError(msg)
            # A gap-triggering constituent can close its new bucket exactly;
            # update() intentionally leaves that new bucket pending.
            working = Bar(**{**raw, "timestamp": timestamp})
        return period_end, working, count, source_minutes

    def update(self, bar: Bar) -> Bar | None:
        """Feed a minute bar and return a consolidated bar if the period is complete.

        Args:
            bar: A minute-resolution Bar.

        Returns:
            A consolidated Bar when the period closes, else None.
        """
        bar_close_ts = _bar_close_time(bar)
        period_end = _period_end(bar_close_ts, self._period_minutes)

        # New period → emit previous period's bar, start new one
        if self._current_period_end is not None and period_end != self._current_period_end:
            emitted = self._emit()
            self._start_new_period(bar, period_end)
            return emitted

        # Same period or very first bar
        if self._working_bar is None:
            self._start_new_period(bar, period_end)
        else:
            self._working_bar.high = max(self._working_bar.high, bar.high)
            self._working_bar.low = min(self._working_bar.low, bar.low)
            self._working_bar.close = bar.close
            self._working_bar.volume += bar.volume
            self._working_bar.timestamp = bar_close_ts
            self._working_bar.metadata = _source_metadata(bar)
            self._bar_count += 1

        # Check if this minute bar fills the period exactly
        if self._current_period_end is not None and bar_close_ts >= self._current_period_end:
            return self._emit()

        return None

    def flush(self) -> Bar | None:
        """Force-emit the current working bar (e.g. at end of day)."""
        if self._working_bar is not None:
            return self._emit()
        return None

    def _start_new_period(self, bar: Bar, period_end: datetime) -> None:
        bar_close_ts = _bar_close_time(bar)
        self._current_period_end = period_end
        self._source_minutes = _timeframe_minutes(bar.timeframe)
        self._working_bar = Bar(
            symbol=bar.symbol,
            timestamp=bar_close_ts,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            timeframe=f"{self._period_minutes}m",
            source=bar.source,
            metadata=_source_metadata(bar),
        )
        self._bar_count = 1

    def _coverage(self) -> float:
        expected = max(1, self._period_minutes // self._source_minutes)
        return min(1.0, self._bar_count / expected)

    def _emit(self) -> Bar | None:
        bar = self._working_bar
        coverage = self._coverage()
        constituents = self._bar_count
        self._working_bar = None
        self._current_period_end = None
        self._bar_count = 0
        if bar is None:
            return None
        bar.metadata["constituent_bars"] = constituents
        bar.metadata["coverage"] = round(coverage, 4)
        bar.metadata["complete"] = coverage >= 1.0
        # Drop a period whose source data was too gappy to trust as a full bar.
        if self._min_coverage > 0.0 and coverage < self._min_coverage:
            return None
        if self._on_bar is not None:
            self._on_bar(bar)
        return bar
