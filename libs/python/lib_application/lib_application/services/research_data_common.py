"""Catalogue, timing and transactional limits shared by research data services."""

from __future__ import annotations

import base64
import binascii
import json
from bisect import bisect_right
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, false, func, select, text
from sqlalchemy.orm import Session

from lib_application.db.models.instruments import Instrument, MarketCalendar, MarketSession
from lib_application.db.models.research_data import ResearchDataset, ResearchImport
from lib_data.dataset import timeframe_interval
from lib_data.research_dataset import MAX_IMPORT_BARS, canonical_json, iso_timestamp, utc_timestamp

OWNER_BYTE_QUOTA = 1024 * 1024 * 1024
MAX_PAGE = 1000
MAX_OWNER_ID_LENGTH = 50
MAX_CURSOR_LENGTH = 256
CURSOR_PARTS = 2


class ResearchDataError(ValueError):
    """Public, secret-free research data refusal."""

    def __init__(self, detail: str, *, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def lock_owner(session: Session, owner_id: str) -> None:
    """Serialize import/freeze admission for one owner through transaction end."""
    if not owner_id or len(owner_id) > MAX_OWNER_ID_LENGTH:
        msg = "An explicit owner scope is required"
        raise ResearchDataError(msg, status_code=403)
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(177013, hashtext(:owner))"), {"owner": owner_id}
        )


def check_quota(
    session: Session, owner_id: str, size: int, *, quota: int = OWNER_BYTE_QUOTA
) -> None:
    used = sum(
        int(
            session.scalar(
                select(func.coalesce(func.sum(table.stored_bytes), 0)).where(
                    table.owner_id == owner_id
                )
            )
            or 0
        )
        for table in (ResearchImport, ResearchDataset)
    )
    if used + size > quota:
        msg = "Owner research stored-byte quota exceeded"
        raise ResearchDataError(msg, status_code=413)


def page_limit(limit: int) -> int:
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        msg = f"limit must be between 1 and {MAX_PAGE}"
        raise ResearchDataError(msg)
    return limit


def encode_cursor(timestamp: datetime, identity: str) -> str:
    return (
        base64.urlsafe_b64encode(canonical_json([iso_timestamp(timestamp), identity]))
        .decode()
        .rstrip("=")
    )


def _cursor_value(value: str) -> tuple[datetime, str]:
    if len(value) > MAX_CURSOR_LENGTH:
        msg = "long cursor"
        raise ValueError(msg)
    decoded = json.loads(
        base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    )
    if (
        not isinstance(decoded, list)
        or len(decoded) != CURSOR_PARTS
        or not isinstance(decoded[1], str)
        or len(decoded[1]) > MAX_OWNER_ID_LENGTH
    ):
        msg = "invalid cursor shape"
        raise ValueError(msg)
    return utc_timestamp(decoded[0]), decoded[1]


def decode_cursor(value: str | None) -> tuple[datetime, str] | None:
    if value is None:
        return None
    try:
        return _cursor_value(value)
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
        msg = "Invalid pagination cursor"
        raise ResearchDataError(msg) from exc


_INSTRUMENT_COLUMNS = (
    Instrument.instr_id,
    Instrument.canonical,
    Instrument.asset_class,
    Instrument.exchange,
    Instrument.settlement_currency,
    Instrument.tick_size,
    Instrument.lot_size,
    Instrument.is_tradable,
    Instrument.market_session_policy,
    Instrument.market_calendar_id,
)
_CALENDAR_COLUMNS = (
    MarketCalendar.calendar_id,
    MarketCalendar.code,
    MarketCalendar.source_kind,
    MarketCalendar.provider,
    MarketCalendar.source_reference,
    MarketCalendar.observation_id,
    MarketCalendar.coverage_start,
    MarketCalendar.coverage_end,
    MarketCalendar.observed_at,
)
_SESSION_COLUMNS = (MarketSession.session_id, MarketSession.opens_at, MarketSession.closes_at)


def reference_observation(
    session: Session, instrument_id: int, currency: str, bars: list[dict[str, str]]
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Read instrument, calendar provenance and sessions from one MVCC statement."""
    session_filter = MarketSession.calendar_id == MarketCalendar.calendar_id
    if bars:
        first, last = utc_timestamp(bars[0]["ts"]), utc_timestamp(bars[-1]["ts"])
        padding = timedelta(days=2)
        first -= min(padding, first - datetime.min.replace(tzinfo=UTC))
        last += min(padding, datetime.max.replace(tzinfo=UTC) - last)
        session_filter = and_(
            session_filter, MarketSession.closes_at >= first, MarketSession.opens_at <= last
        )
    else:
        session_filter = and_(session_filter, false())
    rows = (
        session.execute(
            select(
                *_INSTRUMENT_COLUMNS,
                *(column.label("calendar_" + column.key) for column in _CALENDAR_COLUMNS),
                *(column.label("session_" + column.key) for column in _SESSION_COLUMNS),
            )
            .select_from(Instrument)
            .outerjoin(MarketCalendar, Instrument.market_calendar_id == MarketCalendar.calendar_id)
            .outerjoin(MarketSession, session_filter)
            .where(Instrument.instr_id == instrument_id)
            .order_by(MarketSession.opens_at, MarketSession.session_id)
            .limit(MAX_IMPORT_BARS + 1)
        )
        .mappings()
        .all()
    )
    if not rows:
        msg = "Instrument not found"
        raise ResearchDataError(msg, status_code=404)
    terms = {column.key: rows[0][column.key] for column in _INSTRUMENT_COLUMNS}
    if terms["settlement_currency"] != currency:
        msg = "Import currency does not match the catalogue instrument currency"
        raise ResearchDataError(msg)
    for name in ("tick_size", "lot_size"):
        terms[name] = str(terms[name]) if terms[name] is not None else None
    if terms["asset_class"] == "crypto":
        return terms, {"kind": "continuous"}, []
    calendar = None
    if rows[0]["calendar_calendar_id"] is not None:
        calendar = {
            column.key: iso_timestamp(rows[0]["calendar_" + column.key])
            if isinstance(rows[0]["calendar_" + column.key], datetime)
            else rows[0]["calendar_" + column.key]
            for column in _CALENDAR_COLUMNS
        }
    sessions = (
        [
            {column.key: row["session_" + column.key] for column in _SESSION_COLUMNS}
            for row in rows
            if row["session_session_id"] is not None
        ]
        if len(rows) <= MAX_IMPORT_BARS
        else []
    )
    evidence = {
        "kind": "authoritative_calendar",
        "calendar": calendar,
        "sessions": [
            {
                key: iso_timestamp(value) if isinstance(value, datetime) else value
                for key, value in row.items()
            }
            for row in sessions
        ],
    }
    return terms, evidence, sessions


def _session_bounds(
    bar: dict[str, str],
    metadata: dict[str, Any],
    interval: timedelta,
    sessions: list[dict[str, Any]],
    starts: list[datetime],
) -> tuple[datetime | None, datetime | None]:
    ts = utc_timestamp(bar["ts"])
    start = utc_timestamp(bar["bar_open"]) if "bar_open" in bar else None
    end = utc_timestamp(bar["bar_close"]) if "bar_close" in bar else None
    if start is None and interval < timedelta(days=1):
        start = ts if metadata["timestamp_semantics"] == "bar_open" else ts - interval
        end = start + interval
    if start is None and interval == timedelta(days=1):
        index = bisect_right(starts, ts) - 1
        if index >= 0:
            selected_close = sessions[index]["closes_at"]
            selected_close = (
                selected_close.replace(tzinfo=UTC)
                if selected_close.tzinfo is None
                else selected_close.astimezone(UTC)
            )
            if (metadata["timestamp_semantics"] == "bar_open" and ts == starts[index]) or (
                metadata["timestamp_semantics"] == "bar_close" and ts == selected_close
            ):
                start, end = starts[index], selected_close
    return start, end


def timing_evidence(
    metadata: dict[str, Any],
    terms: dict[str, Any],
    bars: list[dict[str, str]],
    *,
    evidence: dict[str, Any],
    sessions: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate times using one already frozen reference observation."""
    failures: list[str] = []
    interval = timeframe_interval(metadata["timeframe"])
    continuous = terms["asset_class"] == "crypto" and terms["market_session_policy"] == "continuous"
    if not continuous and bars and (not evidence["calendar"] or not sessions):
        failures.append("Missing authoritative calendar coverage and session timing")
    starts = [
        row["opens_at"].replace(tzinfo=UTC)
        if row["opens_at"].tzinfo is None
        else row["opens_at"].astimezone(UTC)
        for row in sessions
    ]
    for bar in bars:
        ts = utc_timestamp(bar["ts"])
        if continuous:
            continuous_open = ts if metadata["timestamp_semantics"] == "bar_open" else ts - interval
            continuous_close = continuous_open + interval
            if "bar_open" in bar and (
                utc_timestamp(bar["bar_open"]) != continuous_open
                or utc_timestamp(bar["bar_close"]) != continuous_close
            ):
                failures.append("Explicit bar timing differs from continuous-market timeframe")
                break
            bar.update(
                bar_open=iso_timestamp(continuous_open), bar_close=iso_timestamp(continuous_close)
            )
            continue
        if not sessions:
            break
        start, end = _session_bounds(bar, metadata, interval, sessions, starts)
        if start is None or end is None:
            failures.append("Session bars require authoritative bar_open/bar_close timestamps")
            break
        index = bisect_right(starts, start) - 1
        if index < 0:
            failures.append("Bar is outside authoritative market sessions")
            break
        closes = sessions[index]["closes_at"]
        close = closes.replace(tzinfo=UTC) if closes.tzinfo is None else closes.astimezone(UTC)
        calendar = evidence["calendar"]
        if (
            not calendar.get("coverage_start")
            or not calendar.get("coverage_end")
            or not calendar.get("observed_at")
        ):
            failures.append("Missing authoritative calendar coverage observation")
            break
        if (
            start < utc_timestamp(calendar["coverage_start"])
            or end > utc_timestamp(calendar["coverage_end"])
            or end > close
            or (interval < timedelta(days=1) and end - start != interval)
            or interval > timedelta(days=1)
            or (interval >= timedelta(days=1) and (start != starts[index] or end != close))
        ):
            failures.append("Bar timing is incompatible with authoritative session coverage")
            break
        bar.update(bar_open=iso_timestamp(start), bar_close=iso_timestamp(end))
    product_failures = []
    if terms["asset_class"] not in {"crypto", "equity", "etf"} or not terms["is_tradable"]:
        product_failures.append(
            "Historical execution supports tradable cash equity, ETF "
            "and fully funded crypto spot only"
        )
    eligibility = {
        "training": not failures,
        "backtest": not failures and not product_failures,
        "training_refusals": list(dict.fromkeys(failures)),
        "backtest_refusals": list(dict.fromkeys(failures + product_failures)),
        "refusals": list(dict.fromkeys(failures + product_failures)),
        "provenance_strength": "owner_supplied_structural_receipt",
        "certified": False,
    }
    return evidence, eligibility
