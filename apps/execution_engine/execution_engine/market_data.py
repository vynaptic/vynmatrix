"""Market-data provider backed by the ``prices`` table (G5).

Gives the execution engine's ``StateProvider`` and ``PnLService`` an
owner-aware quote port. Shared feeds continue to read ``prices``; personally
licensed delayed BBOs read the normalized immutable equity-observation contract
only for their exact owner and only in paper mode.

DB reads are synchronous and low-volume (paper trading), so ``get_quote`` runs
the query inline; switch to ``run_in_executor`` if volume grows.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from lib_application.db.models import InstrumentPrice
from lib_application.services.equity_lineage import (
    EquityObservationAuthorityError,
    OwnerScopedDelayedBBOEvidence,
    load_owner_scoped_delayed_bbo,
)
from lib_application.services.instrument_resolution import (
    InstrumentResolutionError,
    resolve_instrument,
)
from lib_common.logging import get_logger
from lib_common.time_utils import now_utc
from lib_data.dataset import UnsupportedSessionTimingError, fixed_duration_interval
from lib_strategy.signals.utils import ensure_utc

logger = get_logger(__name__)

SessionFactory = Callable[[], AbstractContextManager[Session]]

_DEFAULT_SOURCE_PRIORITY = ("coinbase_live",)
_DEFAULT_TIMEFRAME_PRIORITY = ("1m", "5m", "15m", "1h", "1d")
_DEFAULT_MAX_STALENESS = timedelta(minutes=5)
_PAPER_ENVIRONMENT = "paper"
_LIVE_ENVIRONMENT = "live"
_PAPER_ONLY_AUTHORITY = "paper_only"


class PaperOnlyQuoteAuthorityError(RuntimeError):
    """An owner-scoped delayed quote was requested as live execution authority."""


class SqlPriceQuoteProvider:
    """Latest-close market-data provider over the ``prices`` table."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        source_priority: Sequence[str] | None = None,
        timeframe_priority: Sequence[str] | None = None,
        max_staleness: timedelta = _DEFAULT_MAX_STALENESS,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self._session_factory = session_factory
        self._source_priority = list(source_priority or _DEFAULT_SOURCE_PRIORITY)
        self._timeframe_priority = list(timeframe_priority or _DEFAULT_TIMEFRAME_PRIORITY)
        if max_staleness <= timedelta(0):
            msg = "max_staleness must be positive"
            raise ValueError(msg)
        self._max_staleness = max_staleness
        self._clock = clock

    async def get_quote(
        self,
        symbol: str,
        *,
        user_id: str,
        environment: str,
    ) -> dict[str, Any] | None:
        """Return ``{symbol, price, fetched_at, source, timeframe}`` for the most
        recent public candle, or ``None`` when the symbol/price is unknown.

        Ownership and environment are required at the execution port even
        though this provider reads only globally reusable public-feed rows.
        """
        _require_execution_scope(user_id=user_id, environment=environment)
        return self._latest_quote(symbol)

    def _latest_quote(self, symbol: str) -> dict[str, Any] | None:
        if not symbol:
            return None
        try:
            with self._session_factory() as session:
                instrument = resolve_instrument(session, symbol)
                if instrument is None:
                    return None
                row = self._latest_row(
                    session,
                    int(instrument.instr_id),
                    asset_class=instrument.asset_class,
                )
        except (InstrumentResolutionError, SQLAlchemyError, UnsupportedSessionTimingError) as exc:
            if isinstance(exc, UnsupportedSessionTimingError):
                logger.debug(
                    "No fixed-duration public execution quote path for session-based symbol %s",
                    symbol,
                )
            else:
                logger.exception("Price quote lookup failed for %s", symbol)
            return None
        if row is None:
            return None
        close, bar_open_at, bar_close_at, source, timeframe = row
        price = float(close)
        if price <= 0:
            return None
        return {
            "symbol": symbol,
            "price": price,
            "fetched_at": bar_close_at,
            "bar_open_at": bar_open_at,
            "source": source,
            "timeframe": timeframe,
        }

    def _latest_row(
        self,
        session: Session,
        instr_id: int,
        *,
        asset_class: str | None,
    ) -> tuple[Any, datetime, datetime, str, str] | None:
        """Return the first fresh, completed source/timeframe candidate."""
        observed_at = ensure_utc(self._clock())
        for source in self._source_priority:
            for timeframe in self._timeframe_priority:
                interval = fixed_duration_interval(timeframe, asset_class=asset_class)
                newest_open = observed_at - interval
                oldest_open = observed_at - self._max_staleness - interval
                stmt = (
                    select(
                        InstrumentPrice.close,
                        InstrumentPrice.ts,
                        InstrumentPrice.source,
                        InstrumentPrice.timeframe,
                    )
                    .where(
                        InstrumentPrice.instr_id == instr_id,
                        InstrumentPrice.source == source,
                        InstrumentPrice.timeframe == timeframe,
                        InstrumentPrice.ts <= newest_open.replace(tzinfo=None),
                        InstrumentPrice.ts >= oldest_open.replace(tzinfo=None),
                    )
                    .order_by(desc(InstrumentPrice.ts))
                    .limit(1)
                )
                row = session.execute(stmt).first()
                if row is None:
                    continue
                close, raw_open_at, row_source, row_timeframe = row
                bar_open_at = ensure_utc(raw_open_at)
                return (
                    close,
                    bar_open_at,
                    bar_open_at + interval,
                    str(row_source),
                    str(row_timeframe),
                )
        return None


class SqlOwnerScopedDelayedBBOProvider:
    """Read normalized delayed BBO evidence for one exact owner in paper mode."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        max_staleness: timedelta,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        if max_staleness <= timedelta(0):
            msg = "Owner-scoped delayed BBO max_staleness must be positive"
            raise ValueError(msg)
        self._session_factory = session_factory
        self._max_staleness = max_staleness
        self._clock = clock

    async def get_quote(
        self,
        symbol: str,
        *,
        user_id: str,
        environment: str,
    ) -> dict[str, Any] | None:
        owner, normalized_environment = _require_execution_scope(
            user_id=user_id,
            environment=environment,
        )
        if normalized_environment != _PAPER_ENVIRONMENT:
            msg = "Owner-scoped delayed BBOs are paper-only execution evidence"
            raise PaperOnlyQuoteAuthorityError(msg)
        if not symbol:
            return None
        try:
            with self._session_factory() as session:
                instrument = resolve_instrument(session, symbol, asset_class="equity")
                if instrument is None:
                    return None
                evidence = load_owner_scoped_delayed_bbo(
                    session,
                    instrument_id=int(instrument.instr_id),
                    entitlement_owner_user_id=owner,
                    observed_at=ensure_utc(self._clock()),
                    max_staleness=self._max_staleness,
                )
                if evidence is None:
                    return None
                return self._build_quote(symbol=symbol, evidence=evidence)
        except EquityObservationAuthorityError as exc:
            raise PaperOnlyQuoteAuthorityError(str(exc)) from exc
        except (InstrumentResolutionError, SQLAlchemyError):
            logger.exception("Owner-scoped delayed quote lookup failed for %s", symbol)
            return None

    @staticmethod
    def _build_quote(
        *,
        symbol: str,
        evidence: OwnerScopedDelayedBBOEvidence,
    ) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "provider_symbol": evidence.source_symbol,
            "price": float(evidence.last_trade_price),
            "timestamp": evidence.last_trade_at,
            "fetched_at": evidence.available_at,
            "available_at": evidence.available_at,
            "snapshot_at": evidence.snapshot_at,
            "bid_price": float(evidence.bid_price),
            "bid_size": evidence.bid_size,
            "bid_at": evidence.bid_at,
            "ask_price": float(evidence.ask_price),
            "ask_size": evidence.ask_size,
            "ask_at": evidence.ask_at,
            "currency": evidence.currency,
            "exchange": evidence.exchange,
            "source": evidence.provider,
            "timeframe": "delayed_quote",
            "market_data_class": "delayed",
            "execution_authority": _PAPER_ONLY_AUTHORITY,
            "lineage_id": evidence.lineage_id,
            "observation_id": evidence.observation_id,
            "content_sha256": evidence.content_sha256,
            "source_content_sha256": evidence.source_content_sha256,
            "raw_response_sha256": evidence.raw_response_sha256,
        }


class SqlExecutionQuoteProvider:
    """Route shared public quotes and owner-scoped paper-only delayed quotes."""

    def __init__(
        self,
        *,
        public_provider: SqlPriceQuoteProvider,
        owner_delayed_provider: SqlOwnerScopedDelayedBBOProvider,
    ) -> None:
        self._public_provider = public_provider
        self._owner_delayed_provider = owner_delayed_provider

    async def get_quote(
        self,
        symbol: str,
        *,
        user_id: str,
        environment: str,
    ) -> dict[str, Any] | None:
        owner, normalized_environment = _require_execution_scope(
            user_id=user_id,
            environment=environment,
        )
        public_quote = await self._public_provider.get_quote(
            symbol,
            user_id=owner,
            environment=normalized_environment,
        )
        if public_quote is not None:
            return public_quote
        if normalized_environment == _LIVE_ENVIRONMENT:
            return None
        return await self._owner_delayed_provider.get_quote(
            symbol,
            user_id=owner,
            environment=normalized_environment,
        )


def _require_execution_scope(*, user_id: str, environment: str) -> tuple[str, str]:
    owner = str(user_id or "").strip()
    normalized_environment = str(environment or "").strip().lower()
    if not owner:
        msg = "Execution quote lookup requires a non-empty user_id"
        raise ValueError(msg)
    if normalized_environment not in {_PAPER_ENVIRONMENT, _LIVE_ENVIRONMENT}:
        msg = f"Execution quote lookup received unsupported environment {environment!r}"
        raise ValueError(msg)
    return owner, normalized_environment
