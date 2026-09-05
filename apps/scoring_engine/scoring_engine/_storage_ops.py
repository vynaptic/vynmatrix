from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from lib_application.outbox import OutboxRecord, OutboxRedriveResult
from lib_application.services.strategy_authority import require_active_strategy_version
from lib_common.logging import get_logger
from lib_strategy.signals.normalization import normalize_scoring_action
from lib_strategy.signals.utils import compute_execution_dedup_key

from ._storage_base import _StoreInfra, app_models
from .models import (
    ModePerformance,
    ScoreRecord,
    SignalRecord,
)
from .rebalance_store import _RebalanceStoreOps

logger = get_logger(__name__)


class _StoreOps(_RebalanceStoreOps, _StoreInfra):
    """Write-side + outbox operations for the scoring store."""

    def enqueue_event(
        self,
        *,
        topic: str,
        event_type: str,
        payload: dict[str, Any],
        schema_version: str = "v1",
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        event_key: str | None = None,
        ordering_key: str | None = None,
        headers: dict[str, Any] | None = None,
        max_attempts: int = 10,
        available_at: datetime | None = None,
    ) -> str:
        # Enqueue on the shared unit-of-work session so the event commits in the
        # same transaction as the signal/score writes (transactional outbox).
        # Standalone (no unit_of_work active) it opens its own session and
        # commits, preserving the previous behaviour for non-atomic callers.
        with self._session() as s:
            event_id = self._outbox.enqueue_on_session(
                s,
                topic=topic,
                event_type=event_type,
                payload=payload,
                schema_version=schema_version,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_key=event_key,
                ordering_key=ordering_key,
                headers=headers,
                max_attempts=max_attempts,
                available_at=available_at,
            )
            self._maybe_commit(s)
            return event_id

    def persist_decision_context(
        self,
        *,
        signal_id: str,
        symbol: str | None = None,
        strategy_id: str | None = None,
        run_id: str | None = None,
        correlation_id: str | None = None,
        action: str | None = None,
        score_value: float | None = None,
        feature_snapshot: dict[str, Any] | None = None,
        signal_metadata: dict[str, Any] | None = None,
        instr_id: int | None = None,
    ) -> int | None:
        """Write an immutable ``decision_contexts`` snapshot (provenance).

        Best-effort field extraction from the score payload + signal metadata;
        the row is added to the active unit-of-work session so it commits
        atomically with the signal/scores/execution command (or commits
        standalone outside a unit of work). Field extraction never raises —
        provenance must never break signal ingestion.
        """
        if app_models is None:
            return None
        snap: dict[str, Any] = feature_snapshot or {}
        meta: dict[str, Any] = signal_metadata or {}

        def _num(*keys: str) -> Decimal | None:
            for src in (snap, meta):
                for key in keys:
                    val = src.get(key)
                    if val is None:
                        continue
                    try:
                        return Decimal(str(val))
                    except (ArithmeticError, TypeError, ValueError):
                        continue
            return None

        def _txt(*keys: str) -> str | None:
            for src in (snap, meta):
                for key in keys:
                    val = src.get(key)
                    if val is not None:
                        return str(val)
            return None

        resolved_instr = instr_id
        if resolved_instr is None and symbol:
            resolved_instr = self.resolve_instrument_id(symbol)

        score_dec: Decimal | None = None
        if score_value is not None:
            try:
                score_dec = Decimal(str(score_value))
            except (ArithmeticError, TypeError, ValueError):
                score_dec = None

        abstained_val = snap.get("abstained")
        row = app_models.DecisionContext(
            signal_id=signal_id,
            instr_id=resolved_instr,
            strategy_id=strategy_id,
            symbol=symbol,
            run_id=run_id,
            correlation_id=correlation_id,
            pipeline_version=_txt("pipeline_version"),
            model_version=_txt("model_version", "meta_classifier"),
            alpha_core=_num("alpha_core"),
            alpha_raw=_num("alpha_raw"),
            score_standardized=_num("standardized_score", "score_standardized"),
            p_agg=_num("p_agg", "probability"),
            score_value=score_dec,
            regime=_txt("regime", "market_regime"),
            vix_level=_num("vix_level", "vix"),
            iv_rank=_num("iv_rank"),
            realized_vol=_num("realized_vol", "realized_vol_20d"),
            liquidity_score=_num("liquidity_score", "liquidity"),
            news_sentiment=_num("news_sentiment"),
            action=action,
            abstained=abstained_val if isinstance(abstained_val, bool) else None,
            abstain_reason=_txt("abstain_reason"),
            feature_snapshot=snap or None,
            factor_contributions=snap.get("components") or meta.get("components"),
            stale_inputs=meta.get("stale_inputs") or snap.get("stale_inputs"),
        )
        with self._session() as s:
            s.add(row)
            s.flush()
            context_id = int(row.context_id) if row.context_id is not None else None
            self._maybe_commit(s)
            return context_id

    def claim_outbox_batch(
        self,
        *,
        topics: Iterable[str],
        consumer: str,
        limit: int = 100,
        lease_seconds: int = 60,
    ) -> list[OutboxRecord]:
        return list(
            self._outbox.claim_batch(
                topics=topics,
                consumer=consumer,
                limit=limit,
                lease_seconds=lease_seconds,
            )
        )

    def outbox_backlog_counts(self, *, topics: Iterable[str] | None = None) -> dict[str, int]:
        return self._outbox.backlog_counts(topics=topics)

    def oldest_outbox_undelivered_created_at(
        self,
        *,
        topics: Iterable[str] | None = None,
    ) -> datetime | None:
        pending = self._outbox.list_pending(
            topics=topics,
            limit=1,
            include_dead_letter=True,
        )
        return pending[0].created_at if pending else None

    def mark_outbox_published(
        self,
        event_id: str,
        *,
        expected_claim_owner: str,
        expected_attempts: int,
        delivery_metadata: dict[str, Any] | None = None,
    ) -> bool:
        return self._outbox.mark_published(
            event_id,
            expected_claim_owner=expected_claim_owner,
            expected_attempts=expected_attempts,
            delivery_metadata=delivery_metadata,
        )

    def mark_outbox_failed(
        self,
        event_id: str,
        *,
        expected_claim_owner: str,
        expected_attempts: int,
        error_message: str,
        retry_delay_seconds: int = 30,
        failure_class: str = "transient",
        terminal: bool = False,
    ) -> bool:
        return self._outbox.mark_failed(
            event_id,
            expected_claim_owner=expected_claim_owner,
            expected_attempts=expected_attempts,
            error_message=error_message,
            retry_delay_seconds=retry_delay_seconds,
            failure_class=failure_class,
            terminal=terminal,
        )

    def list_outbox_dead_letters(
        self,
        *,
        topics: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[OutboxRecord]:
        return self._outbox.list_dead_letters(topics=topics, limit=limit)

    def redrive_outbox_dead_letter(
        self,
        event_id: str,
        *,
        actor: str,
        reason: str,
        expected_generation: int,
    ) -> OutboxRedriveResult:
        return self._outbox.redrive_dead_letter(
            event_id,
            actor=actor,
            reason=reason,
            expected_generation=expected_generation,
        )

    def add_signal(self, signal: SignalRecord) -> int | None:
        if app_models is None:
            return None
        with self._session() as s:
            self._require_strategy(s, signal.strategy_id, signal.asset_class)
            version_id = require_active_strategy_version(
                s, strategy_id=signal.strategy_id, strategy_version=signal.strategy_version
            )
            instr = self._resolve_instrument(
                s, signal.symbol, signal.asset_class, signal.instrument_id
            )
            sector = self._resolve_sector(s, signal.sector, signal.asset_class)
            if sector:
                self._require_instrument_sector(s, instr.instr_id, sector.sector_id)

            meta = dict(signal.metadata or {})
            for key, value in (
                ("strategy_version", signal.strategy_version),
                ("run_id", signal.run_id),
                ("source", signal.source),
                ("sector", signal.sector),
                ("industry", signal.industry),
                ("index", signal.index),
                ("stop_loss", signal.stop_loss),
                ("take_profit", signal.take_profit),
                ("size_hint", signal.size_hint),
                ("entry_price", signal.entry_price),
                ("signal_id", signal.signal_id),
                ("expires_at", signal.expires_at.isoformat() if signal.expires_at else None),
            ):
                if value is not None:
                    meta.setdefault(key, value)

            signal.action = normalize_scoring_action(signal.action)
            direction = signal.action if signal.action in {"long", "short"} else "flat"
            horizon_seconds = (
                int(signal.horizon_days * 86400) if signal.horizon_days is not None else None
            )

            ext_id = signal.external_signal_id
            if not ext_id:
                msg = f"Signal {signal.signal_id} has no canonical external_signal_id"
                raise ValueError(msg)
            signal_values: dict[str, Any] = {
                "strategy_id": signal.strategy_id,
                "strat_ver_id": version_id,
                "instr_id": instr.instr_id,
                "sector_id": sector.sector_id if sector else None,
                "action": signal.action,
                "confidence": signal.confidence,
                "raw_score": signal.score_value,
                "direction": direction,
                "entry_price": signal.entry_price,
                "expected_return": signal.expected_return,
                "predicted_risk": signal.predicted_risk,
                "horizon_seconds": horizon_seconds,
                "features": meta.get("features"),
                "signal_meta": meta,
                "source_runner": signal.strategy_type,
                "run_id": signal.run_id,
                "ts": signal.timestamp,
                "external_signal_id": ext_id,
                "expires_at": signal.expires_at,
            }
            if not self._is_sqlite:
                # Atomic idempotent insert (M10): a NOTIFY redelivery, worker restart,
                # or outbox retry can re-POST the same signal concurrently with the
                # original. INSERT ... ON CONFLICT DO UPDATE on the unique
                # external_signal_id makes re-delivery update the row instead of one
                # INSERT racing the other into an IntegrityError that fails ingest
                # (the prior SELECT-then-INSERT had that race).
                stmt = (
                    pg_insert(app_models.CanonicalSignal)
                    .values(**signal_values)
                    .on_conflict_do_update(
                        index_elements=[app_models.CanonicalSignal.external_signal_id],
                        set_={
                            "confidence": signal.confidence,
                            "raw_score": signal.score_value,
                            "signal_meta": meta,
                            "run_id": signal.run_id,
                            "expires_at": signal.expires_at,
                        },
                    )
                    .returning(app_models.CanonicalSignal.signal_id)
                )
                canonical_signal_id = int(s.execute(stmt).scalar_one())
                self._maybe_commit(s)
                return canonical_signal_id

            # SQLite tests are single-process, so SELECT-then-update is safe.
            existing = (
                s.query(app_models.CanonicalSignal)
                .filter(app_models.CanonicalSignal.external_signal_id == ext_id)
                .first()
            )
            if existing is not None:
                existing.confidence = signal.confidence
                existing.raw_score = signal.score_value
                existing.signal_meta = meta
                existing.run_id = signal.run_id
                existing.expires_at = signal.expires_at
                self._maybe_commit(s)
                return int(existing.signal_id)

            signal_id = None
            if self._is_sqlite:
                self._signal_seq += 1
                signal_id = self._signal_seq

            canonical_signal = app_models.CanonicalSignal(signal_id=signal_id, **signal_values)
            s.add(canonical_signal)
            s.flush()
            canonical_signal_id = int(canonical_signal.signal_id)
            self._maybe_commit(s)
            return canonical_signal_id

    def _upsert_asset_score(
        self,
        session: Session,
        score: ScoreRecord,
        components: list[dict[str, Any]],
    ) -> None:
        """Persist one asset score idempotently on canonical signal identity."""
        if app_models is None:
            return
        instr = self._resolve_instrument(session, score.target, None)
        weights: dict[str, Any] = {
            component.strategy_id: component.weight for component in score.components
        }
        if score.metadata:
            weights["_meta"] = score.metadata
        ext_id = score.external_signal_id
        if not ext_id:
            msg = f"Asset score for {score.target} has no external_signal_id"
            raise ValueError(msg)
        asset_values: dict[str, Any] = {
            "instr_id": instr.instr_id,
            "score_value": score.score,
            "components": components,
            "weights_applied": weights,
            "aggregation_method": "weighted_average",
            "confidence": None,
            "decay_factor": 1.0,
            "signals_count": len(score.components),
            "ts": score.computed_at,
            "external_signal_id": ext_id,
        }
        if not self._is_sqlite:
            # PostgreSQL arbitrates concurrent redelivery on the unique
            # external_signal_id instead of racing SELECT against INSERT.
            stmt = (
                pg_insert(app_models.AssetScore)
                .values(**asset_values)
                .on_conflict_do_update(
                    index_elements=[app_models.AssetScore.external_signal_id],
                    set_={
                        "score_value": score.score,
                        "components": components,
                        "weights_applied": weights,
                        "signals_count": len(score.components),
                        "ts": score.computed_at,
                    },
                )
            )
            session.execute(stmt)
            return

        # SQLite is used by single-process unit tests.
        existing = (
            session.query(app_models.AssetScore)
            .filter(app_models.AssetScore.external_signal_id == ext_id)
            .first()
        )
        if existing is not None:
            existing.score_value = score.score
            existing.components = components
            existing.weights_applied = weights
            existing.signals_count = len(score.components)
            existing.ts = score.computed_at
            return

        self._asset_score_seq += 1
        session.add(app_models.AssetScore(score_id=self._asset_score_seq, **asset_values))

    def upsert_score(self, score: ScoreRecord) -> None:
        if app_models is None:
            return
        with self._session() as s:
            components = [
                {
                    "strategy_id": c.strategy_id,
                    "weight": c.weight,
                    "raw_value": c.raw_value,
                    "weighted_value": c.weighted_value,
                    "confidence": c.confidence,
                    "timestamp": c.timestamp.isoformat(),
                }
                for c in score.components
            ]
            if score.scope == "asset":
                self._upsert_asset_score(s, score, components)
            elif score.scope == "sector":
                sector = self._resolve_sector(s, score.target, None)
                if sector:
                    sector_payload: dict[str, Any] = {
                        c.strategy_id: c.weighted_value for c in score.components
                    }
                    if score.metadata:
                        sector_payload["_meta"] = score.metadata
                    score_id = None
                    if self._is_sqlite:
                        self._sector_score_seq += 1
                        score_id = self._sector_score_seq
                    s.add(
                        app_models.SectorScore(
                            score_id=score_id,
                            sector_id=sector.sector_id,
                            score_value=score.score,
                            constituent_scores=sector_payload,
                            aggregation_method="weighted_average",
                            ts=score.computed_at,
                        )
                    )
            elif score.scope == "market":
                market_payload: dict[str, Any] = {"_score": score.score}
                if score.metadata:
                    market_payload["_meta"] = score.metadata
                score_id = None
                if self._is_sqlite:
                    self._market_score_seq += 1
                    score_id = self._market_score_seq
                s.add(
                    app_models.MarketScore(
                        score_id=score_id,
                        asset_class=score.target,
                        score_value=score.score,
                        sector_scores=market_payload,
                        ts=score.computed_at,
                    )
                )
            self._maybe_commit(s)

    def upsert_mode_performance(self, perf: ModePerformance) -> None:
        if app_models is None:
            return
        if perf.account_id is None or perf.strategy_id is None:
            msg = "ModePerformance requires account_id and strategy_id"
            raise ValueError(msg)
        with self._session() as s:
            horizon = self._normalize_horizon(perf.horizon)

            # Determine scope: instrument, sector, or asset_class
            # Priority: explicit IDs from DTO > resolve from asset string
            instr_id = perf.instrument_id
            sector_id = perf.sector_id
            asset_class = perf.asset_class

            # If no explicit scope, try to resolve from asset string
            if instr_id is None and sector_id is None and not asset_class:
                instr = self._resolve_instrument(s, perf.asset, None)
                instr_id = instr.instr_id

            payload = {
                "account_id": perf.account_id,
                "strategy_id": perf.strategy_id,
                "instr_id": instr_id,
                "sector_id": sector_id,
                "asset_class": asset_class,
                "execution_mode": perf.execution_mode,
                "horizon": horizon,
                "period_start": perf.period_start or perf.updated_at,
                "period_end": perf.period_end or perf.updated_at,
                "total_return": perf.total_return,
                "sharpe_ratio": perf.sharpe,
                "sortino_ratio": perf.sortino,
                "win_rate": perf.win_rate,
                "avg_win": perf.avg_win,
                "avg_loss": perf.avg_loss,
                "max_drawdown": perf.max_drawdown,
                "sample_size": perf.sample_size,
                "updated_at": perf.updated_at,
            }
            if instr_id is not None:
                conflict_columns = [
                    app_models.ModePerformance.account_id,
                    app_models.ModePerformance.strategy_id,
                    app_models.ModePerformance.instr_id,
                    app_models.ModePerformance.execution_mode,
                    app_models.ModePerformance.horizon,
                ]
                conflict_where = app_models.ModePerformance.instr_id.is_not(None)
            elif sector_id is not None:
                conflict_columns = [
                    app_models.ModePerformance.account_id,
                    app_models.ModePerformance.strategy_id,
                    app_models.ModePerformance.sector_id,
                    app_models.ModePerformance.execution_mode,
                    app_models.ModePerformance.horizon,
                ]
                conflict_where = app_models.ModePerformance.instr_id.is_(None) & (
                    app_models.ModePerformance.sector_id.is_not(None)
                )
            elif asset_class:
                conflict_columns = [
                    app_models.ModePerformance.account_id,
                    app_models.ModePerformance.strategy_id,
                    app_models.ModePerformance.asset_class,
                    app_models.ModePerformance.execution_mode,
                    app_models.ModePerformance.horizon,
                ]
                conflict_where = (
                    app_models.ModePerformance.instr_id.is_(None)
                    & app_models.ModePerformance.sector_id.is_(None)
                    & app_models.ModePerformance.asset_class.is_not(None)
                )
            else:
                msg = "ModePerformance requires instrument, sector, or asset-class scope"
                raise ValueError(msg)

            update_values = {
                key: value
                for key, value in payload.items()
                if key
                not in {
                    "account_id",
                    "strategy_id",
                    "instr_id",
                    "sector_id",
                    "asset_class",
                    "execution_mode",
                    "horizon",
                }
            }
            insert = sqlite_insert if self._is_sqlite else pg_insert
            statement = (
                insert(app_models.ModePerformance)
                .values(**payload)
                .on_conflict_do_update(
                    index_elements=conflict_columns,
                    index_where=conflict_where,
                    set_=update_values,
                )
            )
            s.execute(statement)
            self._maybe_commit(s)

    def persist_decision_log(
        self,
        decision: Any,
        signal_action: str,
        dedup_identity: str,
        run_id: str | None = None,
    ) -> None:
        """Persist an ExecutionDecision to the ExecutionDecisionLog table.

        Args:
            decision: ExecutionDecision domain object from lib_strategy.scoring.types
            signal_action: The signal's action value (e.g. "long", "short", "close")
                used for idempotency key generation.  Must match what execution dedup
                passes to ``get_execution_key()`` — i.e. ``signal.action.value.lower()``.
            dedup_identity: The stable ``external_signal_id`` used for the
                idempotency key so a redelivered signal with a
                fresh ``run_id``/``signal_id`` resolves to the SAME decision row
                (SC-1). Must match what the execution engine passes to
                ``get_execution_key()``.
        """
        if app_models is None:
            return
        if not hasattr(app_models, "ExecutionDecisionLog"):
            return

        user_id_for_db = str(decision.user_id).strip()
        identity = dedup_identity.strip()
        action_for_key = signal_action.strip()
        if not user_id_for_db or not identity or not action_for_key:
            msg = "Decision persistence requires user, external signal, and action identity"
            raise ValueError(msg)

        # Build the idempotency key via the SAME shared helper the execution
        # engine uses, keyed on external_signal_id so a redelivered signal
        # resolves to this same row (SC-1).
        # IMPORTANT: use signal_action (e.g. "close"), NOT decision.direction
        # (e.g. "flat"), because execution dedup keys on signal.action.value.lower().
        idem_key = compute_execution_dedup_key(
            identity,
            user_id_for_db,
            decision.broker_account_id,
            decision.symbol or "",
            action_for_key,
        )

        try:
            with self._session() as s:
                canonical_signal = (
                    s.query(app_models.CanonicalSignal)
                    .filter(app_models.CanonicalSignal.external_signal_id == identity)
                    .one_or_none()
                )
                if canonical_signal is None:
                    msg = (
                        "Decision persistence requires an exact canonical signal "
                        f"for external identity {identity!r}"
                    )
                    raise ValueError(msg)
                if int(canonical_signal.instr_id) != int(decision.instrument_id):
                    msg = "Decision instrument does not match its canonical signal"
                    raise ValueError(msg)
                if str(canonical_signal.strategy_id) != str(decision.strategy_id):
                    msg = "Decision strategy does not match its canonical signal"
                    raise ValueError(msg)
                canonical_signal_id = int(canonical_signal.signal_id)
                broker_account_id = int(decision.broker_account_id)

                # Upsert: if a row with the same idempotency_key exists, update it
                existing = (
                    s.query(app_models.ExecutionDecisionLog)
                    .filter(app_models.ExecutionDecisionLog.idempotency_key == idem_key)
                    .first()
                )

                # Extract policy snapshots from reasoning dict for dedicated columns.
                # These are frozen at decision time so execution is reproducible
                # even if user config later changes (compliance requirement).
                reasoning_dict = decision.reasoning or {}
                policy_snap = reasoning_dict.get("execution_policy_snapshot")
                route_snap = reasoning_dict.get("broker_route_snapshot")

                if existing:
                    if existing.canonical_signal_id not in (
                        None,
                        canonical_signal_id,
                    ) or existing.broker_account_id not in (None, broker_account_id):
                        msg = "Decision replay conflicts with persisted canonical lineage"
                        raise ValueError(msg)
                    existing.canonical_signal_id = canonical_signal_id
                    existing.broker_account_id = broker_account_id
                    existing.lineage_schema_version = "v1"
                    existing.should_execute = decision.should_execute
                    existing.execution_mode = decision.execution_mode
                    existing.direction = decision.direction
                    existing.reasoning = decision.reasoning
                    existing.binding_config_snapshot = policy_snap
                    existing.broker_route_snapshot = route_snap
                else:
                    log_entry = app_models.ExecutionDecisionLog(
                        user_id=user_id_for_db,
                        binding_id=decision.binding_id if decision.binding_id else None,
                        instr_id=decision.instrument_id if decision.instrument_id else None,
                        canonical_signal_id=canonical_signal_id,
                        broker_account_id=broker_account_id,
                        lineage_schema_version="v1",
                        asset_score=(
                            float(decision.asset_score)
                            if decision.asset_score is not None
                            else None
                        ),
                        sector_score=(
                            float(decision.sector_score)
                            if decision.sector_score is not None
                            else None
                        ),
                        market_score=(
                            float(decision.market_score)
                            if decision.market_score is not None
                            else None
                        ),
                        should_execute=decision.should_execute,
                        execution_mode=decision.execution_mode,
                        direction=decision.direction,
                        position_size_pct=(
                            float(decision.position_size_pct)
                            if decision.position_size_pct is not None
                            else None
                        ),
                        position_size_usd=(
                            float(decision.position_size_usd)
                            if decision.position_size_usd is not None
                            else None
                        ),
                        status="pending" if decision.should_execute else "skipped",
                        signal_id=decision.signal_id,
                        action=action_for_key,
                        run_id=run_id,
                        idempotency_key=idem_key,
                        reasoning=decision.reasoning,
                        binding_config_snapshot=policy_snap,
                        broker_route_snapshot=route_snap,
                    )
                    s.add(log_entry)
                self._maybe_commit(s)
        except SQLAlchemyError:
            # Re-raise (OB-2): inside the ingest unit_of_work this write shares the
            # transaction with the signal/scores/outbox, so swallowing it left a
            # poisoned transaction that rolled the whole signal back anyway — but
            # misattributed. Propagate so the rollback is atomic and correctly
            # reported instead of surfacing later as a stray PendingRollbackError.
            logger.exception("Failed to persist ExecutionDecisionLog")
            raise
