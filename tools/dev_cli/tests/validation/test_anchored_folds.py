"""Timestamp-fold, event-purge, and pooled-OOS regression tests."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dev_cli.validation.backtest.folds import (
    TimestampFoldWindow,
    build_timestamp_anchored_folds,
)
from dev_cli.validation.backtest.walk_forward import (
    FoldOOSSeries,
    pool_fold_oos_returns,
)

_REPO = Path(__file__).resolve().parents[4]


def _timestamps(count: int) -> list[datetime]:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    return [start + timedelta(days=index) for index in range(count)]


def _calendar_timestamps(start: datetime, end: datetime) -> list[datetime]:
    output: list[datetime] = []
    current = start
    while current < end:
        output.append(current)
        current += timedelta(days=1)
    return output


def test_timestamp_fold_uses_exact_leap_year_boundaries_despite_data_gap() -> None:
    missing = datetime(2020, 6, 15, tzinfo=UTC)
    timestamps = [
        timestamp
        for timestamp in _calendar_timestamps(
            datetime(2019, 1, 1, tzinfo=UTC),
            datetime(2021, 1, 2, tzinfo=UTC),
        )
        if timestamp != missing
    ]
    window = TimestampFoldWindow(
        train_end_exclusive=datetime(2020, 1, 1, tzinfo=UTC),
        test_start=datetime(2020, 1, 1, tzinfo=UTC),
        test_end_exclusive=datetime(2021, 1, 1, tzinfo=UTC),
    )

    fold = build_timestamp_anchored_folds(
        timestamps,
        windows=[window],
        bootstrap_bars=32,
    )[0]
    oos_timestamps = [timestamps[index] for index in fold.test_indices]

    assert fold.test_start_ts == datetime(2020, 1, 1, tzinfo=UTC)
    assert fold.test_end_exclusive_ts == datetime(2021, 1, 1, tzinfo=UTC)
    assert datetime(2020, 2, 29, tzinfo=UTC) in oos_timestamps
    assert missing not in oos_timestamps
    assert len(oos_timestamps) == 365  # Leap year's 366 days minus one missing observation.
    assert max(oos_timestamps) == datetime(2020, 12, 31, tzinfo=UTC)
    assert datetime(2021, 1, 1, tzinfo=UTC) not in oos_timestamps


def test_calendar_purge_and_selection_embargo_expose_exact_indices() -> None:
    timestamps = _calendar_timestamps(
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2023, 1, 2, tzinfo=UTC),
    )
    # A missing source day must not turn the 60-calendar-day purge into a
    # nominal 60-observation purge.
    timestamps.remove(datetime(2021, 11, 15, tzinfo=UTC))
    window = TimestampFoldWindow(
        train_end_exclusive=datetime(2022, 1, 1, tzinfo=UTC),
        test_start=datetime(2022, 1, 1, tzinfo=UTC),
        test_end_exclusive=datetime(2023, 1, 1, tzinfo=UTC),
    )

    fold = build_timestamp_anchored_folds(
        timestamps,
        windows=[window],
        bootstrap_bars=32,
        pre_oos_purge=timedelta(days=60),
        embargo_bars=30,
    )[0]
    purge_start = datetime(2021, 11, 2, tzinfo=UTC)
    expected_calendar_purge = tuple(
        index
        for index in fold.raw_training_indices
        if purge_start <= timestamps[index] < window.test_start
    )
    otherwise_eligible = tuple(
        index for index in fold.raw_training_indices if timestamps[index] < purge_start
    )

    assert fold.calendar_purge_start_ts == purge_start
    assert fold.calendar_purged_training_indices == expected_calendar_purge
    assert len(fold.calendar_purged_training_indices) == 59
    assert fold.embargoed_training_indices == otherwise_eligible[-30:]
    assert fold.training_indices == otherwise_eligible[:-30]
    assert fold.purged_training_indices == tuple(
        sorted((*expected_calendar_purge, *otherwise_eligible[-30:]))
    )
    assert not set(fold.training_indices) & set(fold.purged_training_indices)
    assert fold.selection_last_ts == timestamps[otherwise_eligible[-31]]
    assert fold.train_end_exclusive_ts == timestamps[otherwise_eligible[-30]]
    assert fold.declared_train_end_exclusive_ts == window.train_end_exclusive
    assert fold.bootstrap_indices == tuple(range(fold.test_indices[0] - 32, fold.test_indices[0]))
    assert set(fold.bootstrap_indices) <= set(fold.purged_training_indices)


def test_protocol_folds_are_explicit_half_open_timestamp_windows() -> None:
    protocol = json.loads(
        (
            _REPO
            / "tests/fixtures/strategy_validation/registered_campaign/validation_protocol.json"
        ).read_text()
    )

    windows = [
        TimestampFoldWindow(
            train_end_exclusive=datetime.fromisoformat(fold["train_end_exclusive"]),
            test_start=datetime.fromisoformat(fold["test_start"]),
            test_end_exclusive=datetime.fromisoformat(fold["test_end_exclusive"]),
        )
        for fold in protocol["folds"]
    ]

    assert [window.test_start.year for window in windows] == [2022, 2023, 2024, 2025]
    assert all(window.train_end_exclusive == window.test_start for window in windows)
    assert all(
        window.test_end_exclusive == datetime(window.test_start.year + 1, 1, 2, tzinfo=UTC)
        for window in windows
    )


def test_pooled_oos_return_and_sharpe_match_hand_calculation() -> None:
    timestamps = _timestamps(4)
    pooled = pool_fold_oos_returns(
        [
            FoldOOSSeries(
                fold_index=0,
                expected_timestamps=tuple(timestamps[:2]),
                equity_curve=((timestamps[0], 110.0), (timestamps[1], 99.0)),
                initial_capital=100.0,
            ),
            FoldOOSSeries(
                fold_index=1,
                expected_timestamps=tuple(timestamps[2:]),
                equity_curve=((timestamps[2], 105.0), (timestamps[3], 105.0)),
                initial_capital=100.0,
            ),
        ],
        annualization_factor=365.0,
    )

    expected_returns = [0.10, -0.10, 0.05, 0.0]
    expected_mean = sum(expected_returns) / len(expected_returns)
    expected_std = math.sqrt(
        sum((value - expected_mean) ** 2 for value in expected_returns) / len(expected_returns)
    )
    assert [observation.fold_index for observation in pooled.observations] == [0, 0, 1, 1]
    assert [observation.daily_return for observation in pooled.observations] == pytest.approx(
        expected_returns
    )
    assert pooled.metrics.compounded_return_pct == pytest.approx((1.10 * 0.90 * 1.05 - 1.0) * 100.0)
    assert pooled.metrics.sharpe_ratio == pytest.approx(
        expected_mean / expected_std * math.sqrt(365.0)
    )
    assert pooled.metrics.evidence_role == "primary_pooled_oos"


def test_pooled_oos_rejects_missing_and_overlapping_observations() -> None:
    timestamps = _timestamps(3)
    missing = FoldOOSSeries(
        fold_index=0,
        expected_timestamps=tuple(timestamps[:2]),
        equity_curve=((timestamps[0], 101.0),),
        initial_capital=100.0,
    )
    with pytest.raises(ValueError, match="missing or unexpected"):
        pool_fold_oos_returns([missing], annualization_factor=365.0)

    overlapping = [
        FoldOOSSeries(
            fold_index=0,
            expected_timestamps=tuple(timestamps[:2]),
            equity_curve=((timestamps[0], 101.0), (timestamps[1], 102.0)),
            initial_capital=100.0,
        ),
        FoldOOSSeries(
            fold_index=1,
            expected_timestamps=tuple(timestamps[1:]),
            equity_curve=((timestamps[1], 99.0), (timestamps[2], 100.0)),
            initial_capital=100.0,
        ),
    ]
    with pytest.raises(ValueError, match="duplicate, overlapping"):
        pool_fold_oos_returns(overlapping, annualization_factor=365.0)
