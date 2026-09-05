"""Default-off quarter-end gate for US Quality Compounder panel production."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, NoReturn, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    MarketCalendar,
    MarketSession,
    Strategy,
    StrategyPanelInputRevision,
    StrategyVersion,
)
from lib_strategy.equity_quality_compounder import QUALITY_COMPOUNDER_STRATEGY_VERSION

_STRATEGY_ID = "us_quality_compounder_v1"
_STRATEGY_VERSION = QUALITY_COMPOUNDER_STRATEGY_VERSION
_UNIVERSE = "SP500"


class QualityCompounderQuarterlyError(RuntimeError):
    """Quarterly production cannot prove a safe, active execution window."""


def _invalid(message: str) -> NoReturn:
    raise QualityCompounderQuarterlyError(message)


class QualityCompounderQuarterlyStatus(StrEnum):
    """Stable outcomes exposed to scheduler readiness and audit logs."""

    DISABLED = "disabled"
    NOT_QUARTER_END = "not_quarter_end"
    ALREADY_COMPLETE = "already_complete"
    PRODUCED = "produced"


@dataclass(frozen=True, slots=True)
class QualityCompounderQuarterlyResult:
    """One deterministic scheduling decision."""

    status: QualityCompounderQuarterlyStatus
    decision_session: date | None = None
    input_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class QualityCompounderQuarterlyWindow:
    """Detached official session pair safe to carry across network acquisition."""

    calendar_id: int
    decision_session_id: int
    decision_opens_at: datetime
    decision_closes_at: datetime
    execution_session_id: int
    execution_opens_at: datetime
    execution_closes_at: datetime


class QualityCompounderPanelProducer(Protocol):
    """Concrete producer that owns acquisition and its short DB transactions."""

    def produce(
        self,
        *,
        window: QualityCompounderQuarterlyWindow,
        started_at: datetime,
        complete_before: datetime,
    ) -> None:
        """Persist exactly one complete, validated panel input revision."""


SessionFactory = Callable[[], Any]


class QualityCompounderQuarterlyJob:
    """Run only between a completed official quarter end and the next open."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        producer: QualityCompounderPanelProducer,
        enabled: bool = False,
        calendar_code: str = "XNYS",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(session_factory):
            _invalid("quarterly job requires a session factory")
        if producer is None or not callable(getattr(producer, "produce", None)):
            _invalid("quarterly job requires a panel producer")
        if not isinstance(enabled, bool):
            _invalid("quarterly job enabled flag must be boolean")
        canonical_calendar = str(calendar_code).strip().upper()
        if not canonical_calendar:
            _invalid("quarterly job calendar code must be non-empty")
        self._session_factory = session_factory
        self._producer = producer
        self._enabled = enabled
        self._calendar_code = canonical_calendar
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    def run_once(self) -> QualityCompounderQuarterlyResult:
        """Produce at most one idempotent panel revision, or return why not."""

        if not self._enabled:
            return QualityCompounderQuarterlyResult(
                status=QualityCompounderQuarterlyStatus.DISABLED
            )
        now = _utc(self._clock(), field_name="quarterly clock")
        with self._session_factory() as session:
            _require_registered_version(session)
            window = _current_window(
                session,
                calendar_code=self._calendar_code,
                now=now,
            )
            decision_date = window.decision_opens_at.date()
            execution_date = window.execution_opens_at.date()
            if _quarter(decision_date) == _quarter(execution_date):
                return QualityCompounderQuarterlyResult(
                    status=QualityCompounderQuarterlyStatus.NOT_QUARTER_END,
                    decision_session=decision_date,
                )
            existing = _completed_revision(session, decision_session=decision_date)
            if existing is not None:
                return QualityCompounderQuarterlyResult(
                    status=QualityCompounderQuarterlyStatus.ALREADY_COMPLETE,
                    decision_session=decision_date,
                    input_sha256=str(existing.input_sha256),
                )
        self._producer.produce(
            window=window,
            started_at=now,
            complete_before=window.execution_opens_at,
        )
        completed_at = _utc(self._clock(), field_name="quarterly completion clock")
        if completed_at >= window.execution_opens_at:
            _invalid("quarterly producer completed after the execution-session open")
        with self._session_factory() as session:
            produced = _completed_revision(session, decision_session=decision_date)
            if produced is None:
                _invalid("panel producer returned without a durable input revision")
            produced_cutoff = _db_utc(produced.cutoff_at)
            if not now <= produced_cutoff <= completed_at:
                _invalid("panel producer persisted a cutoff outside its acquisition window")
            if _db_utc(produced.execute_not_before) != window.execution_opens_at:
                _invalid("panel producer persisted a different execution-session open")
            return QualityCompounderQuarterlyResult(
                status=QualityCompounderQuarterlyStatus.PRODUCED,
                decision_session=decision_date,
                input_sha256=str(produced.input_sha256),
            )


def _require_registered_version(session: Session) -> None:
    """Require immutable model lineage without granting execution authority."""

    strategy = session.get(Strategy, _STRATEGY_ID)
    version = session.scalar(
        select(StrategyVersion).where(
            StrategyVersion.strategy_id == _STRATEGY_ID,
            StrategyVersion.semver == _STRATEGY_VERSION,
        )
    )
    if strategy is None:
        _invalid("quality-compounder strategy registration is missing")
    if version is None or str(version.status) != "active":
        _invalid("quality-compounder strategy version is not active")


def _current_window(
    session: Session,
    *,
    calendar_code: str,
    now: datetime,
) -> QualityCompounderQuarterlyWindow:
    calendars = tuple(
        session.scalars(select(MarketCalendar).where(MarketCalendar.code == calendar_code))
    )
    if len(calendars) != 1:
        _invalid("quarterly job requires one exact official market calendar")
    calendar = calendars[0]
    if str(calendar.source_kind) != "exchange":
        _invalid("quarterly job requires official exchange calendar authority")
    calendar_id = int(calendar.calendar_id)
    decision = session.scalar(
        select(MarketSession)
        .where(
            MarketSession.calendar_id == calendar_id,
            MarketSession.closes_at <= _stored(now),
        )
        .order_by(MarketSession.closes_at.desc())
        .limit(1)
    )
    if decision is None:
        _invalid("quarterly job has no completed official session")
    execution = session.scalar(
        select(MarketSession)
        .where(
            MarketSession.calendar_id == calendar_id,
            MarketSession.opens_at > decision.closes_at,
        )
        .order_by(MarketSession.opens_at)
        .limit(1)
    )
    if execution is None:
        _invalid("quarterly job has no next official execution session")
    execution_opens = _db_utc(execution.opens_at)
    if now >= execution_opens:
        _invalid("quarterly panel window is already closed")
    return QualityCompounderQuarterlyWindow(
        calendar_id=calendar_id,
        decision_session_id=int(decision.session_id),
        decision_opens_at=_db_utc(decision.opens_at),
        decision_closes_at=_db_utc(decision.closes_at),
        execution_session_id=int(execution.session_id),
        execution_opens_at=execution_opens,
        execution_closes_at=_db_utc(execution.closes_at),
    )


def _completed_revision(
    session: Session,
    *,
    decision_session: date,
) -> StrategyPanelInputRevision | None:
    rows = tuple(
        session.scalars(
            select(StrategyPanelInputRevision).where(
                StrategyPanelInputRevision.strategy_id == _STRATEGY_ID,
                StrategyPanelInputRevision.strategy_version == _STRATEGY_VERSION,
                StrategyPanelInputRevision.universe_code == _UNIVERSE,
                StrategyPanelInputRevision.official_session_date == decision_session,
            )
        )
    )
    if len(rows) > 1:
        _invalid("quarterly session contains multiple durable panel revisions")
    return rows[0] if rows else None


def _quarter(value: date) -> tuple[int, int]:
    return value.year, (value.month - 1) // 3 + 1


def _utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _stored(value: datetime) -> datetime:
    return _utc(value, field_name="database timestamp").replace(tzinfo=None)


def _db_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = [
    "QualityCompounderPanelProducer",
    "QualityCompounderQuarterlyError",
    "QualityCompounderQuarterlyJob",
    "QualityCompounderQuarterlyResult",
    "QualityCompounderQuarterlyStatus",
    "QualityCompounderQuarterlyWindow",
]
