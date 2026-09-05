"""Atomic persistence for broker/exchange trading-calendar observations."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from lib_application.db.models import Instrument, MarketCalendar, MarketSession
from lib_application.services.instrument_resolution import (
    resolve_broker_instrument_identity,
    resolve_instrument,
)
from lib_common.time_utils import now_utc

_MAX_COVERAGE = timedelta(days=31)
_MAX_FUTURE_OBSERVATION_SKEW = timedelta(seconds=5)
_CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_.:-]{0,99}$")
_PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")


@dataclass(frozen=True)
class MarketSessionWindow:
    """One official regular-session interval in a synchronized batch."""

    opens_at: datetime
    closes_at: datetime


@dataclass(frozen=True, slots=True)
class MarketCalendarTarget:
    """Canonical instrument plus its exact official-provider identity."""

    instrument_id: int
    canonical_symbol: str
    product_id: str
    broker_instrument_type: str | None


@dataclass(frozen=True)
class _CalendarIdentity:
    code: str
    source_kind: str
    provider: str
    source_reference: str


@dataclass(frozen=True)
class _CalendarCoverage:
    observed_at: datetime
    starts_at: datetime
    ends_at: datetime
    updated_at: datetime


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"{field} must include an explicit UTC offset"
        raise ValueError(msg)
    return value.astimezone(UTC)


def _normalize_identity(
    *,
    code: str,
    source_kind: str,
    provider: str,
    source_reference: str,
) -> _CalendarIdentity:
    normalized = _CalendarIdentity(
        code=str(code or "").strip().upper(),
        source_kind=str(source_kind or "").strip().lower(),
        provider=str(provider or "").strip().lower(),
        source_reference=str(source_reference or "").strip(),
    )
    if _CODE_PATTERN.fullmatch(normalized.code) is None:
        msg = "Market calendar code must use uppercase letters, digits, '.', ':', '_' or '-'"
        raise ValueError(msg)
    if normalized.source_kind not in {"broker", "exchange"}:
        msg = "Market calendar source_kind must be 'broker' or 'exchange'"
        raise ValueError(msg)
    if _PROVIDER_PATTERN.fullmatch(normalized.provider) is None:
        msg = "Market calendar provider is invalid"
        raise ValueError(msg)
    if not normalized.source_reference.startswith("https://"):
        msg = "Market calendar source_reference must be an HTTPS official source"
        raise ValueError(msg)
    return normalized


def _normalize_coverage(
    *,
    observed_at: datetime,
    coverage_start: datetime,
    coverage_end: datetime,
    now: datetime | None,
) -> _CalendarCoverage:
    coverage = _CalendarCoverage(
        observed_at=_aware_utc(observed_at, field="observed_at"),
        starts_at=_aware_utc(coverage_start, field="coverage_start"),
        ends_at=_aware_utc(coverage_end, field="coverage_end"),
        updated_at=_aware_utc(now or now_utc(), field="now"),
    )
    if coverage.observed_at - coverage.updated_at > _MAX_FUTURE_OBSERVATION_SKEW:
        msg = "Market calendar observed_at cannot be future-dated"
        raise ValueError(msg)
    if coverage.ends_at <= coverage.starts_at:
        msg = "Market calendar coverage_end must be after coverage_start"
        raise ValueError(msg)
    if coverage.ends_at - coverage.starts_at > _MAX_COVERAGE:
        msg = "Market calendar coverage cannot exceed 31 days"
        raise ValueError(msg)
    return coverage


def _normalize_windows(
    windows: Sequence[MarketSessionWindow],
    *,
    coverage: _CalendarCoverage,
) -> tuple[MarketSessionWindow, ...]:
    normalized = tuple(
        sorted(
            (
                MarketSessionWindow(
                    opens_at=_aware_utc(window.opens_at, field="session.opens_at"),
                    closes_at=_aware_utc(window.closes_at, field="session.closes_at"),
                )
                for window in windows
            ),
            key=lambda window: window.opens_at,
        )
    )
    previous_close: datetime | None = None
    for window in normalized:
        if window.closes_at <= window.opens_at:
            msg = "Market session closes_at must be after opens_at"
            raise ValueError(msg)
        if window.opens_at < coverage.starts_at or window.closes_at > coverage.ends_at:
            msg = "Every market session must be contained by the declared coverage"
            raise ValueError(msg)
        if previous_close is not None and window.opens_at < previous_close:
            msg = "Market session windows cannot overlap"
            raise ValueError(msg)
        previous_close = window.closes_at
    return normalized


def _normalize_instrument_ids(instrument_ids: Sequence[int]) -> tuple[int, ...]:
    normalized = tuple(sorted({int(value) for value in instrument_ids}))
    if not normalized or any(value <= 0 for value in normalized):
        msg = "Market calendar sync requires positive instrument_ids"
        raise ValueError(msg)
    return normalized


def load_market_calendar_targets(
    session: Session,
    *,
    broker_code: str,
    symbols: Sequence[str],
    require_instrument_type: bool = False,
) -> tuple[MarketCalendarTarget, ...]:
    """Resolve configured canonical selectors through the broker catalogue.

    Venue identifiers never live in deployment configuration. A missing,
    duplicated, non-tradable, crypto, or incompletely typed mapping fails the
    entire supervised writer at startup/sync.
    """
    normalized_broker = str(broker_code or "").strip().lower()
    selectors = tuple(str(symbol or "").strip() for symbol in symbols if str(symbol or "").strip())
    if not normalized_broker:
        msg = "Market calendar target resolution requires a broker code"
        raise ValueError(msg)
    if not selectors:
        msg = "Market calendar target resolution requires canonical symbols"
        raise ValueError(msg)

    targets: list[MarketCalendarTarget] = []
    instrument_ids: set[int] = set()
    venue_identities: set[tuple[str, str | None]] = set()
    for selector in selectors:
        instrument = resolve_instrument(session, selector)
        if instrument is None:
            msg = f"Unknown canonical market-calendar instrument: {selector!r}"
            raise ValueError(msg)
        instrument_id = int(instrument.instr_id)
        if instrument_id in instrument_ids:
            msg = (
                "Market-calendar selectors must resolve uniquely; "
                f"{selector!r} repeats instr_id={instrument_id}"
            )
            raise ValueError(msg)
        if str(instrument.asset_class) == "crypto":
            msg = f"Crypto instrument {instrument.canonical!r} is explicitly continuous"
            raise ValueError(msg)
        if not bool(instrument.is_tradable):
            msg = (
                f"Reference-only instrument {instrument.canonical!r} "
                "cannot receive a trading calendar"
            )
            raise ValueError(msg)

        identity = resolve_broker_instrument_identity(
            session,
            instrument_id=instrument_id,
            broker_code=normalized_broker,
        )
        if identity is None:
            msg = (
                f"Instrument {instrument.canonical!r} has no "
                f"{normalized_broker!r} catalogue mapping"
            )
            raise ValueError(msg)
        product_id = identity.broker_instrument_id
        if product_id is None or not product_id.isdecimal() or int(product_id) <= 0:
            msg = (
                f"Instrument {instrument.canonical!r} requires a positive numeric "
                f"{normalized_broker!r} broker_instrument_id"
            )
            raise ValueError(msg)
        instrument_type = identity.broker_instrument_type
        if require_instrument_type and instrument_type is None:
            msg = (
                f"Instrument {instrument.canonical!r} requires a typed "
                f"{normalized_broker!r} broker_instrument_type"
            )
            raise ValueError(msg)
        venue_identity = (product_id, instrument_type)
        if venue_identity in venue_identities:
            msg = (
                f"Market-calendar selectors repeat {normalized_broker!r} "
                f"venue identity {venue_identity!r}"
            )
            raise ValueError(msg)

        targets.append(
            MarketCalendarTarget(
                instrument_id=instrument_id,
                canonical_symbol=str(instrument.canonical),
                product_id=product_id,
                broker_instrument_type=instrument_type,
            )
        )
        instrument_ids.add(instrument_id)
        venue_identities.add(venue_identity)
    return tuple(targets)


def _get_or_create_calendar(
    session: Session,
    *,
    identity: _CalendarIdentity,
) -> MarketCalendar:
    calendar: MarketCalendar | None = session.execute(
        select(MarketCalendar).where(MarketCalendar.code == identity.code).with_for_update()
    ).scalar_one_or_none()
    if calendar is None:
        calendar = MarketCalendar(
            code=identity.code,
            source_kind=identity.source_kind,
            provider=identity.provider,
            source_reference=identity.source_reference,
        )
        session.add(calendar)
        session.flush()
    return calendar


def _lock_calendar_instruments(
    session: Session,
    *,
    calendar_id: int,
    requested_ids: tuple[int, ...],
) -> list[Instrument]:
    instruments = list(
        session.execute(
            select(Instrument)
            .where(
                or_(
                    Instrument.instr_id.in_(requested_ids),
                    Instrument.market_calendar_id == calendar_id,
                )
            )
            .with_for_update()
        ).scalars()
    )
    found_ids = {int(row.instr_id) for row in instruments}
    if not set(requested_ids).issubset(found_ids):
        msg = "Market calendar sync references an unknown instrument"
        raise ValueError(msg)
    if any(
        int(row.instr_id) in requested_ids and str(row.asset_class) == "crypto"
        for row in instruments
    ):
        msg = "Crypto instruments are explicitly continuous and cannot use a scheduled calendar"
        raise ValueError(msg)
    return instruments


def replace_market_calendar(
    session: Session,
    *,
    code: str,
    source_kind: str,
    provider: str,
    source_reference: str,
    observed_at: datetime,
    coverage_start: datetime,
    coverage_end: datetime,
    windows: Sequence[MarketSessionWindow],
    instrument_ids: Sequence[int],
    observation_id: str | None = None,
    now: datetime | None = None,
) -> MarketCalendar:
    """Replace one authoritative coverage batch and assign exact instruments.

    The caller owns the transaction. Deleting old windows, inserting the new
    complete coverage, and assigning instruments therefore become visible to
    execution atomically.
    """
    identity = _normalize_identity(
        code=code,
        source_kind=source_kind,
        provider=provider,
        source_reference=source_reference,
    )
    coverage = _normalize_coverage(
        observed_at=observed_at,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        now=now,
    )
    normalized_windows = _normalize_windows(windows, coverage=coverage)
    normalized_instrument_ids = _normalize_instrument_ids(instrument_ids)
    calendar = _get_or_create_calendar(session, identity=identity)
    instruments = _lock_calendar_instruments(
        session,
        calendar_id=int(calendar.calendar_id),
        requested_ids=normalized_instrument_ids,
    )

    calendar.source_kind = identity.source_kind
    calendar.provider = identity.provider
    calendar.source_reference = identity.source_reference
    calendar.observation_id = observation_id
    calendar.observed_at = coverage.observed_at
    calendar.coverage_start = coverage.starts_at
    calendar.coverage_end = coverage.ends_at
    calendar.updated_at = coverage.updated_at

    session.execute(delete(MarketSession).where(MarketSession.calendar_id == calendar.calendar_id))
    session.add_all(
        [
            MarketSession(
                calendar_id=calendar.calendar_id,
                opens_at=window.opens_at,
                closes_at=window.closes_at,
            )
            for window in normalized_windows
        ]
    )
    for instrument in instruments:
        instrument.market_calendar_id = (
            int(calendar.calendar_id)
            if int(instrument.instr_id) in normalized_instrument_ids
            else None
        )
    session.flush()
    return calendar


__all__ = [
    "MarketCalendarTarget",
    "MarketSessionWindow",
    "load_market_calendar_targets",
    "replace_market_calendar",
]
