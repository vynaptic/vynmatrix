"""Watermark tracking for market-data consumers.

Persists the last processed committed bar per worker/symbol/timeframe
so that after a restart the signal worker can catch up on missed bars
without reprocessing already-handled data.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lib_common.logging import get_logger
from lib_common.time_utils import now_utc

logger = get_logger(__name__)

SessionFactory = Callable[[], Any]


def _as_utc(timestamp: datetime) -> datetime:
    """Normalize database-naive and timezone-aware timestamps for ordering."""
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _optional_utc(timestamp: datetime | None) -> datetime | None:
    return _as_utc(timestamp) if timestamp is not None else None


def _as_db_timestamp(timestamp: datetime) -> datetime:
    """Return the repository's timezone-naive UTC database representation."""
    return _as_utc(timestamp).replace(tzinfo=None)


def _timestamp_sql(statement: str, *parameter_names: str) -> Any:
    """Build text SQL with explicit datetime bind types across DB drivers."""
    from sqlalchemy import DateTime, bindparam, text  # noqa: PLC0415

    return text(statement).bindparams(
        *(bindparam(name, type_=DateTime()) for name in parameter_names)
    )


class WatermarkError(RuntimeError):
    """Base class for durable consumer-checkpoint failures."""


class WatermarkIdentityError(WatermarkError):
    """A worker checkpoint belongs to a different feed identity."""


class HistoricalRebuildRequiredError(WatermarkError):
    """Persisted history changed at/before a consumer checkpoint."""


class RebuildGenerationChangedError(HistoricalRebuildRequiredError):
    """A new historical mutation arrived while a rebuild was in progress."""


@dataclass(frozen=True)
class WatermarkState:
    """Fresh durable state for one source-scoped consumer checkpoint."""

    symbol: str
    timeframe: str
    instr_id: int | None
    source: str
    last_ts: datetime
    rebuild_from_ts: datetime | None
    rebuild_generation: int

    @property
    def rebuild_pending(self) -> bool:
        return self.rebuild_from_ts is not None


class Watermark:
    """Tracks the latest processed bar timestamp per (worker, symbol, timeframe).

    Storage is backed by a simple ``watermarks`` table. A missing or unwritable
    table always stops the consumer instead of silently replaying or skipping
    history after a restart.

    Schema (created by migration or manually)::

        CREATE TABLE IF NOT EXISTS watermarks (
            worker_id   VARCHAR(100) NOT NULL,
            symbol      VARCHAR(50)  NOT NULL,
            timeframe   VARCHAR(20)  NOT NULL,
            instr_id    INTEGER      NOT NULL,
            source      VARCHAR(50)  NOT NULL,
            last_ts     TIMESTAMP    NOT NULL,
            rebuild_from_ts TIMESTAMP,
            rebuild_generation BIGINT NOT NULL DEFAULT 0,
            updated_at  TIMESTAMP    NOT NULL DEFAULT NOW(),
            PRIMARY KEY (worker_id, symbol, timeframe)
        );
    """

    _EPOCH = datetime(2000, 1, 1, tzinfo=UTC)

    def __init__(
        self,
        session_factory: SessionFactory,
        worker_id: str,
        *,
        source: str,
    ) -> None:
        self._session_factory = session_factory
        self._worker_id = worker_id
        self._source = source.strip()
        if not self._source:
            msg = "Watermark source must not be empty"
            raise ValueError(msg)
        self._cache: dict[str, datetime] = {}  # "symbol:timeframe" → last_ts
        self._table_available: bool | None = None
        self._lock = threading.RLock()

    def _key(self, symbol: str, timeframe: str) -> str:
        return f"{symbol}:{timeframe}"

    def get(self, symbol: str, timeframe: str) -> datetime:
        """Return the last processed timestamp, or epoch if none recorded."""
        with self._lock:
            key = self._key(symbol, timeframe)
            if key in self._cache:
                return self._cache[key]

            state = self.get_state(symbol, timeframe)
            self._cache[key] = state.last_ts
            return state.last_ts

    def get_state(self, symbol: str, timeframe: str) -> WatermarkState:
        """Read marker/generation state directly from durable storage."""
        if self._table_available is False:
            msg = "Durable watermarks table is unavailable"
            raise WatermarkError(msg)
        try:
            with self._session_factory() as session:
                state = self._read_state(session, symbol, timeframe, for_update=False)
            self._table_available = True
            return state  # noqa: TRY300
        except WatermarkError:
            raise
        except Exception as exc:
            if self._table_available is None:
                logger.warning(
                    "watermarks table not available; watermark tracking disabled",
                    exc_info=True,
                )
                self._table_available = False
            msg = "Durable watermarks table is unavailable"
            raise WatermarkError(msg) from exc

    @classmethod
    def is_initial(cls, timestamp: datetime) -> bool:
        """Return whether ``timestamp`` represents an absent durable checkpoint."""
        return _as_utc(timestamp) == cls._EPOCH

    def ensure(self, symbol: str, timeframe: str, *, instr_id: int) -> WatermarkState:
        """Persist an epoch checkpoint so later history requests a bootstrap."""
        state = self._persist(symbol, timeframe, self._EPOCH, instr_id=instr_id)
        self.remember(state)
        return state

    def initialize_on_session(
        self,
        session: Any,
        symbol: str,
        timeframe: str,
        last_ts: datetime,
        *,
        instr_id: int,
    ) -> WatermarkState:
        """Create the first checkpoint inside a caller-owned transaction.

        This is intentionally separate from :meth:`set`: a strategy cold start
        must commit its initial model snapshot and every feed checkpoint as one
        unit. The caller must invoke :meth:`remember` only after that
        transaction commits.
        """
        if isinstance(instr_id, bool) or instr_id <= 0:
            msg = "Watermark instr_id must be a positive integer"
            raise ValueError(msg)
        candidate = _as_utc(last_ts)
        now = _as_db_timestamp(now_utc())
        session.execute(
            _timestamp_sql(
                """
                INSERT INTO watermarks (
                    worker_id, symbol, timeframe, instr_id, source,
                    last_ts, rebuild_generation, updated_at
                )
                VALUES (:wid, :sym, :tf, :instr_id, :source, :ts, 0, :now)
                ON CONFLICT (worker_id, symbol, timeframe) DO NOTHING
                """,
                "ts",
                "now",
            ),
            {
                "wid": self._worker_id,
                "sym": symbol,
                "tf": timeframe,
                "instr_id": instr_id,
                "source": self._source,
                "ts": _as_db_timestamp(candidate),
                "now": now,
            },
        )
        current = self._read_state(session, symbol, timeframe, for_update=True)
        self._validate_identity(current, instr_id=instr_id)
        if current.rebuild_pending:
            msg = (
                f"Historical rebuild pending for {symbol}/{timeframe} "
                f"from {current.rebuild_from_ts} generation={current.rebuild_generation}"
            )
            raise HistoricalRebuildRequiredError(msg)
        if self.is_initial(current.last_ts) and not self.is_initial(candidate):
            result = session.execute(
                _timestamp_sql(
                    """
                    UPDATE watermarks
                    SET last_ts = :ts, updated_at = :now
                    WHERE worker_id = :wid
                      AND symbol = :sym
                      AND timeframe = :tf
                      AND instr_id = :instr_id
                      AND source = :source
                      AND last_ts = :initial_ts
                      AND rebuild_generation = :generation
                      AND rebuild_from_ts IS NULL
                    """,
                    "ts",
                    "now",
                    "initial_ts",
                ),
                {
                    "wid": self._worker_id,
                    "sym": symbol,
                    "tf": timeframe,
                    "instr_id": instr_id,
                    "source": self._source,
                    "ts": _as_db_timestamp(candidate),
                    "now": now,
                    "initial_ts": _as_db_timestamp(self._EPOCH),
                    "generation": current.rebuild_generation,
                },
            )
            if int(result.rowcount or 0) != 1:
                msg = f"Cold-start checkpoint changed for {symbol}/{timeframe}"
                raise HistoricalRebuildRequiredError(msg)
            current = WatermarkState(
                symbol=symbol,
                timeframe=timeframe,
                instr_id=instr_id,
                source=self._source,
                last_ts=candidate,
                rebuild_from_ts=None,
                rebuild_generation=current.rebuild_generation,
            )
        elif current.last_ts != candidate:
            msg = (
                f"Cold-start checkpoint already initialized for {symbol}/{timeframe}: "
                f"stored={current.last_ts} candidate={candidate}"
            )
            raise WatermarkError(msg)
        self._table_available = True
        return current

    def validate_state(self, state: WatermarkState, *, instr_id: int) -> None:
        """Fail closed when persisted and configured feed identities diverge."""
        self._validate_identity(state, instr_id=instr_id)

    def request_rebuilds(
        self,
        feeds: list[tuple[str, str, int]],
        *,
        rebuild_from: datetime,
    ) -> list[WatermarkState]:
        """Expand one feed correction to the complete shared strategy state.

        A ``SignalWorker`` owns one strategy instance across all subscribed
        symbols. Rebuilding only the corrected symbol would leave cross-symbol
        state dependent on process timing, so every feed checkpoint receives the
        same earliest replay boundary under one transaction.
        """
        boundary = _as_utc(rebuild_from)
        try:
            with self._session_factory() as session:
                requested: list[WatermarkState] = []
                for symbol, timeframe, instr_id in sorted(feeds):
                    current = self._read_state(session, symbol, timeframe, for_update=True)
                    self._validate_identity(current, instr_id=instr_id)
                    effective_boundary = (
                        min(current.rebuild_from_ts, boundary)
                        if current.rebuild_from_ts is not None
                        else boundary
                    )
                    if current.rebuild_from_ts != effective_boundary:
                        session.execute(
                            _timestamp_sql(
                                """
                                UPDATE watermarks
                                SET rebuild_from_ts = :boundary,
                                    rebuild_generation = rebuild_generation + 1,
                                    updated_at = :now
                                WHERE worker_id = :wid
                                  AND symbol = :sym
                                  AND timeframe = :tf
                                """,
                                "boundary",
                                "now",
                            ),
                            {
                                "boundary": _as_db_timestamp(effective_boundary),
                                "now": _as_db_timestamp(now_utc()),
                                "wid": self._worker_id,
                                "sym": symbol,
                                "tf": timeframe,
                            },
                        )
                    requested.append(self._read_state(session, symbol, timeframe, for_update=False))
                session.commit()
        except WatermarkError:
            raise
        except Exception as exc:
            msg = "Failed to request a full historical strategy rebuild"
            raise WatermarkError(msg) from exc
        return requested

    def set(
        self,
        symbol: str,
        timeframe: str,
        last_ts: datetime,
        *,
        instr_id: int,
    ) -> None:
        """Advance the watermark monotonically in memory and durable storage."""
        candidate = _as_utc(last_ts)
        with self._lock:
            state = self._persist(symbol, timeframe, candidate, instr_id=instr_id)
            self._cache[self._key(symbol, timeframe)] = state.last_ts

    def lock_for_processing(
        self,
        session: Any,
        symbol: str,
        timeframe: str,
        *,
        instr_id: int,
    ) -> WatermarkState:
        """Lock and validate a checkpoint before mutating live strategy state."""
        state = self._read_state(session, symbol, timeframe, for_update=True)
        self._validate_identity(state, instr_id=instr_id)
        if state.rebuild_pending:
            msg = (
                f"Historical rebuild pending for {symbol}/{timeframe} "
                f"from {state.rebuild_from_ts} generation={state.rebuild_generation}"
            )
            raise HistoricalRebuildRequiredError(msg)
        return state

    def advance_locked(
        self,
        session: Any,
        state: WatermarkState,
        last_ts: datetime,
    ) -> WatermarkState:
        """Advance a checkpoint already locked by :meth:`lock_for_processing`."""
        candidate = _as_utc(last_ts)
        if candidate <= state.last_ts:
            return state
        result = session.execute(
            _timestamp_sql(
                """
                UPDATE watermarks
                SET last_ts = :ts, updated_at = :now
                WHERE worker_id = :wid
                  AND symbol = :sym
                  AND timeframe = :tf
                  AND instr_id = :instr_id
                  AND source = :source
                  AND last_ts = :expected_last_ts
                  AND rebuild_generation = :expected_generation
                  AND rebuild_from_ts IS NULL
                """,
                "ts",
                "now",
                "expected_last_ts",
            ),
            {
                "wid": self._worker_id,
                "sym": state.symbol,
                "tf": state.timeframe,
                "instr_id": state.instr_id,
                "source": self._source,
                "expected_generation": state.rebuild_generation,
                "expected_last_ts": _as_db_timestamp(state.last_ts),
                "ts": _as_db_timestamp(candidate),
                "now": _as_db_timestamp(now_utc()),
            },
        )
        if int(result.rowcount or 0) != 1:
            msg = f"Checkpoint changed while processing {state.symbol}/{state.timeframe}"
            raise HistoricalRebuildRequiredError(msg)
        return WatermarkState(
            symbol=state.symbol,
            timeframe=state.timeframe,
            instr_id=state.instr_id,
            source=state.source,
            last_ts=candidate,
            rebuild_from_ts=None,
            rebuild_generation=state.rebuild_generation,
        )

    def remember(self, state: WatermarkState) -> None:
        """Update the process cache after the caller commits an external transaction."""
        with self._lock:
            self._cache[self._key(state.symbol, state.timeframe)] = state.last_ts

    def acknowledge_rebuilds(
        self,
        states: list[WatermarkState],
        *,
        target_last_ts: dict[tuple[str, str], datetime] | None = None,
    ) -> list[WatermarkState]:
        """Clear captured markers atomically after a successful full-core replay."""
        if not states:
            return []
        try:
            with self._session_factory() as session:
                acknowledged = self.acknowledge_rebuilds_on_session(
                    session,
                    states,
                    target_last_ts=target_last_ts,
                )
                session.commit()
        except WatermarkError:
            raise
        except Exception as exc:
            msg = "Failed to acknowledge historical rebuild generation"
            raise WatermarkError(msg) from exc

        for state in acknowledged:
            self.remember(state)
        return acknowledged

    def acknowledge_rebuilds_on_session(
        self,
        session: Any,
        states: list[WatermarkState],
        *,
        target_last_ts: dict[tuple[str, str], datetime] | None = None,
    ) -> list[WatermarkState]:
        """Clear captured rebuild fences inside the caller's transaction.

        The caller owns commit/rollback and must call :meth:`remember` for each
        returned state only after a successful commit. This lets a strategy
        runtime persist its rebuilt model snapshot and acknowledge every feed
        generation through one database boundary.
        """
        targets = target_last_ts or {}
        locked: list[WatermarkState] = []
        for expected in sorted(states, key=lambda item: (item.symbol, item.timeframe)):
            current = self._read_state(
                session,
                expected.symbol,
                expected.timeframe,
                for_update=True,
            )
            self._validate_identity(current, instr_id=int(expected.instr_id or 0))
            if (
                not current.rebuild_pending
                or current.rebuild_generation != expected.rebuild_generation
                or current.rebuild_from_ts != expected.rebuild_from_ts
                or current.last_ts != expected.last_ts
            ):
                msg = (
                    "Historical rebuild generation changed for "
                    f"{expected.symbol}/{expected.timeframe}"
                )
                raise RebuildGenerationChangedError(msg)
            locked.append(current)

        now = _as_db_timestamp(now_utc())
        acknowledged: list[WatermarkState] = []
        for current in locked:
            rebuild_from = current.rebuild_from_ts
            if rebuild_from is None:
                msg = (
                    "Historical rebuild marker disappeared before "
                    f"acknowledgement for {current.symbol}/{current.timeframe}"
                )
                raise RebuildGenerationChangedError(msg)
            target = _as_utc(
                targets.get(
                    (current.symbol, current.timeframe),
                    current.last_ts,
                )
            )
            if target < current.last_ts and not self.is_initial(current.last_ts):
                msg = (
                    f"Historical rebuild cannot regress {current.symbol}/"
                    f"{current.timeframe} from {current.last_ts} to {target}"
                )
                raise WatermarkError(msg)
            result = session.execute(
                _timestamp_sql(
                    """
                    UPDATE watermarks
                    SET last_ts = :target_last_ts,
                        rebuild_from_ts = NULL,
                        updated_at = :now
                    WHERE worker_id = :wid
                      AND symbol = :sym
                      AND timeframe = :tf
                      AND rebuild_generation = :generation
                      AND rebuild_from_ts = :rebuild_from
                      AND last_ts = :last_ts
                    """,
                    "target_last_ts",
                    "now",
                    "rebuild_from",
                    "last_ts",
                ),
                {
                    "wid": self._worker_id,
                    "sym": current.symbol,
                    "tf": current.timeframe,
                    "generation": current.rebuild_generation,
                    "rebuild_from": _as_db_timestamp(rebuild_from),
                    "last_ts": _as_db_timestamp(current.last_ts),
                    "target_last_ts": _as_db_timestamp(target),
                    "now": now,
                },
            )
            if int(result.rowcount or 0) != 1:
                msg = (
                    "Historical rebuild acknowledgement lost its fence for "
                    f"{current.symbol}/{current.timeframe}"
                )
                raise RebuildGenerationChangedError(msg)
            acknowledged.append(
                WatermarkState(
                    symbol=current.symbol,
                    timeframe=current.timeframe,
                    instr_id=current.instr_id,
                    source=current.source,
                    last_ts=target,
                    rebuild_from_ts=None,
                    rebuild_generation=current.rebuild_generation,
                )
            )
        return acknowledged

    def _persist(
        self,
        symbol: str,
        timeframe: str,
        last_ts: datetime,
        *,
        instr_id: int,
    ) -> WatermarkState:
        if self._table_available is False:
            msg = "Durable watermarks table is unavailable"
            raise WatermarkError(msg)
        if isinstance(instr_id, bool) or instr_id <= 0:
            msg = "Watermark instr_id must be a positive integer"
            raise ValueError(msg)
        try:
            with self._session_factory() as session:
                session.execute(
                    _timestamp_sql(
                        """
                        INSERT INTO watermarks (
                            worker_id, symbol, timeframe, instr_id, source,
                            last_ts, rebuild_generation, updated_at
                        )
                        VALUES (:wid, :sym, :tf, :instr_id, :source, :ts, 0, :now)
                        ON CONFLICT (worker_id, symbol, timeframe)
                        DO UPDATE SET last_ts = :ts, updated_at = :now
                        WHERE watermarks.last_ts < :ts
                          AND watermarks.instr_id = :instr_id
                          AND watermarks.source = :source
                          AND watermarks.rebuild_from_ts IS NULL
                        """,
                        "ts",
                        "now",
                    ),
                    {
                        "wid": self._worker_id,
                        "sym": symbol,
                        "tf": timeframe,
                        "instr_id": instr_id,
                        "source": self._source,
                        "ts": _as_db_timestamp(last_ts),
                        "now": _as_db_timestamp(now_utc()),
                    },
                )
                state = self._read_state(session, symbol, timeframe, for_update=False)
                self._validate_identity(state, instr_id=instr_id)
                if state.rebuild_pending:
                    msg = (
                        f"Historical rebuild pending for {symbol}/{timeframe} "
                        f"from {state.rebuild_from_ts} generation={state.rebuild_generation}"
                    )
                    raise HistoricalRebuildRequiredError(msg)  # noqa: TRY301
                session.commit()
                self._table_available = True
                return state
        except WatermarkError:
            raise
        except Exception as exc:
            if self._table_available is None:
                logger.warning(
                    "watermarks table not available; watermark tracking disabled",
                    exc_info=True,
                )
                self._table_available = False
            logger.warning(
                "Failed to persist watermark for %s/%s/%s",
                self._worker_id,
                symbol,
                timeframe,
                exc_info=True,
            )
            msg = "Failed to persist durable signal-worker watermark"
            raise WatermarkError(msg) from exc

    def _read_state(
        self,
        session: Any,
        symbol: str,
        timeframe: str,
        *,
        for_update: bool,
    ) -> WatermarkState:
        from sqlalchemy import text  # noqa: PLC0415

        statement = """
            SELECT instr_id, source, last_ts, rebuild_from_ts, rebuild_generation
            FROM watermarks
            WHERE worker_id = :wid AND symbol = :sym AND timeframe = :tf
        """
        if for_update and session.bind is not None and session.bind.dialect.name != "sqlite":
            statement += " FOR UPDATE"
        row = (
            session.execute(
                text(statement),
                {"wid": self._worker_id, "sym": symbol, "tf": timeframe},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return WatermarkState(
                symbol=symbol,
                timeframe=timeframe,
                instr_id=None,
                source=self._source,
                last_ts=self._EPOCH,
                rebuild_from_ts=None,
                rebuild_generation=0,
            )
        last_ts = row["last_ts"]
        rebuild_from = row["rebuild_from_ts"]
        return WatermarkState(
            symbol=symbol,
            timeframe=timeframe,
            instr_id=int(row["instr_id"]),
            source=str(row["source"]),
            last_ts=_as_utc(
                last_ts if isinstance(last_ts, datetime) else datetime.fromisoformat(str(last_ts))
            ),
            rebuild_from_ts=_optional_utc(
                rebuild_from
                if isinstance(rebuild_from, datetime) or rebuild_from is None
                else datetime.fromisoformat(str(rebuild_from))
            ),
            rebuild_generation=int(row["rebuild_generation"] or 0),
        )

    def _validate_identity(self, state: WatermarkState, *, instr_id: int) -> None:
        if state.instr_id is None:
            msg = f"Missing durable watermark for {state.symbol}/{state.timeframe}"
            raise WatermarkIdentityError(msg)
        if state.instr_id != instr_id or state.source != self._source:
            msg = (
                f"Watermark feed identity mismatch for {state.symbol}/{state.timeframe}: "
                f"stored instr_id={state.instr_id} source={state.source!r}, "
                f"runtime instr_id={instr_id} source={self._source!r}"
            )
            raise WatermarkIdentityError(msg)


__all__ = [
    "HistoricalRebuildRequiredError",
    "RebuildGenerationChangedError",
    "Watermark",
    "WatermarkError",
    "WatermarkIdentityError",
    "WatermarkState",
]
