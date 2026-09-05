"""Normalize official broker/exchange market-session observations.

The adapters in this module never manufacture weekday templates. IBKR and
Saxo provide bounded schedules; NSE exposes current official market state, so
that source is represented as a deliberately short authorization lease.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .ibkr_client import IBKRCandleClient
from .saxo_client import SaxoCandleClient

IBKR_SCHEDULE_SOURCE = "https://api.ibkr.com/v1/api/contract/trading-schedule"
SAXO_LIVE_SCHEDULE_SOURCE = (
    "https://gateway.saxobank.com/openapi/ref/v1/instruments/tradingschedule"
)
NSE_MARKET_STATUS_SOURCE = "https://www.nseindia.com/api/marketStatus"

_NSE_TIME_ZONE = ZoneInfo("Asia/Kolkata")
_NSE_MIN_LEASE_SECONDS = 30
_NSE_MAX_LEASE_SECONDS = 300
_COMPACT_DATE_LENGTH = 8


class OfficialMarketCalendarError(RuntimeError):
    """An official response cannot safely authorize a market session."""


@dataclass(frozen=True, slots=True)
class OfficialSessionWindow:
    """One provider-declared regular trading interval."""

    opens_at: datetime
    closes_at: datetime


@dataclass(frozen=True, slots=True)
class OfficialMarketCalendar:
    """One complete, bounded official observation ready for persistence."""

    source_kind: str
    provider: str
    source_reference: str
    observed_at: datetime
    coverage_start: datetime
    coverage_end: datetime
    sessions: tuple[OfficialSessionWindow, ...]


class OfficialMarketCalendarProvider(Protocol):
    """Port implemented by one supervised official schedule source."""

    provider: str

    def fetch(
        self,
        *,
        product_id: str,
        broker_instrument_type: str | None,
    ) -> OfficialMarketCalendar: ...

    def close(self) -> None: ...


def _as_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"{field} must include an explicit UTC offset"
        raise OfficialMarketCalendarError(msg)
    return value.astimezone(UTC)


def _parse_rfc3339(value: object, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        msg = f"{field} is not an RFC3339 datetime"
        raise OfficialMarketCalendarError(msg) from exc
    return _as_utc(parsed, field=field)


def _build_calendar(
    *,
    source_kind: str,
    provider: str,
    source_reference: str,
    observed_at: datetime,
    coverage_start: datetime,
    coverage_end: datetime,
    sessions: list[OfficialSessionWindow],
) -> OfficialMarketCalendar:
    observed = _as_utc(observed_at, field="observed_at")
    starts_at = _as_utc(coverage_start, field="coverage_start")
    ends_at = _as_utc(coverage_end, field="coverage_end")
    if ends_at <= starts_at:
        msg = "Official market-calendar coverage is empty"
        raise OfficialMarketCalendarError(msg)

    ordered = tuple(sorted(sessions, key=lambda row: row.opens_at))
    previous_close: datetime | None = None
    for row in ordered:
        opens_at = _as_utc(row.opens_at, field="session.opens_at")
        closes_at = _as_utc(row.closes_at, field="session.closes_at")
        if closes_at <= opens_at:
            msg = "Official market-calendar session has a non-positive duration"
            raise OfficialMarketCalendarError(msg)
        if opens_at < starts_at or closes_at > ends_at:
            msg = "Official market-calendar session lies outside provider coverage"
            raise OfficialMarketCalendarError(msg)
        if previous_close is not None and opens_at < previous_close:
            msg = "Official market-calendar sessions overlap"
            raise OfficialMarketCalendarError(msg)
        previous_close = closes_at

    return OfficialMarketCalendar(
        source_kind=source_kind,
        provider=provider,
        source_reference=source_reference,
        observed_at=observed,
        coverage_start=starts_at,
        coverage_end=ends_at,
        sessions=ordered,
    )


def _parse_ibkr_date(value: object) -> date:
    raw = str(value).strip()
    try:
        if len(raw) == _COMPACT_DATE_LENGTH and raw.isdecimal():
            return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        return date.fromisoformat(raw)
    except ValueError:
        pass
    msg = f"IBKR trading-schedule date is invalid: {raw!r}"
    raise OfficialMarketCalendarError(msg)


def _parse_ibkr_epoch(value: object, *, field: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        msg = f"{field} must be an epoch-second integer"
        raise OfficialMarketCalendarError(msg)
    try:
        epoch_seconds = int(value)
    except (TypeError, ValueError) as exc:
        msg = f"{field} must be an epoch-second integer"
        raise OfficialMarketCalendarError(msg) from exc
    try:
        return datetime.fromtimestamp(epoch_seconds, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        msg = f"{field} is outside the supported epoch range"
        raise OfficialMarketCalendarError(msg) from exc


def normalize_ibkr_trading_schedule(
    payload: dict[str, Any],
    *,
    observed_at: datetime,
) -> OfficialMarketCalendar:
    """Normalize IBKR's six-day contract schedule using regular liquid hours."""
    timezone_name = payload.get("exchange_time_zone")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        msg = "IBKR trading schedule is missing exchange_time_zone"
        raise OfficialMarketCalendarError(msg)
    try:
        exchange_zone = ZoneInfo(timezone_name.strip())
    except ZoneInfoNotFoundError as exc:
        msg = f"IBKR returned an unknown exchange timezone: {timezone_name!r}"
        raise OfficialMarketCalendarError(msg) from exc

    schedules = payload.get("schedules")
    if not isinstance(schedules, dict) or not schedules:
        msg = "IBKR trading schedule has no bounded schedules"
        raise OfficialMarketCalendarError(msg)

    schedule_dates: list[date] = []
    sessions: list[OfficialSessionWindow] = []
    for raw_date, raw_entries in schedules.items():
        schedule_dates.append(_parse_ibkr_date(raw_date))
        if isinstance(raw_entries, dict):
            entries = [raw_entries]
        elif isinstance(raw_entries, list) and all(
            isinstance(entry, dict) for entry in raw_entries
        ):
            entries = raw_entries
        else:
            msg = f"IBKR trading schedule has invalid hours for {raw_date!r}"
            raise OfficialMarketCalendarError(msg)

        for entry in entries:
            liquid_hours = entry.get("liquid_hours")
            if not isinstance(liquid_hours, list):
                msg = f"IBKR trading schedule is missing liquid_hours for {raw_date!r}"
                raise OfficialMarketCalendarError(msg)
            for index, interval in enumerate(liquid_hours):
                if not isinstance(interval, dict):
                    msg = f"IBKR liquid_hours[{index}] is not an object"
                    raise OfficialMarketCalendarError(msg)
                sessions.append(
                    OfficialSessionWindow(
                        opens_at=_parse_ibkr_epoch(
                            interval.get("opening"),
                            field=f"IBKR liquid_hours[{index}].opening",
                        ),
                        closes_at=_parse_ibkr_epoch(
                            interval.get("closing"),
                            field=f"IBKR liquid_hours[{index}].closing",
                        ),
                    )
                )

    first_boundary = datetime.combine(min(schedule_dates), time.min, tzinfo=exchange_zone)
    final_boundary = datetime.combine(
        max(schedule_dates) + timedelta(days=1),
        time.min,
        tzinfo=exchange_zone,
    )
    if sessions:
        first_boundary = min(
            first_boundary.astimezone(UTC),
            *(session.opens_at for session in sessions),
        )
        final_boundary = max(
            final_boundary.astimezone(UTC),
            *(session.closes_at for session in sessions),
        )
    return _build_calendar(
        source_kind="broker",
        provider="ibkr",
        source_reference=IBKR_SCHEDULE_SOURCE,
        observed_at=observed_at,
        coverage_start=first_boundary,
        coverage_end=final_boundary,
        sessions=sessions,
    )


def normalize_saxo_trading_schedule(
    payload: dict[str, Any],
    *,
    observed_at: datetime,
) -> OfficialMarketCalendar:
    """Normalize only Saxo's documented ``AutomatedTrading`` intervals."""
    sessions_payload = payload.get("Sessions")
    if not isinstance(sessions_payload, list) or not sessions_payload:
        msg = "Saxo trading schedule has no bounded Sessions"
        raise OfficialMarketCalendarError(msg)

    coverage_intervals: list[tuple[datetime, datetime]] = []
    sessions: list[OfficialSessionWindow] = []
    for index, raw_session in enumerate(sessions_payload):
        if not isinstance(raw_session, dict):
            msg = f"Saxo Sessions[{index}] is not an object"
            raise OfficialMarketCalendarError(msg)
        starts_at = _parse_rfc3339(
            raw_session.get("StartTime"),
            field=f"Saxo Sessions[{index}].StartTime",
        )
        ends_at = _parse_rfc3339(
            raw_session.get("EndTime"),
            field=f"Saxo Sessions[{index}].EndTime",
        )
        if ends_at <= starts_at:
            msg = f"Saxo Sessions[{index}] has a non-positive duration"
            raise OfficialMarketCalendarError(msg)
        state = raw_session.get("State")
        if not isinstance(state, str) or not state.strip():
            msg = f"Saxo Sessions[{index}] is missing State"
            raise OfficialMarketCalendarError(msg)
        coverage_intervals.append((starts_at, ends_at))
        if state == "AutomatedTrading":
            sessions.append(OfficialSessionWindow(opens_at=starts_at, closes_at=ends_at))

    return _build_calendar(
        source_kind="broker",
        provider="saxo_live",
        source_reference=SAXO_LIVE_SCHEDULE_SOURCE,
        observed_at=observed_at,
        coverage_start=min(interval[0] for interval in coverage_intervals),
        coverage_end=max(interval[1] for interval in coverage_intervals),
        sessions=sessions,
    )


class IBKROfficialMarketCalendarProvider:
    """IBKR Client Portal trading-schedule adapter."""

    provider = "ibkr"

    def __init__(
        self,
        *,
        gateway_url: str | None = None,
        client: IBKRCandleClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client or IBKRCandleClient(gateway_url=gateway_url)
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    def fetch(
        self,
        *,
        product_id: str,
        broker_instrument_type: str | None,
    ) -> OfficialMarketCalendar:
        del broker_instrument_type
        payload = self._client.fetch_trading_schedule(product_id=product_id)
        return normalize_ibkr_trading_schedule(payload, observed_at=self._clock())

    def close(self) -> None:
        self._client.close()


class SaxoOfficialMarketCalendarProvider:
    """Saxo OpenAPI trading-schedule adapter."""

    def __init__(
        self,
        *,
        access_token: str | None,
        access_token_expires_at: str | datetime | None,
        client: SaxoCandleClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.provider = "saxo_live"
        self._client = client or SaxoCandleClient(
            environment="live",
            access_token=access_token,
            access_token_expires_at=access_token_expires_at,
        )
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    def fetch(
        self,
        *,
        product_id: str,
        broker_instrument_type: str | None,
    ) -> OfficialMarketCalendar:
        payload = self._client.fetch_trading_schedule(
            product_id=product_id,
            broker_instrument_type=broker_instrument_type,
        )
        return normalize_saxo_trading_schedule(
            payload,
            observed_at=self._clock(),
        )

    def close(self) -> None:
        self._client.close()


class NSEOfficialMarketCalendarProvider:
    """Short-lived official NSE market-state lease used by Zerodha routes.

    Kite exposes no authoritative holiday/session endpoint. A positive lease is
    therefore issued only while NSE's own current-state endpoint says the exact
    configured market is open and its trade date matches the current India date.
    """

    provider = "nse"

    def __init__(
        self,
        *,
        market: str,
        lease_seconds: int = 120,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        normalized_market = str(market or "").strip()
        if not normalized_market:
            msg = "NSE market-calendar provider requires an exact market name"
            raise ValueError(msg)
        if not _NSE_MIN_LEASE_SECONDS <= lease_seconds <= _NSE_MAX_LEASE_SECONDS:
            msg = "NSE market-state lease_seconds must be between 30 and 300"
            raise ValueError(msg)
        self._market = normalized_market
        self._lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._headers = {
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": "vynmatrix-MarketCalendar/1.0",
        }
        self._client = client or httpx.Client(
            base_url="https://www.nseindia.com",
            timeout=20.0,
            headers=self._headers,
        )

    def fetch(
        self,
        *,
        product_id: str,
        broker_instrument_type: str | None,
    ) -> OfficialMarketCalendar:
        del product_id, broker_instrument_type
        response = self._client.get("/api/marketStatus", headers=self._headers)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            msg = "NSE market-status response is not an object"
            raise OfficialMarketCalendarError(msg)
        observed_at = _as_utc(self._clock(), field="NSE observation clock")
        return self._normalize(payload, observed_at=observed_at)

    def _normalize(
        self,
        payload: dict[str, Any],
        *,
        observed_at: datetime,
    ) -> OfficialMarketCalendar:
        raw_states = payload.get("marketState")
        if not isinstance(raw_states, list):
            msg = "NSE market-status response has no marketState list"
            raise OfficialMarketCalendarError(msg)
        matches = [
            row
            for row in raw_states
            if isinstance(row, dict)
            and str(row.get("market") or "").strip().casefold() == self._market.casefold()
        ]
        if len(matches) != 1:
            msg = f"NSE market-status response must contain exactly one {self._market!r} state"
            raise OfficialMarketCalendarError(msg)
        state = matches[0]
        status = str(state.get("marketStatus") or "").strip().casefold()
        coverage_end = observed_at + timedelta(seconds=self._lease_seconds)
        sessions: list[OfficialSessionWindow] = []
        if status == "open":
            trade_date = str(state.get("tradeDate") or "").strip()
            try:
                provider_date = (
                    datetime.strptime(
                        trade_date.split(" ", maxsplit=1)[0],
                        "%d-%b-%Y",
                    )
                    .replace(tzinfo=_NSE_TIME_ZONE)
                    .date()
                )
            except ValueError as exc:
                msg = "NSE open state has an invalid tradeDate"
                raise OfficialMarketCalendarError(msg) from exc
            if provider_date != observed_at.astimezone(_NSE_TIME_ZONE).date():
                msg = "NSE open state is stale for the current India trading date"
                raise OfficialMarketCalendarError(msg)
            sessions.append(OfficialSessionWindow(opens_at=observed_at, closes_at=coverage_end))
        return _build_calendar(
            source_kind="exchange",
            provider="nse",
            source_reference=NSE_MARKET_STATUS_SOURCE,
            observed_at=observed_at,
            coverage_start=observed_at,
            coverage_end=coverage_end,
            sessions=sessions,
        )

    def close(self) -> None:
        self._client.close()


__all__ = [
    "IBKROfficialMarketCalendarProvider",
    "NSEOfficialMarketCalendarProvider",
    "OfficialMarketCalendar",
    "OfficialMarketCalendarError",
    "OfficialMarketCalendarProvider",
    "OfficialSessionWindow",
    "SaxoOfficialMarketCalendarProvider",
    "normalize_ibkr_trading_schedule",
    "normalize_saxo_trading_schedule",
]
