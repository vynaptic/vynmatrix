"""Golden tests for the trading-calendar wrapper (design doc §8.3).

Every assertion pins a real historical XNYS fact so a calendar regression —
a library bump that drops a closure, a DST bug, a hand-rolled shortcut —
fails loudly instead of silently corrupting session timing.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from lib_data.sessions import (
    UnknownSessionError,
    _get_calendar,
    is_half_day,
    is_trading_day,
    session_close_utc,
    session_open_utc,
    sessions_between,
)

XNYS = "XNYS"


@pytest.mark.parametrize(
    ("session_date", "expected"),
    [
        # Juneteenth: NYSE first observed it in 2022 (2021-06-19 fell on a
        # Saturday and was NOT observed on Friday 2021-06-18).
        (date(2020, 6, 19), True),
        (date(2021, 6, 18), True),
        (date(2022, 6, 20), False),  # observed (2022-06-19 was a Sunday)
        (date(2023, 6, 19), False),
        # National Day of Mourning for President Carter.
        (date(2025, 1, 9), False),
        # Good Friday 2020 closed; the COVID floor closure (all-electronic
        # from 2020-03-23) lost zero sessions.
        (date(2020, 4, 10), False),
        (date(2020, 3, 23), True),
    ],
)
def test_trading_day_golden_dates(session_date: date, expected: bool) -> None:
    assert is_trading_day(XNYS, session_date) is expected


@pytest.mark.parametrize(
    ("session_date", "expected_close_utc"),
    [
        # Half days close 13:00 ET; both dates are EST (UTC-5) → 18:00 UTC.
        (date(2024, 11, 29), datetime(2024, 11, 29, 18, 0)),
        (date(2024, 12, 24), datetime(2024, 12, 24, 18, 0)),
    ],
)
def test_half_days_close_1300_et(session_date: date, expected_close_utc: datetime) -> None:
    assert is_half_day(XNYS, session_date) is True
    close = session_close_utc(XNYS, session_date)
    assert close == expected_close_utc
    assert close.tzinfo is None


@pytest.mark.parametrize(
    ("session_date", "expected_close_utc"),
    [
        # Regular close is 16:00 ET: 20:00 UTC under EDT, 21:00 UTC under EST.
        (date(2024, 7, 10), datetime(2024, 7, 10, 20, 0)),
        (date(2024, 1, 10), datetime(2024, 1, 10, 21, 0)),
    ],
)
def test_regular_close_across_dst_boundary(
    session_date: date, expected_close_utc: datetime
) -> None:
    assert is_half_day(XNYS, session_date) is False
    close = session_close_utc(XNYS, session_date)
    assert close == expected_close_utc
    assert close.tzinfo is None


@pytest.mark.parametrize(
    ("session_date", "expected_open_utc"),
    [
        # Regular open is 09:30 ET: 13:30 UTC under EDT, 14:30 UTC under EST.
        (date(2024, 7, 10), datetime(2024, 7, 10, 13, 30)),
        (date(2024, 1, 10), datetime(2024, 1, 10, 14, 30)),
    ],
)
def test_regular_open_across_dst_boundary(session_date: date, expected_open_utc: datetime) -> None:
    session_open = session_open_utc(XNYS, session_date)
    assert session_open == expected_open_utc
    assert session_open.tzinfo is None


def test_sessions_between_christmas_week_2024() -> None:
    assert sessions_between(XNYS, date(2024, 12, 23), date(2024, 12, 27)) == [
        date(2024, 12, 23),
        date(2024, 12, 24),
        date(2024, 12, 26),
        date(2024, 12, 27),
    ]


def test_non_trading_day_timing_fails_closed() -> None:
    with pytest.raises(UnknownSessionError, match="2025-01-09 is not a XNYS trading day"):
        session_close_utc(XNYS, date(2025, 1, 9))
    with pytest.raises(UnknownSessionError, match="not a XNYS trading day"):
        session_open_utc(XNYS, date(2024, 12, 25))


def test_is_half_day_false_on_non_trading_day() -> None:
    assert is_half_day(XNYS, date(2024, 12, 25)) is False


def test_calendar_instances_are_cached_per_mic() -> None:
    assert _get_calendar(XNYS) is _get_calendar(XNYS)
