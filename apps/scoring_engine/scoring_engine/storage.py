from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session, aliased

from lib_application.services.instrument_resolution import resolve_instrument
from lib_common.logging import get_logger

from ._storage_base import app_models
from ._storage_ops import _StoreOps
from .binding_utils import resolve_execution_mode
from .models import (
    InstrumentHierarchy,
    ModePerformance,
    ScoreRecord,
    ScoringUserBinding,
    SignalRecord,
)
from .storage_base import ScoreStore
from .storage_memory import InMemoryScoreStore

logger = get_logger(__name__)

__all__ = ["AppScoreStore", "InMemoryScoreStore", "ScoreStore"]


class AppScoreStore(_StoreOps):
    """PostgreSQL-backed scoring store (lib_application schema).

    Composed via single inheritance: read methods here, write/outbox ops in
    :class:`_StoreOps`, and session/resolver/loader infrastructure in
    :class:`_StoreInfra`. Configure via DATABASE_URL.
    """

    def _resolve_instrument_row(self, s: Session, symbol: str) -> Any:
        """Resolve ``symbol`` through the canonical application resolver."""
        if app_models is None:
            return None
        return resolve_instrument(s, symbol)

    def list_signals(self, symbol: str, limit: int = 200) -> list[SignalRecord]:
        if app_models is None:
            return []
        with self._session() as s:
            instr = self._resolve_instrument_row(s, symbol)
            if not instr:
                return []
            rows = (
                s.query(app_models.CanonicalSignal)
                .filter(app_models.CanonicalSignal.instr_id == instr.instr_id)
                .order_by(app_models.CanonicalSignal.ts.desc())
                .limit(limit)
                .all()
            )
            sector_map = self._load_sector_map(s, {r.sector_id for r in rows if r.sector_id})
            return [self._signal_from_row(r, instr, sector_map.get(r.sector_id)) for r in rows]

    def list_latest_signal_per_strategy(self, symbol: str, limit: int = 64) -> list[SignalRecord]:
        """Latest signal per strategy for ``symbol`` via a single windowed query.

        ``ROW_NUMBER() OVER (PARTITION BY strategy_id ORDER BY ts DESC) = 1`` keeps
        exactly the most-recent row per strategy, so a chatty strategy can never
        crowd slower strategies off the page (the failure mode of a flat newest-N
        scan). Rows return newest-first, capped at ``limit``.
        """
        if app_models is None:
            return []
        with self._session() as s:
            instr = self._resolve_instrument_row(s, symbol)
            if not instr:
                return []
            cs = app_models.CanonicalSignal
            rank = (
                func.row_number()
                .over(
                    partition_by=cs.strategy_id,
                    order_by=cs.ts.desc(),
                )
                .label("_rank")
            )
            ranked = s.query(cs, rank).filter(cs.instr_id == instr.instr_id).subquery()
            latest = aliased(cs, ranked)
            rows = (
                s.query(latest)
                .filter(ranked.c._rank == 1)
                .order_by(ranked.c.ts.desc())
                .limit(limit)
                .all()
            )
            sector_map = self._load_sector_map(s, {r.sector_id for r in rows if r.sector_id})
            return [self._signal_from_row(r, instr, sector_map.get(r.sector_id)) for r in rows]

    def list_signals_by_sector(self, sector: str, limit: int = 500) -> list[SignalRecord]:
        if app_models is None:
            return []
        with self._session() as s:
            sector_row = s.query(app_models.Sector).filter_by(code=sector).first()
            if not sector_row:
                return []
            rows = (
                s.query(app_models.CanonicalSignal)
                .filter(app_models.CanonicalSignal.sector_id == sector_row.sector_id)
                .order_by(app_models.CanonicalSignal.ts.desc())
                .limit(limit)
                .all()
            )
            instr_map = self._load_instrument_map(s, {r.instr_id for r in rows if r.instr_id})
            return [
                self._signal_from_row(
                    r,
                    instr_map.get(r.instr_id),
                    sector_row.code,
                )
                for r in rows
                if instr_map.get(r.instr_id)
            ]

    def list_signals_by_industry(self, industry: str, limit: int = 500) -> list[SignalRecord]:
        return self.list_signals_by_sector(industry, limit=limit)

    def list_signals_by_index(self, index_name: str, limit: int = 500) -> list[SignalRecord]:
        return [
            s for s in self.list_all_signals(limit=limit) if s.metadata.get("index") == index_name
        ]

    def list_all_signals(self, limit: int = 1000) -> list[SignalRecord]:
        if app_models is None:
            return []
        with self._session() as s:
            rows = (
                s.query(app_models.CanonicalSignal)
                .order_by(app_models.CanonicalSignal.ts.desc())
                .limit(limit)
                .all()
            )
            instr_map = self._load_instrument_map(s, {r.instr_id for r in rows if r.instr_id})
            sector_map = self._load_sector_map(s, {r.sector_id for r in rows if r.sector_id})
            results: list[SignalRecord] = []
            for r in rows:
                instr = instr_map.get(r.instr_id)
                if not instr:
                    continue
                results.append(self._signal_from_row(r, instr, sector_map.get(r.sector_id)))
            return results

    def get_latest_score(  # noqa: PLR0911
        self, target: str, scope: str = "asset"
    ) -> ScoreRecord | None:
        if app_models is None:
            return None
        with self._session() as s:
            if scope == "asset":
                instr = self._resolve_instrument_row(s, target)
                if not instr:
                    return None
                row = (
                    s.query(app_models.AssetScore)
                    .filter(app_models.AssetScore.instr_id == instr.instr_id)
                    .order_by(app_models.AssetScore.ts.desc())
                    .first()
                )
                if not row:
                    return None
                weights = dict(row.weights_applied or {})
                metadata = weights.pop("_meta", {})
                comps = self._components_from_payload(row.components or [])
                return ScoreRecord(
                    target=instr.canonical,
                    scope="asset",
                    score=float(row.score_value),
                    computed_at=row.ts,
                    components=comps,
                    metadata=metadata,
                )
            if scope in {"sector", "industry"}:
                sector = s.query(app_models.Sector).filter_by(code=target).first()
                if not sector:
                    return None
                row = (
                    s.query(app_models.SectorScore)
                    .filter(app_models.SectorScore.sector_id == sector.sector_id)
                    .order_by(app_models.SectorScore.ts.desc())
                    .first()
                )
                if not row:
                    return None
                payload = dict(row.constituent_scores or {})
                metadata = payload.pop("_meta", {})
                return ScoreRecord(
                    target=sector.code,
                    scope="sector",
                    score=float(row.score_value),
                    computed_at=row.ts,
                    components=[],
                    metadata=metadata,
                )
            if scope == "market":
                row = (
                    s.query(app_models.MarketScore)
                    .filter(app_models.MarketScore.asset_class == target)
                    .order_by(app_models.MarketScore.ts.desc())
                    .first()
                )
                if not row:
                    return None
                payload = dict(row.sector_scores or {})
                metadata = payload.pop("_meta", {})
                return ScoreRecord(
                    target=row.asset_class,
                    scope="market",
                    score=float(row.score_value),
                    computed_at=row.ts,
                    components=[],
                    metadata=metadata,
                )
        return None

    def recent_asset_alpha_history(self, window: int = 100) -> dict[str, list[float]]:
        """Per-asset chronological ``alpha_raw`` history (oldest→newest) for
        warm-starting Layer-3 standardization on boot.

        Reads the ``_meta.alpha_raw`` stored alongside each persisted asset score
        (see ``upsert_score``) so a restarted scoring process resumes with the
        same rolling mean/std instead of the cold-start default. Bounded scan.
        """
        if app_models is None:
            return {}
        scan_limit = max(window, 1) * 200
        with self._session() as s:
            rows = (
                s.query(
                    app_models.AssetScore.weights_applied,
                    app_models.Instrument.canonical,
                )
                .join(
                    app_models.Instrument,
                    app_models.AssetScore.instr_id == app_models.Instrument.instr_id,
                )
                .order_by(app_models.AssetScore.ts.desc())
                .limit(scan_limit)
                .all()
            )
        history: dict[str, list[float]] = {}
        for weights_applied, canonical in reversed(rows):  # oldest→newest
            meta = weights_applied.get("_meta") if isinstance(weights_applied, dict) else None
            if not isinstance(meta, dict):
                continue
            alpha = meta.get("alpha_raw")
            if alpha is None:
                continue
            try:
                history.setdefault(canonical, []).append(float(alpha))
            except (TypeError, ValueError):
                continue
        return {asset: values[-window:] for asset, values in history.items() if values}

    def list_bindings(self) -> list[ScoringUserBinding]:
        if app_models is None:
            return []
        cached = self._cached_bindings()
        if cached is not None:
            return cached
        with self._session() as s:
            rows = (
                s.query(app_models.UserStrategyBinding)
                .filter(app_models.UserStrategyBinding.is_active.is_(True))
                .all()
            )
            if not rows:
                self._store_bindings_cache([])
                return []

            instrument_ids = self._collect_instrument_ids(rows)
            instrument_map = self._load_instrument_map(s, instrument_ids)
            sector_map = self._load_sector_map(s, self._collect_sector_ids(rows))
            sizing_map = self._load_sizing_profiles(s, rows)

            bindings: list[ScoringUserBinding] = []
            for row in rows:
                asset_filter = self._resolve_asset_filter(row, instrument_map)
                sector_filter = [
                    sector_map.get(sector_id)
                    for sector_id in (row.sectors_allowed or [])
                    if sector_map.get(sector_id)
                ]
                execution_mode = resolve_execution_mode(
                    preferred=row.preferred_mode,
                    allowed=row.execution_modes_allowed,
                    policy=row.mode_selection_policy,
                )
                sizing_profile = sizing_map.get(row.sizing_profile_id, {})
                risk_caps = {
                    "max_position_pct": (
                        float(row.max_position_pct) if row.max_position_pct is not None else None
                    ),
                    "max_total_exposure_pct": (
                        float(row.max_total_exposure_pct)
                        if row.max_total_exposure_pct is not None
                        else None
                    ),
                    "max_daily_loss_pct": (
                        float(row.max_daily_loss_pct)
                        if row.max_daily_loss_pct is not None
                        else None
                    ),
                    "max_open_positions": row.max_open_positions,
                }
                risk_caps = {k: v for k, v in risk_caps.items() if v is not None}

                bindings.append(
                    ScoringUserBinding(
                        user_id=row.user_id,
                        binding_id=row.binding_id,
                        strategy_id=row.strategy_id,
                        broker_account_id=row.broker_account_id,
                        asset_score_threshold=float(row.asset_score_threshold),
                        asset_filter=asset_filter,
                        sector_filter=[s for s in sector_filter if s],
                        execution_mode=execution_mode,
                        sizing_profile=sizing_profile,
                        risk_caps=risk_caps,
                        sector_score_threshold=(
                            float(row.sector_score_threshold)
                            if row.sector_score_threshold is not None
                            else None
                        ),
                        market_score_threshold=(
                            float(row.market_score_threshold)
                            if row.market_score_threshold is not None
                            else None
                        ),
                        allowed_brokers=list(row.allowed_brokers or []),
                        autopilot=bool(row.autopilot) if row.autopilot is not None else False,
                        entries_enabled=bool(row.entries_enabled),
                        exits_enabled=bool(row.exits_enabled),
                        execution_modes_allowed=list(row.execution_modes_allowed or ["spot"]),
                        mode_selection_policy=str(row.mode_selection_policy or "highest_sharpe"),
                        preferred_mode=row.preferred_mode,
                    )
                )

            self._store_bindings_cache(bindings)
            return bindings

    def list_inactive_strategy_binding_ids(
        self,
        user_id: str,
        strategy_id: str,
        broker_account_id: int | None,
    ) -> list[int]:
        """Inactive binding ids for one user/account/strategy route.

        Safety helper for suppressing wildcard fallback after explicit deactivation:
        ``list_bindings`` is active-only, so the score engine cannot otherwise
        tell a user who never had a strategy-specific binding apart from one
        whose binding was deactivated.
        """
        if app_models is None or broker_account_id is None:
            return []
        with self._session() as s:
            rows = (
                s.query(app_models.UserStrategyBinding.binding_id)
                .filter(
                    app_models.UserStrategyBinding.user_id == user_id,
                    app_models.UserStrategyBinding.strategy_id == strategy_id,
                    app_models.UserStrategyBinding.broker_account_id == broker_account_id,
                    app_models.UserStrategyBinding.is_active.is_(False),
                )
                .all()
            )
            return [int(row[0]) for row in rows]

    def list_mode_performance(
        self,
        asset: str | None = None,
        horizon: str | None = None,
        instrument_id: int | None = None,
        sector_id: int | None = None,
        asset_class: str | None = None,
        account_id: int | None = None,
        strategy_id: str | None = None,
    ) -> list[ModePerformance]:
        """
        List mode performance records by scope.

        Supports three scope types:
        - Instrument scope: pass asset (symbol) or instrument_id
        - Sector scope: pass sector_id
        - Asset class scope: pass asset_class

        Args:
            asset: Symbol to look up (resolves to instrument_id)
            horizon: Optional horizon filter (intraday, swing, long_term)
            instrument_id: Direct instrument ID lookup
            sector_id: Sector ID for sector-scoped records
            asset_class: Asset class for asset-class-scoped records

        Returns:
            List of ModePerformance DTOs matching the scope
        """
        if app_models is None:
            return []
        if account_id is None or strategy_id is None:
            return []

        with self._session() as s:
            # Build scope filter based on provided parameters
            filters = []

            # Resolve asset symbol to instrument_id if provided
            resolved_instr_id = instrument_id
            if asset and not instrument_id:
                instr = (
                    s.query(app_models.Instrument)
                    .filter(app_models.Instrument.canonical == asset)
                    .first()
                )
                if instr:
                    resolved_instr_id = instr.instr_id

            # Apply scope filters with isolation
            # Scope hierarchy: instrument > sector > asset_class (pure)
            # Note: asset_class is METADATA on instrument/sector rows, not a scope discriminator
            # Only rows with ONLY asset_class set (no instr_id, no sector_id) are
            # "asset_class scoped"
            if resolved_instr_id is not None:
                filters.append(app_models.ModePerformance.instr_id == resolved_instr_id)
                # Instrument scope: exclude sector-only scoped rows (sector_id set but no instr_id)
                # Do NOT filter on asset_class - it's metadata that can be present
            elif sector_id is not None:
                filters.append(app_models.ModePerformance.sector_id == sector_id)
                # Sector scope: exclude instrument-scoped rows
                filters.append(app_models.ModePerformance.instr_id.is_(None))
                # Do NOT filter on asset_class - it's metadata
            elif asset_class:
                # Pure asset_class scope: only rows with no instrument or sector
                filters.append(app_models.ModePerformance.asset_class == asset_class)
                filters.append(app_models.ModePerformance.instr_id.is_(None))
                filters.append(app_models.ModePerformance.sector_id.is_(None))

            # If no scope provided and asset didn't resolve, return empty
            if not filters:
                return []

            stmt = s.query(app_models.ModePerformance).filter(
                *filters,
                app_models.ModePerformance.account_id == account_id,
                app_models.ModePerformance.strategy_id == strategy_id,
            )

            if horizon:
                stmt = stmt.filter(
                    app_models.ModePerformance.horizon == self._normalize_horizon(horizon)
                )

            rows = stmt.all()

            # Determine asset label for DTO
            def get_asset_label(row: Any) -> str:
                if row.instr_id and asset:
                    return asset
                if row.instr_id:
                    # Try to resolve symbol from instrument
                    instr = s.query(app_models.Instrument).get(row.instr_id)
                    return instr.canonical if instr else str(row.instr_id)
                if row.sector_id:
                    return f"sector:{row.sector_id}"
                if row.asset_class:
                    return f"class:{row.asset_class}"
                return ""

            return [
                ModePerformance(
                    asset=get_asset_label(row),
                    execution_mode=row.execution_mode,
                    horizon=row.horizon,
                    sharpe=float(row.sharpe_ratio) if row.sharpe_ratio is not None else 0.0,
                    total_return=float(row.total_return) if row.total_return is not None else 0.0,
                    max_drawdown=float(row.max_drawdown) if row.max_drawdown is not None else 0.0,
                    updated_at=row.updated_at,
                    sortino=float(row.sortino_ratio) if row.sortino_ratio is not None else None,
                    win_rate=float(row.win_rate) if row.win_rate is not None else 0.0,
                    avg_win=float(row.avg_win) if row.avg_win is not None else None,
                    avg_loss=float(row.avg_loss) if row.avg_loss is not None else None,
                    sample_size=row.sample_size or 0,
                    period_start=row.period_start,
                    period_end=row.period_end,
                    instrument_id=row.instr_id,
                    sector_id=row.sector_id,
                    asset_class=row.asset_class,
                    account_id=row.account_id,
                    strategy_id=row.strategy_id,
                )
                for row in rows
            ]

    def get_instrument(self, symbol: str) -> InstrumentHierarchy | None:
        if app_models is None:
            return None
        with self._session() as s:
            instr = self._resolve_instrument_row(s, symbol)
            if not instr:
                return None
            sector_code = None
            mapping = (
                s.query(app_models.InstrumentSector, app_models.Sector)
                .join(
                    app_models.Sector,
                    app_models.InstrumentSector.sector_id == app_models.Sector.sector_id,
                )
                .filter(app_models.InstrumentSector.instr_id == instr.instr_id)
                .first()
            )
            if mapping:
                sector_code = mapping[1].code
            return InstrumentHierarchy(
                symbol=instr.canonical,
                settlement_currency=instr.settlement_currency,
                asset_class=instr.asset_class,
                sector=sector_code,
                industry=None,
                index=None,
            )
