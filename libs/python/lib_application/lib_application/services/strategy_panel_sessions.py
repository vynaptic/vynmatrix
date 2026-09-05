"""Authoritative persisted-session validation for synchronized equity panels."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    EquitySourceLineage,
    Instrument,
    MarketCalendar,
    MarketSession,
)
from lib_application.services.equity_lineage import (
    validate_equity_observation_authority,
)
from lib_common.hashing import canonical_json_hash
from lib_strategy.data_authority import DataUseScope
from lib_strategy.panels import OfficialSessionCutoff, PanelReadyInput, SessionAuthority

_MAX_FUTURE_SKEW = timedelta(seconds=5)
_DEFAULT_MAX_OBSERVATION_AGE = timedelta(hours=1)
_REQUIRED_SESSION_WINDOW_COUNT = 2


class StrategyPanelSessionError(RuntimeError):
    """A panel session cannot be reconciled to authoritative persisted state."""


def market_session_source_identity(calendar: MarketCalendar) -> str:
    """Return the canonical provenance identity frozen into a panel session."""

    return ":".join(
        (
            "market-calendar",
            str(calendar.code),
            str(calendar.source_kind),
            str(calendar.provider),
            str(calendar.source_reference),
        )
    )


def market_session_content_sha256(
    calendar: MarketCalendar,
    window: MarketSession,
    lineage: EquitySourceLineage,
) -> str:
    """Hash stable provider schedule identity and exact regular-session content.

    Observation identity, source-content revisions, retrieval metadata, and
    coverage remain mandatory validation/audit inputs but are deliberately
    excluded. Extending an unchanged official schedule must preserve the
    historical session identity pinned at decision time; correcting its
    venue, status, or regular-hours interval must change the identity.
    """

    return canonical_json_hash(
        {
            "schema": "authoritative-market-session-v1",
            "calendar_code": str(calendar.code),
            "source_kind": str(calendar.source_kind),
            "provider": str(calendar.provider),
            "source_reference": str(calendar.source_reference),
            "product": str(lineage.product),
            "endpoint": str(lineage.endpoint),
            "source_identity": str(lineage.source_identity),
            "timestamp_semantics": lineage.timestamp_semantics,
            "entitlement_scope": str(lineage.entitlement_scope),
            "session_date": _utc(window.opens_at).date().isoformat(),
            "session_status": "open_regular",
            "opens_at": _utc(window.opens_at).isoformat(),
            "closes_at": _utc(window.closes_at).isoformat(),
        }
    )


def validate_strategy_panel_sessions(  # noqa: PLR0912 - fail-closed authority ledger
    session: Session,
    *,
    panel: PanelReadyInput,
    now: datetime,
    max_observation_age: timedelta = _DEFAULT_MAX_OBSERVATION_AGE,
) -> None:
    """Fail closed unless both windows match one complete persisted calendar."""

    if max_observation_age <= timedelta(0):
        _invalid("maximum calendar observation age must be positive")
    instruments = list(
        session.scalars(
            select(Instrument)
            .where(Instrument.instr_id.in_(tuple(member.instrument_id for member in panel.members)))
            .with_for_update()
        )
    )
    by_id = {int(instrument.instr_id): instrument for instrument in instruments}
    if set(by_id) != {member.instrument_id for member in panel.members}:
        _invalid("panel contains an unknown catalogue instrument")
    for member in panel.members:
        instrument = by_id[member.instrument_id]
        if (
            str(instrument.asset_class) != "equity"
            or not bool(instrument.is_tradable)
            or str(instrument.market_session_policy) != "scheduled"
        ):
            _invalid("panel member does not match a tradable scheduled equity instrument")

    if panel.session.authority is SessionAuthority.RESEARCH_LIBRARY:
        if panel.data_use_scope is not DataUseScope.HISTORICAL_VALIDATION:
            _invalid("research calendar sessions cannot be used for forward decisions")
        return

    calendar_ids = {instrument.market_calendar_id for instrument in instruments}
    if None in calendar_ids or len(calendar_ids) != 1:
        _invalid("every panel member must share one authoritative market calendar")
    raw_calendar_id = next(iter(calendar_ids))
    if raw_calendar_id is None:
        _invalid("panel market calendar identity is unavailable")
    calendar_id = int(raw_calendar_id)
    calendar = session.get(MarketCalendar, calendar_id)
    if calendar is None:
        _invalid("panel market calendar is unavailable")
    expected_authority = (
        SessionAuthority.OFFICIAL_EXCHANGE
        if str(calendar.source_kind) == "exchange"
        else SessionAuthority.AUTHENTICATED_BROKER
    )
    if panel.session.authority is not expected_authority:
        _invalid("panel session authority does not match persisted calendar provenance")
    if panel.execution_session.authority is not expected_authority:
        _invalid("execution session authority does not match persisted calendar provenance")
    calendar_observation, calendar_lineage = validate_equity_observation_authority(
        session,
        observation_id=calendar.observation_id,
        expected_kind="calendar",
        cutoff=panel.cutoff,
        provider_authority_policy=panel.provider_authority_policy,
        expected_instrument_id=None,
    )
    if str(calendar_lineage.provider) != str(calendar.provider) or str(
        calendar_observation.source_record_identity
    ) != str(calendar.source_reference):
        _invalid("market calendar provenance differs from its immutable observation")

    if now.tzinfo is None or now.utcoffset() is None:
        _invalid("panel session validation clock must be timezone-aware")
    current = now.astimezone(UTC)
    coverage_start = _utc_required(calendar.coverage_start, "coverage_start")
    coverage_end = _utc_required(calendar.coverage_end, "coverage_end")
    observed_at = _utc_required(calendar.observed_at, "observed_at")
    if observed_at - current > _MAX_FUTURE_SKEW:
        _invalid("market calendar observation is future-dated")
    if panel.data_use_scope is not DataUseScope.HISTORICAL_VALIDATION and (
        current - observed_at > max_observation_age
    ):
        _invalid("market calendar observation is stale")

    windows = list(
        session.scalars(
            select(MarketSession)
            .where(
                MarketSession.calendar_id == calendar_id,
                MarketSession.opens_at.in_(
                    (
                        _stored(panel.session.opens_at),
                        _stored(panel.execution_session.opens_at),
                    )
                ),
            )
            .order_by(MarketSession.opens_at)
        )
    )
    if len(windows) != _REQUIRED_SESSION_WINDOW_COUNT:
        _invalid("decision and execution sessions are not both persisted")
    by_open = {_utc(window.opens_at): window for window in windows}
    _validate_window(
        supplied=panel.session,
        window=by_open.get(_utc(panel.session.opens_at)),
        calendar=calendar,
        calendar_lineage=calendar_lineage,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
    )
    _validate_window(
        supplied=panel.execution_session,
        window=by_open.get(_utc(panel.execution_session.opens_at)),
        calendar=calendar,
        calendar_lineage=calendar_lineage,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
    )
    cutoff = _utc(panel.cutoff)
    if not (_utc(panel.session.closes_at) <= cutoff < _utc(panel.execution_session.opens_at)):
        _invalid(
            "panel knowledge cutoff must be at or after the persisted decision-session "
            "close and precede the execution-session open"
        )
    intervening = session.scalar(
        select(MarketSession.session_id)
        .where(
            MarketSession.calendar_id == calendar_id,
            MarketSession.opens_at > _stored(panel.session.closes_at),
            MarketSession.opens_at < _stored(panel.execution_session.opens_at),
        )
        .limit(1)
    )
    if intervening is not None:
        _invalid("execution session is not the next persisted official session")


def _validate_window(
    *,
    supplied: OfficialSessionCutoff,
    window: MarketSession | None,
    calendar: MarketCalendar,
    calendar_lineage: EquitySourceLineage,
    coverage_start: datetime,
    coverage_end: datetime,
) -> None:
    if window is None:
        _invalid("panel session window is unavailable")
    opens_at = _utc(window.opens_at)
    closes_at = _utc(window.closes_at)
    supplied_open = _utc(supplied.opens_at)
    supplied_close = _utc(supplied.closes_at)
    if supplied_open != opens_at or supplied_close != closes_at:
        _invalid("panel session window differs from persisted regular hours")
    if not coverage_start <= opens_at < closes_at <= coverage_end:
        _invalid("market calendar coverage does not contain the complete session")
    if supplied.mic != str(calendar.code):
        _invalid("panel MIC/calendar identity does not match persisted provenance")
    if supplied.source_identity != market_session_source_identity(calendar):
        _invalid("panel session source identity does not match persisted provenance")
    if supplied.content_sha256 != market_session_content_sha256(
        calendar,
        window,
        calendar_lineage,
    ):
        _invalid("panel session digest does not match persisted calendar content")


def _stored(value: datetime) -> datetime:
    return _utc(value).replace(tzinfo=None)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _utc_required(value: datetime | None, field_name: str) -> datetime:
    if value is None:
        _invalid(f"market calendar {field_name} is missing")
    return _utc(value)


def _invalid(message: str) -> NoReturn:
    raise StrategyPanelSessionError(message)


__all__ = [
    "StrategyPanelSessionError",
    "market_session_content_sha256",
    "market_session_source_identity",
    "validate_strategy_panel_sessions",
]
