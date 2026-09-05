"""Trading-session calendar helpers backed by ``exchange_calendars`` (§8.3).

Thin wrapper resolving exchange session timing by MIC (e.g. ``"XNYS"``).
Returned datetimes are tz-naive UTC, matching the ``prices.ts`` convention
(adapters strip tzinfo). Hand-rolling exchange calendars is exactly the bug
class the ``UnsupportedSessionTimingError`` guard exists to prevent; this
module delegates to the pinned library and fails closed on non-trading days.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any, cast

# pandas + exchange_calendars are the lib-data[sessions] extra: only the
# market-data image (scheduled-market backfill, EODHD/vendor clients) needs
# them, so they are imported lazily to keep every other service image free of
# the heavy analytics closure.
if TYPE_CHECKING:
    import pandas as pd
    from exchange_calendars import ExchangeCalendar


def _require_sessions_extra() -> tuple[Any, Any]:
    try:
        import exchange_calendars as xcals  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - environment-specific
        msg = (
            "lib_data.sessions requires the lib-data[sessions] extra "
            "(pandas + exchange_calendars); install the market-data runtime profile"
        )
        raise ImportError(msg) from exc
    return xcals, pd


__all__ = [
    "UnknownSessionError",
    "is_half_day",
    "is_trading_day",
    "session_close_utc",
    "session_open_utc",
    "sessions_between",
]


class UnknownSessionError(ValueError):
    """Raised when session timing is requested for a non-trading day."""


# Calendar construction is expensive (the full sessions schedule is built up
# front), so instances are cached per MIC for the life of the process.
_CALENDAR_CACHE: dict[str, ExchangeCalendar] = {}


def _timestamp(value: date | datetime) -> pd.Timestamp:
    _, pd = _require_sessions_extra()
    return pd.Timestamp(value)


def _get_calendar(mic: str) -> ExchangeCalendar:
    calendar = _CALENDAR_CACHE.get(mic)
    if calendar is None:
        xcals, _ = _require_sessions_extra()
        calendar = xcals.get_calendar(mic)
        _CALENDAR_CACHE[mic] = calendar
    return calendar


def _require_session(mic: str, session_date: date) -> ExchangeCalendar:
    calendar = _get_calendar(mic)
    if not calendar.is_session(_timestamp(session_date)):
        msg = f"{session_date.isoformat()} is not a {mic} trading day"
        raise UnknownSessionError(msg)
    return calendar


def _to_naive_utc(timestamp: pd.Timestamp) -> datetime:
    # to_pydatetime() is untyped in pandas; the value is always a datetime.
    return cast(datetime, timestamp.tz_convert("UTC").tz_localize(None).to_pydatetime())


def session_open_utc(mic: str, session_date: date) -> datetime:
    """Return the session open as a tz-naive UTC datetime.

    Raises:
        UnknownSessionError: If ``session_date`` is not a trading day on ``mic``.
    """
    calendar = _require_session(mic, session_date)
    return _to_naive_utc(calendar.session_open(_timestamp(session_date)))


def session_close_utc(mic: str, session_date: date) -> datetime:
    """Return the session close as a tz-naive UTC datetime.

    Raises:
        UnknownSessionError: If ``session_date`` is not a trading day on ``mic``.
    """
    calendar = _require_session(mic, session_date)
    return _to_naive_utc(calendar.session_close(_timestamp(session_date)))


def is_trading_day(mic: str, session_date: date) -> bool:
    """Return whether ``session_date`` is a trading day on ``mic``."""
    calendar = _get_calendar(mic)
    return bool(calendar.is_session(_timestamp(session_date)))


def is_half_day(mic: str, session_date: date) -> bool:
    """Return whether ``session_date`` closes early (e.g. 13:00 ET on XNYS).

    Non-trading days are not half days; they return ``False``.
    """
    calendar = _get_calendar(mic)
    return _timestamp(session_date) in calendar.early_closes


def sessions_between(mic: str, start_date: date, end_date: date) -> list[date]:
    """Return the trading days on ``mic`` in ``[start_date, end_date]``, both ends inclusive."""
    calendar = _get_calendar(mic)
    sessions = calendar.sessions_in_range(_timestamp(start_date), _timestamp(end_date))
    return [session.date() for session in sessions]
