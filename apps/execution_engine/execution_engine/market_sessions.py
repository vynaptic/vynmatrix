"""Database-backed authoritative market-session admission.

The execution engine never manufactures weekdays, holidays, or venue hours.
Scheduled instruments are admitted only when a fresh exchange/broker calendar
batch covers the evaluation time and contains an open regular-session window.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from lib_application.db.models import (
    EquityObservation,
    EquitySourceLineage,
    Instrument,
    MarketCalendar,
    MarketSession,
)
from lib_application.services.strategy_panel_sessions import market_session_content_sha256
from lib_common.logging import get_logger
from lib_strategy.signals.signal import Signal

logger = get_logger(__name__)

SessionFactory = Callable[[], AbstractContextManager[Session]]
MAX_FUTURE_OBSERVATION_SKEW = timedelta(seconds=5)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class MarketSessionDecision:
    """Result of evaluating one persisted instrument's session policy."""

    is_open: bool
    reason: str
    message: str
    calendar_code: str | None = None
    provider: str | None = None


class SqlMarketSessionProvider:
    """Evaluate persisted continuous/scheduled market-session policy."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        max_observation_age: timedelta,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        if max_observation_age <= timedelta(0):
            msg = "max_observation_age must be positive"
            raise ValueError(msg)
        self._session_factory = session_factory
        self._max_observation_age = max_observation_age
        self._clock = clock

    def evaluate(self, signal: Signal) -> MarketSessionDecision:
        """Return an auditable admission decision for ``signal``."""
        try:
            instrument_id = int(signal.instrument_id) if signal.instrument_id is not None else None
        except (TypeError, ValueError):
            instrument_id = None
        if instrument_id is None or instrument_id <= 0:
            return MarketSessionDecision(
                is_open=False,
                reason="market_session_identity_invalid",
                message="Execution requires a persisted instrument_id for market-session gating",
            )

        now = _utc(self._clock())
        try:
            with self._session_factory() as session:
                instrument = session.get(Instrument, instrument_id)
                identity_error = self._validate_identity(instrument, signal)
                if identity_error is not None:
                    return identity_error
                assert instrument is not None  # narrowed by _validate_identity
                if instrument.market_session_policy == "continuous":
                    return MarketSessionDecision(
                        is_open=True,
                        reason="continuous_market",
                        message=(
                            f"{instrument.canonical} is explicitly classified as a "
                            "continuous crypto market"
                        ),
                    )
                if instrument.market_session_policy != "scheduled":
                    return MarketSessionDecision(
                        is_open=False,
                        reason="market_session_policy_invalid",
                        message=(
                            f"Instrument {instrument_id} has unsupported market-session "
                            f"policy {instrument.market_session_policy!r}"
                        ),
                    )
                return self._evaluate_scheduled(session, instrument, now)
        except SQLAlchemyError:
            logger.exception(
                "Market-session lookup failed",
                instrument_id=instrument_id,
                symbol=signal.symbol,
            )
            return MarketSessionDecision(
                is_open=False,
                reason="market_session_unavailable",
                message="Authoritative market-session state is unavailable",
            )

    def evaluate_pinned_rebalance_session(  # noqa: PLR0911
        self,
        signal: Signal,
        *,
        expected_open_at: datetime,
        expected_content_sha256: str,
    ) -> MarketSessionDecision:
        """Require the current regular session to match frozen semantic content."""
        try:
            instrument_id = int(signal.instrument_id) if signal.instrument_id is not None else None
        except (TypeError, ValueError):
            instrument_id = None
        if instrument_id is None or instrument_id <= 0:
            return MarketSessionDecision(
                is_open=False,
                reason="market_session_identity_invalid",
                message="Rebalance execution requires a persisted instrument_id",
            )
        expected_open = _utc(expected_open_at)
        now = _utc(self._clock())
        try:
            with self._session_factory() as session:
                instrument = session.get(Instrument, instrument_id)
                identity_error = self._validate_identity(instrument, signal)
                if identity_error is not None:
                    return identity_error
                assert instrument is not None
                if (
                    str(instrument.asset_class) != "equity"
                    or instrument.market_session_policy != "scheduled"
                    or instrument.market_calendar_id is None
                ):
                    return MarketSessionDecision(
                        is_open=False,
                        reason="rebalance_execution_session_mismatch",
                        message="Portfolio rebalance requires a scheduled equity instrument",
                    )
                calendar = session.get(MarketCalendar, int(instrument.market_calendar_id))
                if calendar is None:
                    return MarketSessionDecision(
                        is_open=False,
                        reason="market_session_unavailable",
                        message="Authoritative rebalance calendar is unavailable",
                    )
                provenance_error = self._validate_calendar(calendar, now)
                if provenance_error is not None:
                    return provenance_error
                window = session.execute(
                    select(MarketSession)
                    .where(
                        MarketSession.calendar_id == calendar.calendar_id,
                        MarketSession.opens_at <= now,
                        MarketSession.closes_at > now,
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if window is None:
                    return MarketSessionDecision(
                        is_open=False,
                        reason="market_closed",
                        message="Decision-pinned rebalance session is not currently open",
                        calendar_code=str(calendar.code),
                        provider=str(calendar.provider),
                    )
                if _utc(window.opens_at) != expected_open:
                    return MarketSessionDecision(
                        is_open=False,
                        reason="rebalance_execution_session_mismatch",
                        message=(
                            f"Calendar {calendar.code} is open for "
                            f"{_utc(window.opens_at).isoformat()}, not the pinned "
                            f"{expected_open.isoformat()}"
                        ),
                        calendar_code=str(calendar.code),
                        provider=str(calendar.provider),
                    )
                coverage_start = _utc(calendar.coverage_start)  # type: ignore[arg-type]
                coverage_end = _utc(calendar.coverage_end)  # type: ignore[arg-type]
                if not (
                    coverage_start <= _utc(window.opens_at) < _utc(window.closes_at) <= coverage_end
                ):
                    return self._unavailable_calendar(
                        calendar,
                        "does not completely cover the pinned session",
                    )
                observation = (
                    session.get(EquityObservation, str(calendar.observation_id))
                    if calendar.observation_id is not None
                    else None
                )
                lineage = (
                    session.get(EquitySourceLineage, str(observation.lineage_id))
                    if observation is not None
                    else None
                )
                if (
                    observation is None
                    or lineage is None
                    or str(observation.observation_kind) != "calendar"
                    or str(observation.disposition) != "observed"
                    or observation.available_at is None
                    or str(lineage.provider) != str(calendar.provider)
                    or str(observation.source_record_identity) != str(calendar.source_reference)
                ):
                    return self._unavailable_calendar(
                        calendar,
                        "has incomplete immutable observation lineage",
                    )
                actual_digest = market_session_content_sha256(
                    calendar,
                    window,
                    lineage,
                )
                if actual_digest != expected_content_sha256:
                    return MarketSessionDecision(
                        is_open=False,
                        reason="rebalance_execution_session_digest_mismatch",
                        message="Authoritative session content changed after the plan was frozen",
                        calendar_code=str(calendar.code),
                        provider=str(calendar.provider),
                    )
                return MarketSessionDecision(
                    is_open=True,
                    reason="rebalance_execution_session_pinned",
                    message=(
                        f"Pinned calendar {calendar.code} is open until "
                        f"{_utc(window.closes_at).isoformat()}"
                    ),
                    calendar_code=str(calendar.code),
                    provider=str(calendar.provider),
                )
        except SQLAlchemyError:
            logger.exception(
                "Pinned rebalance-session lookup failed",
                instrument_id=instrument_id,
                symbol=signal.symbol,
            )
            return MarketSessionDecision(
                is_open=False,
                reason="market_session_unavailable",
                message="Authoritative rebalance-session state is unavailable",
            )

    def evaluate_tradability(self, signal: Signal) -> MarketSessionDecision:
        """Validate canonical identity and the instrument's execution boundary."""
        try:
            instrument_id = int(signal.instrument_id) if signal.instrument_id is not None else None
        except (TypeError, ValueError):
            instrument_id = None
        if instrument_id is None or instrument_id <= 0:
            return MarketSessionDecision(
                is_open=False,
                reason="instrument_identity_invalid",
                message="Execution requires a persisted instrument_id for tradability gating",
            )
        try:
            with self._session_factory() as session:
                instrument = session.get(Instrument, instrument_id)
                decision = self._validate_identity(instrument, signal)
                if decision is not None:
                    return decision
                assert instrument is not None
                return MarketSessionDecision(
                    is_open=True,
                    reason="instrument_tradable",
                    message=f"Instrument {instrument.instr_id} is explicitly tradable",
                )
        except SQLAlchemyError:
            logger.exception(
                "Instrument tradability lookup failed",
                instrument_id=instrument_id,
                symbol=signal.symbol,
            )
            return MarketSessionDecision(
                is_open=False,
                reason="instrument_tradability_unavailable",
                message="Authoritative instrument tradability is unavailable",
            )

    @staticmethod
    def _validate_identity(
        instrument: Instrument | None,
        signal: Signal,
    ) -> MarketSessionDecision | None:
        if instrument is None:
            return MarketSessionDecision(
                is_open=False,
                reason="market_session_identity_invalid",
                message=f"Unknown persisted instrument_id {signal.instrument_id!r}",
            )
        if signal.asset_class and str(instrument.asset_class) != str(signal.asset_class):
            return MarketSessionDecision(
                is_open=False,
                reason="market_session_identity_invalid",
                message=(
                    f"Instrument {instrument.instr_id} asset class does not match "
                    "the canonical signal"
                ),
            )
        if not instrument.is_tradable:
            return MarketSessionDecision(
                is_open=False,
                reason="instrument_not_tradable",
                message=(
                    f"Instrument {instrument.instr_id} ({instrument.canonical}) is "
                    "reference-only; execute a concrete futures/options instrument"
                ),
            )
        if (
            instrument.market_session_policy == "continuous"
            and str(instrument.asset_class) != "crypto"
        ):
            return MarketSessionDecision(
                is_open=False,
                reason="market_session_policy_invalid",
                message="Only persisted crypto instruments may use continuous-market policy",
            )
        return None

    def _evaluate_scheduled(
        self,
        session: Session,
        instrument: Instrument,
        now: datetime,
    ) -> MarketSessionDecision:
        calendar = instrument.market_calendar
        if calendar is None:
            return MarketSessionDecision(
                is_open=False,
                reason="market_session_unavailable",
                message=(f"Instrument {instrument.instr_id} has no authoritative market calendar"),
            )
        provenance_error = self._validate_calendar(calendar, now)
        if provenance_error is not None:
            return provenance_error

        assert calendar.coverage_start is not None
        assert calendar.coverage_end is not None
        coverage_start = _utc(calendar.coverage_start)
        coverage_end = _utc(calendar.coverage_end)
        if not coverage_start <= now < coverage_end:
            return MarketSessionDecision(
                is_open=False,
                reason="market_session_unavailable",
                message=(f"Calendar {calendar.code} does not cover {now.isoformat()}"),
                calendar_code=str(calendar.code),
                provider=str(calendar.provider),
            )

        session_window = session.execute(
            select(MarketSession)
            .where(
                MarketSession.calendar_id == calendar.calendar_id,
                MarketSession.opens_at <= now,
                MarketSession.closes_at > now,
            )
            .limit(1)
        ).scalar_one_or_none()
        if session_window is None:
            return MarketSessionDecision(
                is_open=False,
                reason="market_closed",
                message=(
                    f"Authoritative calendar {calendar.code} reports no open "
                    f"regular session at {now.isoformat()}"
                ),
                calendar_code=str(calendar.code),
                provider=str(calendar.provider),
            )
        return MarketSessionDecision(
            is_open=True,
            reason="market_open",
            message=(
                f"Authoritative calendar {calendar.code} is open until "
                f"{_utc(session_window.closes_at).isoformat()}"
            ),
            calendar_code=str(calendar.code),
            provider=str(calendar.provider),
        )

    def _validate_calendar(
        self,
        calendar: MarketCalendar,
        now: datetime,
    ) -> MarketSessionDecision | None:
        if (
            calendar.coverage_start is None
            or calendar.coverage_end is None
            or calendar.observed_at is None
        ):
            return self._unavailable_calendar(calendar, "has never been synchronized")
        observed_at = _utc(calendar.observed_at)
        if observed_at - now > MAX_FUTURE_OBSERVATION_SKEW:
            return self._unavailable_calendar(
                calendar,
                "has a future-dated observation",
            )
        if now - observed_at > self._max_observation_age:
            return self._unavailable_calendar(
                calendar,
                "observation is stale",
            )
        return None

    @staticmethod
    def _unavailable_calendar(
        calendar: MarketCalendar,
        detail: str,
    ) -> MarketSessionDecision:
        return MarketSessionDecision(
            is_open=False,
            reason="market_session_unavailable",
            message=f"Authoritative calendar {calendar.code} {detail}",
            calendar_code=str(calendar.code),
            provider=str(calendar.provider),
        )


__all__ = [
    "MarketSessionDecision",
    "SqlMarketSessionProvider",
]
