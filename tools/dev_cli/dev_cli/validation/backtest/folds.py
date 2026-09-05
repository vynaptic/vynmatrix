"""Chronological and combinatorial validation splits with explicit event purging."""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import BacktestReport


@dataclass(frozen=True)
class EventInterval:
    """Half-open information interval associated with one observation."""

    observation_index: int
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end < self.start:
            msg = "event end must not precede event start"
            raise ValueError(msg)


@dataclass(frozen=True)
class AnchoredFold:
    """One anchored training/OOS split over ordered bar indices."""

    index: int
    raw_training_indices: tuple[int, ...]
    training_indices: tuple[int, ...]
    purged_training_indices: tuple[int, ...]
    calendar_purged_training_indices: tuple[int, ...]
    overlap_purged_training_indices: tuple[int, ...]
    embargoed_training_indices: tuple[int, ...]
    bootstrap_indices: tuple[int, ...]
    test_indices: tuple[int, ...]
    test_observation_timestamps: tuple[datetime, ...]
    train_start_ts: datetime
    train_end_exclusive_ts: datetime
    declared_train_end_exclusive_ts: datetime
    selection_last_ts: datetime | None
    calendar_purge_start_ts: datetime | None
    pre_oos_purge: timedelta
    embargo_bars: int
    test_start_ts: datetime
    test_end_exclusive_ts: datetime | None
    evidence_role: str = field(default="retrospective_diagnostic", init=False)


@dataclass(frozen=True)
class TimestampFoldWindow:
    """Exact half-open calendar boundaries for one anchored OOS fold."""

    train_end_exclusive: datetime
    test_start: datetime
    test_end_exclusive: datetime

    def __post_init__(self) -> None:
        values = (
            ("train_end_exclusive", self.train_end_exclusive),
            ("test_start", self.test_start),
            ("test_end_exclusive", self.test_end_exclusive),
        )
        for name, value in values:
            if value.tzinfo is None or value.utcoffset() is None:
                msg = f"{name} must be timezone-aware"
                raise ValueError(msg)
        if self.train_end_exclusive > self.test_start:
            msg = "train_end_exclusive must not be after test_start"
            raise ValueError(msg)
        if self.test_start >= self.test_end_exclusive:
            msg = "test_start must be earlier than test_end_exclusive"
            raise ValueError(msg)


def _validate_timestamps(timestamps: Sequence[datetime]) -> None:
    if not timestamps:
        msg = "timestamps must not be empty"
        raise ValueError(msg)
    if any(current <= previous for previous, current in itertools.pairwise(timestamps)):
        msg = "timestamps must be strictly increasing"
        raise ValueError(msg)
    if any(timestamp.tzinfo is None or timestamp.utcoffset() is None for timestamp in timestamps):
        msg = "timestamps must be timezone-aware"
        raise ValueError(msg)


def _validate_event_intervals(
    event_intervals: Mapping[int, EventInterval],
    *,
    observation_count: int,
) -> None:
    missing = set(range(observation_count)) - set(event_intervals)
    extra = set(event_intervals) - set(range(observation_count))
    if missing or extra:
        msg = "event_intervals must exactly cover every observation index"
        raise ValueError(msg)
    if any(index != event.observation_index for index, event in event_intervals.items()):
        msg = "event_intervals keys must match their observation_index"
        raise ValueError(msg)


def _overlaps(left: EventInterval, right: EventInterval, embargo: timedelta) -> bool:
    left_end = left.end if left.end > left.start else left.start + timedelta(microseconds=1)
    right_end = right.end if right.end > right.start else right.start + timedelta(microseconds=1)
    forbidden_start = right.start - embargo
    forbidden_end = right_end + embargo
    return left.start < forbidden_end and forbidden_start < left_end


def purge_overlapping_events(
    training_indices: Sequence[int],
    test_indices: Sequence[int],
    event_intervals: Mapping[int, EventInterval],
    *,
    embargo: timedelta = timedelta(0),
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Return eligible and purged training indices using actual event horizons."""

    if embargo < timedelta(0):
        msg = "embargo must not be negative"
        raise ValueError(msg)
    test_events = [event_intervals[index] for index in test_indices if index in event_intervals]
    eligible: list[int] = []
    purged: list[int] = []
    for index in training_indices:
        event = event_intervals.get(index)
        if event is not None and any(_overlaps(event, test, embargo) for test in test_events):
            purged.append(index)
        else:
            eligible.append(index)
    return tuple(eligible), tuple(purged)


def build_report_event_intervals(
    timestamps: Sequence[datetime],
    reports: Sequence[BacktestReport],
    *,
    expected_latency_bars: int = 0,
) -> dict[int, EventInterval]:
    """Build conservative event horizons from immutable execution evidence.

    Under the explicit-interval, zero-latency contract, a filled next-open
    entry has the same boundary timestamp as its decision-close signal.  This
    permits exact one-to-one matching by timestamp and side without rerunning a
    strategy or guessing from prices.  Every report must cover the identical
    timestamp grid.  Across candidates/assets, the event end for each decision
    timestamp is the maximum matched exit or terminal mark timestamp.
    """

    _validate_timestamps(timestamps)
    if not reports:
        msg = "event interval construction requires at least one report"
        raise ValueError(msg)
    if isinstance(expected_latency_bars, bool) or not isinstance(
        expected_latency_bars,
        int,
    ):
        msg = "expected_latency_bars must be an integer"
        raise TypeError(msg)
    if expected_latency_bars != 0:
        msg = "exact report event matching currently requires zero latency"
        raise ValueError(msg)

    normalized_timestamps = tuple(timestamp.astimezone(UTC) for timestamp in timestamps)
    timestamp_to_index = {timestamp: index for index, timestamp in enumerate(normalized_timestamps)}
    maximum_ends = list(normalized_timestamps)
    for report_index, report in enumerate(reports):
        _merge_report_event_ends(
            report,
            report_index=report_index,
            timestamps=normalized_timestamps,
            timestamp_to_index=timestamp_to_index,
            maximum_ends=maximum_ends,
        )
    return {
        index: EventInterval(
            observation_index=index,
            start=timestamp,
            end=maximum_ends[index],
        )
        for index, timestamp in enumerate(normalized_timestamps)
    }


def _merge_report_event_ends(
    report: BacktestReport,
    *,
    report_index: int,
    timestamps: tuple[datetime, ...],
    timestamp_to_index: Mapping[datetime, int],
    maximum_ends: list[datetime],
) -> None:
    generated_timestamps = _validate_report_event_contract(
        report,
        report_index=report_index,
        timestamps=timestamps,
        timestamp_to_index=timestamp_to_index,
    )
    matched_exits = _execution_exits_by_entry(
        report,
        report_index=report_index,
        timestamps=timestamps,
        timestamp_to_index=timestamp_to_index,
    )
    _merge_raw_entry_horizons(
        report,
        report_index=report_index,
        generated_timestamps=generated_timestamps,
        timestamp_to_index=timestamp_to_index,
        matched_exits=matched_exits,
        maximum_ends=maximum_ends,
    )


def _validate_report_event_contract(
    report: BacktestReport,
    *,
    report_index: int,
    timestamps: tuple[datetime, ...],
    timestamp_to_index: Mapping[datetime, int],
) -> tuple[datetime, ...]:
    if report.config.fill_policy.timestamp_contract != "explicit_interval":
        msg = f"report {report_index} lacks an explicit timestamp contract"
        raise ValueError(msg)
    latency_bars = (
        report.config.execution_costs.latency_bars
        if report.config.execution_costs is not None
        else 0
    )
    if latency_bars != 0:
        msg = f"report {report_index} does not use zero-latency expected costs"
        raise ValueError(msg)
    if report.unmatched_signals:
        msg = f"report {report_index} contains unmatched signals"
        raise ValueError(msg)
    if report.signals_emitted != len(report.raw_signal_ledger):
        msg = f"report {report_index} raw-signal count does not reconcile"
        raise ValueError(msg)
    equity_timestamps = tuple(timestamp.astimezone(UTC) for timestamp, _ in report.equity_curve)
    if equity_timestamps != timestamps:
        msg = f"report {report_index} does not exactly cover the timestamp grid"
        raise ValueError(msg)

    evidence = report.raw_signal_ledger
    generated_timestamps = tuple(item.generated_at.astimezone(UTC) for item in evidence)
    if any(current <= previous for previous, current in itertools.pairwise(generated_timestamps)):
        msg = f"report {report_index} raw-signal ledger is not strictly ordered"
        raise ValueError(msg)
    if any(timestamp not in timestamp_to_index for timestamp in generated_timestamps):
        msg = f"report {report_index} raw-signal ledger exceeds timestamp coverage"
        raise ValueError(msg)
    if any(item.symbol != report.config.symbol for item in evidence):
        msg = f"report {report_index} raw-signal symbol does not match its config"
        raise ValueError(msg)
    return generated_timestamps


def _execution_exits_by_entry(
    report: BacktestReport,
    *,
    report_index: int,
    timestamps: tuple[datetime, ...],
    timestamp_to_index: Mapping[datetime, int],
) -> dict[tuple[datetime, str], datetime]:
    matched_exits: dict[tuple[datetime, str], datetime] = {}
    previous_entry: datetime | None = None
    for trade in report.trades:
        entry_ts = trade.entry_ts.astimezone(UTC)
        exit_ts = trade.exit_ts.astimezone(UTC)
        if trade.symbol != report.config.symbol:
            msg = f"report {report_index} trade symbol does not match its config"
            raise ValueError(msg)
        if trade.side not in {"long", "short"}:
            msg = f"report {report_index} trade side is invalid"
            raise ValueError(msg)
        if entry_ts not in timestamp_to_index or not entry_ts <= exit_ts <= timestamps[-1]:
            msg = f"report {report_index} trade lies outside timestamp coverage"
            raise ValueError(msg)
        if previous_entry is not None and entry_ts <= previous_entry:
            msg = f"report {report_index} trades are not strictly entry-ordered"
            raise ValueError(msg)
        key = (entry_ts, trade.side)
        if key in matched_exits:
            msg = f"report {report_index} has ambiguous duplicate trade entries"
            raise ValueError(msg)
        matched_exits[key] = exit_ts
        previous_entry = entry_ts

    terminal = report.terminal_position
    if terminal is not None:
        entry_ts = terminal.entry_ts.astimezone(UTC)
        if terminal.symbol != report.config.symbol or terminal.side not in {"long", "short"}:
            msg = f"report {report_index} terminal exposure is invalid"
            raise ValueError(msg)
        if entry_ts not in timestamp_to_index:
            msg = f"report {report_index} terminal entry exceeds timestamp coverage"
            raise ValueError(msg)
        if previous_entry is not None and entry_ts <= previous_entry:
            msg = f"report {report_index} terminal entry is not execution-ordered"
            raise ValueError(msg)
        key = (entry_ts, terminal.side)
        if key in matched_exits:
            msg = f"report {report_index} terminal entry duplicates a closed trade"
            raise ValueError(msg)
        matched_exits[key] = timestamps[-1]
    return matched_exits


def _merge_raw_entry_horizons(
    report: BacktestReport,
    *,
    report_index: int,
    generated_timestamps: tuple[datetime, ...],
    timestamp_to_index: Mapping[datetime, int],
    matched_exits: dict[tuple[datetime, str], datetime],
    maximum_ends: list[datetime],
) -> None:
    entry_actions = {"LONG": "long", "SHORT": "short"}
    allowed_actions = {*entry_actions, "CLOSE", "HOLD"}
    for item, generated_at in zip(
        report.raw_signal_ledger,
        generated_timestamps,
        strict=True,
    ):
        if item.action not in allowed_actions:
            msg = f"report {report_index} raw-signal action is invalid"
            raise ValueError(msg)
        side = entry_actions.get(item.action)
        if side is None:
            continue
        exit_ts = matched_exits.pop((generated_at, side), None)
        if exit_ts is None:
            msg = f"report {report_index} entry signal has no exact execution match"
            raise ValueError(msg)
        observation_index = timestamp_to_index[generated_at]
        maximum_ends[observation_index] = max(
            maximum_ends[observation_index],
            exit_ts,
        )
    if matched_exits:
        msg = f"report {report_index} execution entry has no exact raw-signal match"
        raise ValueError(msg)


def build_timestamp_anchored_folds(
    timestamps: Sequence[datetime],
    *,
    windows: Sequence[TimestampFoldWindow],
    bootstrap_bars: int,
    pre_oos_purge: timedelta = timedelta(days=60),
    embargo_bars: int = 30,
    event_intervals: Mapping[int, EventInterval] | None = None,
) -> tuple[AnchoredFold, ...]:
    """Build expanding folds from exact calendar boundaries.

    For each fold, observations in ``[test_start - pre_oos_purge,
    test_start)`` are calendar-purged first.  The final ``embargo_bars``
    otherwise-eligible training observations immediately before that purge are
    then excluded from parameter selection.  Immediate pre-OOS observations
    may still appear in ``bootstrap_indices`` solely as non-trading indicator
    pre-roll.  The returned exclusion sets are disjoint and identify the exact
    indices used; no boundary is inferred from a nominal bar count.
    """

    _validate_timestamps(timestamps)
    if not windows:
        msg = "windows must not be empty"
        raise ValueError(msg)
    if bootstrap_bars < 0:
        msg = "bootstrap_bars must not be negative"
        raise ValueError(msg)
    if pre_oos_purge < timedelta(0):
        msg = "pre_oos_purge must not be negative"
        raise ValueError(msg)
    if isinstance(embargo_bars, bool) or not isinstance(embargo_bars, int):
        msg = "embargo_bars must be an integer"
        raise TypeError(msg)
    if embargo_bars < 0:
        msg = "embargo_bars must not be negative"
        raise ValueError(msg)
    if event_intervals is not None:
        _validate_event_intervals(event_intervals, observation_count=len(timestamps))

    normalized_timestamps = tuple(timestamp.astimezone(UTC) for timestamp in timestamps)
    normalized_windows = tuple(
        TimestampFoldWindow(
            train_end_exclusive=window.train_end_exclusive.astimezone(UTC),
            test_start=window.test_start.astimezone(UTC),
            test_end_exclusive=window.test_end_exclusive.astimezone(UTC),
        )
        for window in windows
    )
    if any(
        current.test_start < previous.test_end_exclusive
        for previous, current in itertools.pairwise(normalized_windows)
    ):
        msg = "timestamp fold test windows must be ordered and non-overlapping"
        raise ValueError(msg)

    folds: list[AnchoredFold] = []
    for fold_index, window in enumerate(normalized_windows):
        raw_training = tuple(
            index
            for index, timestamp in enumerate(normalized_timestamps)
            if timestamp < window.train_end_exclusive
        )
        test_indices = tuple(
            index
            for index, timestamp in enumerate(normalized_timestamps)
            if window.test_start <= timestamp < window.test_end_exclusive
        )
        if not raw_training:
            msg = f"fold {fold_index} has no training observations before its calendar boundary"
            raise ValueError(msg)
        if not test_indices:
            msg = f"fold {fold_index} has no observations in its exact OOS window"
            raise ValueError(msg)

        purge_start = window.test_start - pre_oos_purge
        calendar_purged = tuple(
            index
            for index in raw_training
            if purge_start <= normalized_timestamps[index] < window.test_start
        )
        calendar_purged_set = set(calendar_purged)
        before_calendar_purge = tuple(
            index for index in raw_training if index not in calendar_purged_set
        )
        embargo_count = min(embargo_bars, len(before_calendar_purge))
        embargoed = before_calendar_purge[-embargo_count:] if embargo_count else ()
        embargoed_set = set(embargoed)
        selection_candidates = tuple(
            index for index in before_calendar_purge if index not in embargoed_set
        )
        if event_intervals is None:
            training_indices = selection_candidates
            overlap_purged: tuple[int, ...] = ()
        else:
            training_indices, overlap_purged = purge_overlapping_events(
                selection_candidates,
                test_indices,
                event_intervals,
            )
        if not training_indices:
            msg = f"fold {fold_index} has no selectable training observations after exclusions"
            raise ValueError(msg)

        purged = tuple(sorted((*calendar_purged, *embargoed, *overlap_purged)))
        first_test_index = test_indices[0]
        preceding_indices = tuple(range(first_test_index))
        bootstrap_indices = preceding_indices[-bootstrap_bars:] if bootstrap_bars else ()
        last_training_index = training_indices[-1]
        effective_end_index = last_training_index + 1
        effective_end = (
            normalized_timestamps[effective_end_index]
            if effective_end_index < len(normalized_timestamps)
            else window.train_end_exclusive
        )
        folds.append(
            AnchoredFold(
                index=fold_index,
                raw_training_indices=raw_training,
                training_indices=training_indices,
                purged_training_indices=purged,
                calendar_purged_training_indices=calendar_purged,
                overlap_purged_training_indices=overlap_purged,
                embargoed_training_indices=embargoed,
                bootstrap_indices=bootstrap_indices,
                test_indices=test_indices,
                test_observation_timestamps=tuple(
                    normalized_timestamps[index] for index in test_indices
                ),
                train_start_ts=normalized_timestamps[raw_training[0]],
                train_end_exclusive_ts=effective_end,
                declared_train_end_exclusive_ts=window.train_end_exclusive,
                selection_last_ts=normalized_timestamps[last_training_index],
                calendar_purge_start_ts=purge_start,
                pre_oos_purge=pre_oos_purge,
                embargo_bars=embargo_bars,
                test_start_ts=window.test_start,
                test_end_exclusive_ts=window.test_end_exclusive,
            )
        )
    return tuple(folds)
