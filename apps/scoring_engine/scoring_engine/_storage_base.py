from __future__ import annotations

import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from types import ModuleType
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from lib_application.db.session import create_engine_for_env
from lib_application.outbox import OutboxStore
from lib_application.services.instrument_resolution import resolve_instrument
from lib_common.logging import get_logger
from lib_strategy.scoring.mode_horizon import normalize_mode_horizon

from .models import (
    ScoreComponent,
    ScoringUserBinding,
    SignalRecord,
)
from .storage_base import ScoreStore

logger = get_logger(__name__)

# Sentinel asset filter that matches no real symbol — used when a binding's
# asset_classes_allowed currently resolves to zero instruments, so it matches
# nothing rather than everything (BD-2).
_NO_ASSET_MATCH = "__no_asset_class_match__"

# Active session for the current scoring unit-of-work (request-scoped). When
# set, all AppScoreStore reads/writes share this session and defer their commit
# to ``unit_of_work``, so a signal, its scores, and its outbox events persist
# atomically (transactional outbox). A ContextVar keeps this correct across the
# async request handler's awaits and across concurrent requests.
_ACTIVE_SESSION: ContextVar[Session | None] = ContextVar("appscore_active_session", default=None)

_app_models_module: ModuleType | None = None
_APP_MODELS_AVAILABLE = False
try:
    from lib_application.db import models as _imported_app_models

    _app_models_module = _imported_app_models
    _APP_MODELS_AVAILABLE = True
except ImportError:
    # ``lib_application`` is an optional dependency for in-memory rigs;
    # ``AppScoreStore`` raises a clearer error at instantiation time.
    pass

app_models = _app_models_module


# SQLAlchemy implementation for the app schema.


def _build_store_engine(url: str) -> Engine:
    """Return the store engine for ``url``.

    PostgreSQL goes through the canonical helper so the pool is bounded by
    ``DB_POOL_SIZE`` / ``DB_MAX_OVERFLOW`` and instrumented like every other
    child. SQLAlchemy's bare default (five pooled plus ten overflow) let one
    scoring process hold three times its documented connection allowance.
    In-memory SQLite keeps one ``StaticPool`` connection so test rigs share
    state across sessions; the canonical helper deliberately exposes no pool
    class, which is why this branch stays here.
    """
    if url.startswith("sqlite"):
        engine_kwargs: dict[str, Any] = {"future": True}
        if ":memory:" in url:
            engine_kwargs.update(
                {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
            )
        return create_engine(url, **engine_kwargs)
    return create_engine_for_env(db_url=url)


class _StoreInfra(ScoreStore):
    """
    PostgreSQL-backed store using lib_application schema.

    Configure via DATABASE_URL (e.g., postgresql://user:pass@host:5432/dbname).
    Pass ``engine`` to share one bounded pool with the rest of the process
    instead of opening a second one for the store.
    """

    def __init__(
        self,
        url: str,
        *,
        bindings_cache_ttl_seconds: float = 5.0,
        engine: Engine | None = None,
    ) -> None:
        if not _APP_MODELS_AVAILABLE or app_models is None:
            msg = "lib_application models are required for AppScoreStore"
            raise RuntimeError(msg)
        self._engine = engine if engine is not None else _build_store_engine(url)
        self._is_sqlite = self._engine.dialect.name == "sqlite"
        self._signal_seq = 0
        self._asset_score_seq = 0
        self._sector_score_seq = 0
        self._market_score_seq = 0
        self._bindings_cache: tuple[str, float, list[ScoringUserBinding]] | None = None
        if bindings_cache_ttl_seconds < 0:
            msg = "bindings_cache_ttl_seconds must be nonnegative"
            raise ValueError(msg)
        self._bindings_cache_ttl_seconds = bindings_cache_ttl_seconds
        # Alembic is the unconditional PostgreSQL schema authority. ``create_all``
        # is limited to isolated SQLite unit stores, which have no migration step.
        if self._is_sqlite:
            app_models.Base.metadata.create_all(self._engine)
        self._outbox = OutboxStore(self._session)
        self.supports_canonical_signals = True

    def _cached_bindings(self, signature: str) -> list[ScoringUserBinding] | None:
        """Reuse a projection only after its complete current authority is re-read."""
        cached = self._bindings_cache
        if self._bindings_cache_ttl_seconds <= 0 or cached is None:
            return None
        cached_signature, cached_at, bindings = cached
        if signature != cached_signature:
            return None
        if time.monotonic() - cached_at >= self._bindings_cache_ttl_seconds:
            return None
        return bindings

    def _store_bindings_cache(self, bindings: list[ScoringUserBinding], *, signature: str) -> None:
        """Publish one complete projection and authority signature atomically."""
        if self._bindings_cache_ttl_seconds <= 0:
            return
        self._bindings_cache = (signature, time.monotonic(), bindings)

    def invalidate_bindings_cache(self) -> None:
        """Drop the cached binding projection after a binding write."""
        self._bindings_cache = None

    @property
    def engine(self) -> Engine:
        """The bound engine, so one process can share a single pool."""
        return self._engine

    @contextmanager
    def _session(self) -> Iterator[Session]:
        """Yield a session for a read/write.

        Inside a ``unit_of_work`` the shared request session is reused and is
        neither committed nor closed here (the unit of work owns its lifecycle);
        otherwise a fresh session is opened and closed. Standalone writes still
        commit via ``_maybe_commit``.
        """
        active = _ACTIVE_SESSION.get()
        if active is not None:
            yield active
            return
        session = Session(self._engine, expire_on_commit=False)
        try:
            yield session
        finally:
            session.close()

    def _maybe_commit(self, session: Session) -> None:
        """Commit a standalone write. Inside a ``unit_of_work`` the unit of work
        owns the single commit, so this is a no-op."""
        if _ACTIVE_SESSION.get() is None:
            session.commit()

    @contextmanager
    def unit_of_work(self) -> Iterator[Session]:
        """Run a group of writes (signal + scores + outbox events) in one
        transaction so they persist all-or-nothing.

        Every AppScoreStore read/write made inside the ``with`` block shares one
        session and sees the others' uncommitted changes; ``Session.begin``
        commits once on success and rolls back on any exception. Re-entrant: a
        nested call joins the outer unit of work (the outermost owns commit).
        """
        existing = _ACTIVE_SESSION.get()
        if existing is not None:
            yield existing
            return
        session = Session(self._engine, expire_on_commit=False)
        token = _ACTIVE_SESSION.set(session)
        try:
            with session.begin():
                yield session
        finally:
            _ACTIVE_SESSION.reset(token)
            session.close()

    def get_session(self) -> Session:
        """Return a new standalone DB session for external callers.

        Not unit-of-work aware — for callers outside the scoring atomic path
        that manage their own transaction."""
        return Session(self._engine, expire_on_commit=False)

    def _resolve_strategy_version(
        self,
        session: Session,
        strategy_id: str,
        strategy_version: str | None = None,
    ) -> int | None:
        if app_models is None or not strategy_id:
            return None
        query = session.query(app_models.StrategyVersion).filter(
            app_models.StrategyVersion.strategy_id == strategy_id
        )
        if strategy_version:
            # A signal's semantic version is immutable lineage. Mapping it to a
            # newer row merely because that row was released later corrupts
            # attribution for replays, delayed delivery, and mixed-version
            # rollouts. An unknown explicit version therefore stays unresolved.
            query = query.filter(app_models.StrategyVersion.semver == strategy_version)
        else:
            query = query.filter(app_models.StrategyVersion.status == "active")
        row = query.order_by(
            app_models.StrategyVersion.released_at.desc(),
            app_models.StrategyVersion.strat_ver_id.desc(),
        ).first()
        return int(row.strat_ver_id) if row else None

    def _require_strategy(
        self,
        session: Session,
        strategy_id: str,
        asset_class: str | None,
    ) -> Any:
        if app_models is None:
            msg = "app models unavailable"
            raise RuntimeError(msg)
        if not strategy_id:
            msg = "Signal strategy_id is required"
            raise ValueError(msg)
        existing = session.query(app_models.Strategy).filter_by(strategy_id=strategy_id).first()
        if existing is None:
            msg = (
                f"Unknown strategy {strategy_id!r}; provision it through the "
                "control-plane strategy catalogue before ingest"
            )
            raise ValueError(msg)
        if asset_class and existing.asset_class and existing.asset_class != asset_class:
            msg = (
                f"Strategy {strategy_id!r} is catalogued for asset_class "
                f"{existing.asset_class!r}, not {asset_class!r}"
            )
            raise ValueError(msg)
        return existing

    def _resolve_instrument(
        self,
        session: Session,
        symbol: str,
        asset_class: str | None,
        instrument_id: str | None = None,
        *,
        allow_create: bool = False,
        settlement_currency: str | None = None,
    ) -> Any:
        if app_models is None:
            msg = "app models unavailable"
            raise RuntimeError(msg)
        instr = resolve_instrument(
            session,
            symbol,
            instrument_id=instrument_id,
            asset_class=asset_class,
            allow_create=allow_create,
            settlement_currency=settlement_currency,
        )
        if instr is None:
            msg = (
                f"Unknown instrument {symbol!r}; provision it through the "
                "source-controlled instrument catalogue before ingest"
            )
            raise ValueError(msg)
        return instr

    def resolve_instrument_id(self, symbol: str) -> int | None:
        """Resolve symbol to instrument_id via DB lookup.

        Args:
            symbol: Symbol to resolve (e.g., "BTCUSD")

        Returns:
            Instrument ID if found, None otherwise
        """
        if app_models is None:
            return None
        with self._session() as s:
            instr = resolve_instrument(s, symbol)
            return int(instr.instr_id) if instr is not None else None

    def resolve_instrument_asset_class(self, symbol: str) -> str | None:
        """Resolve symbol to its catalogued asset class via DB lookup.

        Used by the market-context router to pick the per-asset-class price
        feed; an unknown symbol returns ``None`` and the router fails closed.
        """
        if app_models is None:
            return None
        with self._session() as s:
            instr = resolve_instrument(s, symbol)
            return str(instr.asset_class) if instr is not None else None

    def _resolve_sector(
        self, session: Session, sector_code: str | None, asset_class: str | None
    ) -> Any | None:
        if app_models is None or not sector_code:
            return None
        sector = session.query(app_models.Sector).filter_by(code=sector_code).first()
        if sector is None:
            msg = (
                f"Unknown sector {sector_code!r}; provision it through the "
                "control-plane instrument catalogue before ingest"
            )
            raise ValueError(msg)
        if asset_class and sector.asset_class and sector.asset_class != asset_class:
            msg = (
                f"Sector {sector_code!r} is catalogued for asset_class "
                f"{sector.asset_class!r}, not {asset_class!r}"
            )
            raise ValueError(msg)
        return sector

    def _require_instrument_sector(
        self,
        session: Session,
        instr_id: int,
        sector_id: int,
    ) -> None:
        if app_models is None:
            msg = "app models unavailable"
            raise RuntimeError(msg)
        exists = (
            session.query(app_models.InstrumentSector)
            .filter_by(instr_id=instr_id, sector_id=sector_id)
            .first()
        )
        if exists is not None:
            return
        msg = (
            f"Instrument {instr_id} is not assigned to sector {sector_id}; "
            "provision the relationship through the control-plane catalogue before ingest"
        )
        raise ValueError(msg)

    def _normalize_horizon(self, horizon: str | None) -> str:
        # Single source of truth shared with the feedback ModePerformance writer
        # so written buckets match looked-up buckets (lib_strategy.scoring.mode_horizon).
        return normalize_mode_horizon(horizon)

    def _signal_from_row(
        self,
        row: Any,
        instrument: Any,
        sector_code: str | None,
    ) -> SignalRecord:
        def _as_float(value: Any) -> float | None:
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        meta = dict(row.signal_meta or {})
        features = row.features or {}
        if features and "features" not in meta:
            meta["features"] = features
        meta.setdefault("strategy_version", row.strat_ver_id)
        meta.setdefault("source", row.source_runner)
        meta.setdefault("signal_id", row.signal_id)
        meta.setdefault("canonical_signal_id", row.signal_id)

        horizon_days = None
        if row.horizon_seconds:
            horizon_days = float(row.horizon_seconds) / 86400.0

        return SignalRecord(
            signal_id=str(meta.get("signal_id") or row.signal_id),
            strategy_id=row.strategy_id,
            strategy_type=row.source_runner or meta.get("strategy_type") or "unknown",
            symbol=instrument.canonical,
            expected_return=_as_float(row.expected_return),
            predicted_risk=_as_float(row.predicted_risk),
            horizon_days=horizon_days,
            action=row.action,
            confidence=float(row.confidence or 0.0),
            timestamp=row.ts,
            metadata=meta,
            sector=sector_code or meta.get("sector"),
            industry=meta.get("industry"),
            index=meta.get("index"),
            entry_price=_as_float(meta.get("entry_price") or row.entry_price),
            stop_loss=_as_float(meta.get("stop_loss")),
            take_profit=_as_float(meta.get("take_profit")),
            size_hint=_as_float(meta.get("size_hint")),
            asset_class=instrument.asset_class,
            instrument_id=str(instrument.instr_id),
            strategy_version=str(meta.get("strategy_version") or ""),
            run_id=row.run_id or meta.get("run_id"),
            source=meta.get("source"),
            external_signal_id=row.external_signal_id,
            expires_at=row.expires_at,
            score_value=float(row.raw_score or 0.0),
        )

    def _components_from_payload(self, payload: Sequence[dict[str, Any]]) -> list[ScoreComponent]:
        comps: list[ScoreComponent] = []
        for item in payload:
            try:
                comps.append(
                    ScoreComponent(
                        strategy_id=item["strategy_id"],
                        weight=float(item["weight"]),
                        raw_value=float(item["raw_value"]),
                        weighted_value=float(item["weighted_value"]),
                        confidence=float(item.get("confidence", 0.0)),
                        timestamp=datetime.fromisoformat(item["timestamp"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                # Skip malformed component payloads (missing fields, bad
                # types, or unparseable timestamps); newer rows with the
                # full schema will still be admitted.
                continue
        return comps

    def _collect_instrument_ids(self, rows: Iterable[Any]) -> set[int]:
        ids: set[int] = set()
        for row in rows:
            for item in row.instruments_allowed or []:
                if isinstance(item, int):
                    ids.add(item)
                elif isinstance(item, str) and item.isdigit():
                    ids.add(int(item))
        return ids

    def _collect_sector_ids(self, rows: Iterable[Any]) -> set[int]:
        ids: set[int] = set()
        for row in rows:
            for item in row.sectors_allowed or []:
                if isinstance(item, int):
                    ids.add(item)
                elif isinstance(item, str) and item.isdigit():
                    ids.add(int(item))
        return ids

    def _load_instrument_map(self, session: Session, instrument_ids: set[int]) -> dict[int, Any]:
        if app_models is None:
            return {}
        if not instrument_ids:
            return {}
        instruments = (
            session.query(app_models.Instrument)
            .filter(app_models.Instrument.instr_id.in_(instrument_ids))
            .all()
        )
        return {instrument.instr_id: instrument for instrument in instruments}

    def _load_sector_map(self, session: Session, sector_ids: set[int]) -> dict[int, str]:
        if app_models is None:
            return {}
        if not sector_ids:
            return {}
        sectors = (
            session.query(app_models.Sector)
            .filter(app_models.Sector.sector_id.in_(sector_ids))
            .all()
        )
        return {sector.sector_id: sector.code for sector in sectors}

    def _load_sizing_profiles(
        self, session: Session, rows: Iterable[Any]
    ) -> dict[int | None, dict[str, Any]]:
        if app_models is None:
            return {}
        profile_ids = {row.sizing_profile_id for row in rows if row.sizing_profile_id}
        if not profile_ids:
            return {}
        profiles = (
            session.query(app_models.SizingProfile)
            .filter(app_models.SizingProfile.profile_id.in_(profile_ids))
            .all()
        )
        result: dict[int | None, dict[str, Any]] = {}
        for profile in profiles:
            payload = dict(profile.params or {})
            payload["method"] = profile.method
            payload["profile_name"] = profile.name
            result[profile.profile_id] = payload
        return result

    def _resolve_asset_filter(self, row: Any, instrument_map: dict[int, Any]) -> list[str]:
        asset_filter: list[str] = []
        for item in row.instruments_allowed or []:
            if isinstance(item, str) and not item.isdigit():
                asset_filter.append(item)
                continue
            if isinstance(item, int):
                instr = instrument_map.get(item)
                if instr:
                    asset_filter.append(instr.canonical)
                continue
            if isinstance(item, str) and item.isdigit():
                instr = instrument_map.get(int(item))
                if instr:
                    asset_filter.append(instr.canonical)

        if asset_filter:
            return asset_filter

        if row.asset_classes_allowed:
            # BD-2: an asset-class allow-list must match only instruments in those
            # classes, NOT every asset (the old ["*"] dropped the restriction).
            # Resolve to the concrete symbols; if none currently exist for those
            # classes, match nothing (sentinel) rather than everything.
            allowed_classes = {c for c in row.asset_classes_allowed if c}
            class_symbols = [
                instr.canonical
                for instr in instrument_map.values()
                if getattr(instr, "asset_class", None) in allowed_classes
            ]
            return class_symbols or [_NO_ASSET_MATCH]

        return []
