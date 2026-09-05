"""Bar consolidation with period-close emission semantics.

Consolidates minute bars into N-minute bars stamped at the period CLOSE
boundary (e.g. 09:15, 09:30, 09:45 for 15-minute bars) — the single
production convention shared by the live SignalWorker and the backtest harness.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from .bars import Bar
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
