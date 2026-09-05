"""FastAPI surface for the scoring engine.

Schema versioning:
    - ``X-Signal-Schema-Version`` header indicates the signal format
      version. ``v1`` is the current ``InsightSignalPayload`` format.

Validation:
    - Unknown signal actions are rejected.
    - All timestamps are normalised to UTC.
    - Signal IDs are deterministically generated for deduplication.

Module layout:
    - Pydantic request/response schemas live in :mod:`.schemas`.
    - Pure validation helpers live in :mod:`._signal_validation`.
    - This module owns ``create_app`` and the route handlers themselves.
"""

import asyncio
from collections.abc import Callable
from datetime import datetime
from time import perf_counter
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from lib_common.api_security import verify_admin_api_key
from lib_common.app import create_service_app
from lib_common.internal_events import (
    EXTERNAL_SIGNAL_ID_MAX_LENGTH,
    ModelRebalanceSubmissionEvent,
)
from lib_common.logging import get_logger
from lib_common.metrics import counter, histogram
from lib_common.observability import build_trace_context, ensure_run_id
from lib_strategy.signals.signal import Signal
from lib_strategy.signals.utils import (
    compute_external_signal_id,
    compute_stable_signal_id,
    ensure_utc,
)

from ._signal_validation import (
    expected_return_from_insight_payload,
    insight_direction_to_action,
    validate_signal_for_persistence,
)
from .dispatcher import DispatchProviderContexts, ExecutionDispatcher
from .engine import ScoreEngine, signal_from_record
from .models import ScoreRecord
from .pipeline_events import enqueue_scoring_pipeline_events
from .schemas import (
    SIGNAL_SCHEMA_VERSION,
    VALID_STRATEGY_TYPES,
    BindingResponse,
    InsightSignalPayload,
    InstrumentConfig,
    OutboxRedriveRequest,
    ScoreResponse,
)
from .services.rebalance_service import RebalanceScoringService
from .storage import ScoreStore

logger = get_logger(__name__)

# Observability for the signal/scoring half of the pipeline (G6): instrumented at
# the HTTP boundary (where /metrics is served) so an operator can see whether
# signals are still flowing + scoring latency, without bloating engine internals.
_SIGNALS_INGESTED = counter(
    "vm_signals_ingested_total",
    "Signals ingested + scored by the scoring engine",
    ("strategy_type", "action"),
)
_SCORING_INGEST_SECONDS = histogram(
    "vm_scoring_ingest_seconds",
    "Scoring engine ingest_signal latency (seconds)",
)


def _record_scored(signal: Signal, started_at: float) -> None:
    """Emit the ingest count + latency for one scored signal (G6)."""
    if _SIGNALS_INGESTED is not None:
        action = getattr(signal.action, "value", signal.action)
        _SIGNALS_INGESTED.labels(signal.strategy_type or "unknown", str(action)).inc()
    if _SCORING_INGEST_SECONDS is not None:
        _SCORING_INGEST_SECONDS.observe(perf_counter() - started_at)


def _outbox_record_payload(record: Any) -> dict[str, Any]:
    """Serialize the operator-facing DLQ record without changing its identity."""
    return {
        "event_id": record.event_id,
        "topic": record.topic,
        "event_type": record.event_type,
        "schema_version": record.schema_version,
        "aggregate_type": record.aggregate_type,
        "aggregate_id": record.aggregate_id,
        "event_key": record.event_key,
        "ordering_key": record.ordering_key,
        "payload": record.payload,
        "headers": record.headers,
        "status": record.status,
        "attempts": record.attempts,
        "max_attempts": record.max_attempts,
        "failure_class": record.failure_class,
        "last_error": record.last_error,
        "redrive_generation": record.redrive_generation,
        "redrive_audit": record.redrive_audit,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _store_db_ready(store: ScoreStore) -> bool:
    """Cheap ``SELECT 1`` readiness probe for the scoring store (G14).

    A store without a database session is not durable and therefore cannot be
    production-ready. Any DB error means 'not ready' so the orchestrator stops
    routing traffic until the pool recovers, rather than accepting signals it
    cannot persist.
    """
    session = store.get_session()
    if session is None:
        logger.error("Scoring readiness rejected a non-durable store")
        return False
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.warning("Scoring DB readiness probe failed")
        return False
    else:
        return True
    finally:
        session.close()


def create_app(  # noqa: PLR0915
    engine: ScoreEngine,
    dispatcher: ExecutionDispatcher | None = None,
    user_profiles: dict[str, Any] | None = None,
    user_strategy_configs: dict[int, Any] | None = None,
    lifespan: Any | None = None,
    relay_readiness: Callable[[], bool] | None = None,
) -> FastAPI:
    """
    Create the scoring engine FastAPI application.

    Args:
        engine: The ScoreEngine instance for signal processing
        dispatcher: Optional dispatcher for forwarding to execution engine
        user_profiles: Optional user profile configurations
        user_strategy_configs: Optional configurations keyed by binding id
        lifespan: Optional FastAPI lifespan handler
        relay_readiness: Optional inline-outbox-relay liveness probe. When the
            relay runs in this process, a dead relay stops all execution-command
            delivery while /health stays green, so it gates /ready (OB-1).
    """

    def _readiness() -> dict[str, bool]:
        # Gate /ready on DB connectivity: a scoring instance that has lost its DB
        # pool must stop receiving signal traffic (it is the ingest entrypoint),
        # otherwise dropped signals look like a strategy problem, not infra (G14).
        checks = {"database": _store_db_ready(engine.store)}
        if relay_readiness is not None:
            checks["outbox_relay"] = relay_readiness()
        return checks

    app = create_service_app(
        title="Scoring Engine",
        version="0.1.0",
        description=f"Signal scoring and aggregation. Schema: {SIGNAL_SCHEMA_VERSION}",
        lifespan=lifespan,
        readiness_check=_readiness,
    )
    rebalance_service = (
        RebalanceScoringService(engine=engine, dispatcher=dispatcher)
        if dispatcher is not None
        else None
    )

    def _get_market_score(asset: str) -> ScoreRecord | None:
        asset_class = None
        inst = engine.store.get_instrument(asset)
        if inst and inst.asset_class:
            asset_class = inst.asset_class
        if not asset_class:
            recent_signals = engine.store.list_signals(asset, limit=1)
            if recent_signals and recent_signals[0].asset_class:
                asset_class = recent_signals[0].asset_class
        if not asset_class:
            logger.error(
                "Could not resolve asset_class for %s; market context unavailable",
                asset,
            )
            return None
        return engine.store.get_latest_score(asset_class, scope="market")

    def _build_latest_signal(asset: str) -> Signal | None:
        recent_signals = engine.store.list_signals(asset, limit=1)
        if not recent_signals:
            return None
        # Single source of truth for record->Signal reconstruction (shared with
        # the cross-strategy ensemble gather). Returns None for a malformed row.
        return signal_from_record(recent_signals[0])

    def _emit_pipeline_events(
        *,
        signal: Signal,
        score: ScoreRecord,
        queued_users: list[str] | None = None,
    ) -> None:
        enqueue_scoring_pipeline_events(
            store=engine.store,
            signal=signal,
            score=score,
            queued_users=queued_users,
        )

    async def _resolve_dispatch_provider_contexts(
        signal: Signal,
    ) -> DispatchProviderContexts | None:
        """Pre-resolve dispatch provider lookups on the event loop.

        The ingest unit of work runs on a worker thread with no event loop, so
        potentially-async provider lookups are resolved HERE, before that
        thread starts, for every binding that could yield a decision for this
        signal (``evaluate_bindings`` draws from the same ``list_bindings()``
        snapshot and only filters it, so these candidates are a superset of
        the decisions). ``list_bindings`` is offloaded as its own
        self-contained unit of work: no ``unit_of_work`` is active on this
        request yet, so the context copied into the worker thread carries no
        session and the call opens and closes its own.
        """
        if dispatcher is None:
            return None
        bindings = await asyncio.to_thread(engine.store.list_bindings)
        targets = [
            (binding.user_id, binding.binding_id, binding.broker_account_id)
            for binding in bindings
            if binding.strategy_id is None or binding.strategy_id == signal.strategy_id
        ]
        return await dispatcher.resolve_provider_contexts(
            strategy_id=signal.strategy_id,
            targets=targets,
            user_profiles=user_profiles,
            user_strategy_configs=user_strategy_configs,
        )

    async def _resolve_rebalance_provider_contexts(
        event: ModelRebalanceSubmissionEvent,
    ) -> DispatchProviderContexts:
        if dispatcher is None:
            raise HTTPException(
                status_code=503,
                detail="Rebalance dispatcher is not configured",
            )
        bindings = await asyncio.to_thread(engine.store.list_bindings)
        targets = [
            (binding.user_id, binding.binding_id, binding.broker_account_id)
            for binding in bindings
            if binding.strategy_id == event.strategy_id
            and binding.binding_id is not None
            and binding.broker_account_id is not None
        ]
        return await dispatcher.resolve_provider_contexts(
            strategy_id=event.strategy_id,
            targets=targets,
            user_profiles=user_profiles,
            user_strategy_configs=user_strategy_configs,
        )

    def _dispatch_if_configured(
        signal: Signal,
        score: ScoreRecord,
        sector: str | None,
        provider_contexts: DispatchProviderContexts | None,
    ) -> list[str]:
        """Evaluate bindings and enqueue execution commands (sync, in-transaction).

        Runs inside the worker-thread unit of work so the sector/market score
        reads see this ingest's uncommitted writes and the decision log +
        execution.commands rows commit atomically with the signal. Provider
        lookups were pre-resolved on the event loop (``provider_contexts``)
        because a worker thread cannot await an async provider.
        """
        if dispatcher is None:
            return []

        resolved_sector = sector or signal.sector
        sector_score = (
            engine.store.get_latest_score(resolved_sector, scope="sector")
            if resolved_sector
            else None
        )
        market_score = _get_market_score(signal.symbol)
        decisions = engine.evaluate_bindings(
            score,
            sector_score=sector_score,
            market_score=market_score,
            signal_strategy_id=signal.strategy_id,
            signal_strategy_version=signal.strategy_version,
            signal_action=signal.action,
        )
        if not decisions:
            # Observability (M4): a signal whose strategy has >=1 active binding but
            # produced zero decisions is a silent gap (below threshold, a filter, or
            # a misconfig). dispatcher.persist_decision_log only writes when there
            # ARE decisions, so an empty result otherwise leaves NO trace — log it so
            # "a whole strategy never reached execution" is diagnosable. (list_bindings
            # is cached + active-only; strategy_id None = applies to any strategy.)
            relevant = [
                b
                for b in engine.store.list_bindings()
                if b.strategy_id is None or b.strategy_id == signal.strategy_id
            ]
            if relevant:
                logger.info(
                    "scored signal produced no execution decisions despite active bindings",
                    signal_id=signal.signal_id,
                    strategy_id=signal.strategy_id,
                    symbol=signal.symbol,
                    active_bindings=len(relevant),
                )
            return []

        score_ctx: dict[str, float] = {"asset_score": score.score}
        if sector_score is not None:
            score_ctx["sector_score"] = sector_score.score
        if market_score is not None:
            score_ctx["market_score"] = market_score.score

        dispatch_results = dispatcher.dispatch_resolved(
            signal=signal,
            decisions=decisions,
            provider_contexts=provider_contexts or DispatchProviderContexts(),
            score_context=score_ctx,
            original_signal=signal,
        )
        return [
            str(item["user_id"])
            for item in dispatch_results
            if item.get("status") == "queued" and item.get("user_id") is not None
        ]

    @app.post("/api/v1/signals", response_model=ScoreResponse)
    async def ingest_insight_signal(
        request: Request,
        payload: InsightSignalPayload,
        x_signal_schema_version: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """
        Ingest the strategy insight wire format produced by HttpSignalEmitter.

        Converts insight format to the internal Signal model:
        - direction "Up" -> LONG, "Down" -> SHORT, "Flat" -> CLOSE
        - Uses insight.confidence for signal confidence
        - Stores context in metadata
        """
        logger.info("Signal received", signal_payload=payload)

        # Schema version check (informational, not blocking)
        if x_signal_schema_version and x_signal_schema_version != SIGNAL_SCHEMA_VERSION:
            logger.warning(
                "Signal client using schema version %s, server expects %s",
                x_signal_schema_version,
                SIGNAL_SCHEMA_VERSION,
            )

        # A malformed provider timestamp must not be rewritten to receipt time:
        # doing so changes signal identity, freshness, and replay provenance.
        try:
            timestamp = datetime.fromisoformat(payload.ts)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid signal timestamp") from exc
        timestamp = ensure_utc(timestamp)

        # Convert to internal Signal — prefer X-Run-ID header, then body, then context
        context = payload.context or {}
        header_run_id = request.headers.get("x-run-id")
        run_id = ensure_run_id(header_run_id or payload.run_id or context.get("run_id"))
        action = insight_direction_to_action(payload.insight.direction)
        expires_at = context.get("expires_at")
        if isinstance(expires_at, str):
            try:
                expires_at = datetime.fromisoformat(expires_at)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="Invalid expires_at timestamp") from exc
        if isinstance(expires_at, datetime):
            expires_at = ensure_utc(expires_at)
        elif expires_at is not None:
            raise HTTPException(status_code=422, detail="Invalid expires_at timestamp")

        raw_external_signal_id = context.get("external_signal_id")
        if raw_external_signal_id is not None and (
            not isinstance(raw_external_signal_id, str)
            or not raw_external_signal_id.strip()
            or len(raw_external_signal_id.strip()) > EXTERNAL_SIGNAL_ID_MAX_LENGTH
        ):
            raise HTTPException(status_code=422, detail="Invalid external_signal_id")
        external_signal_id = (
            raw_external_signal_id.strip()
            if isinstance(raw_external_signal_id, str)
            else compute_external_signal_id(
                strategy_id=payload.strategy_id,
                strategy_version=context.get("strategy_version"),
                symbol=payload.symbol,
                action=action,
                bar_close_ts=timestamp,
                reason=(
                    context.get("entry_reason")
                    or context.get("exit_reason")
                    or context.get("reason")
                ),
            )
        )

        # Validate action for DB persistence
        try:
            db_action = validate_signal_for_persistence(action)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        signal_id = compute_stable_signal_id(
            strategy_id=payload.strategy_id,
            symbol=payload.symbol,
            action=action,
            timestamp=timestamp,
            run_id=run_id,
            strategy_version=context.get("strategy_version"),
        )
        # Validate strategy_type from context; reject unknown values with 422
        raw_strategy_type = context.get("strategy_type", "indicator")
        if raw_strategy_type not in VALID_STRATEGY_TYPES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid strategy_type '{raw_strategy_type}' in context. "
                    f"Must be one of {sorted(VALID_STRATEGY_TYPES)}"
                ),
            )

        sig = Signal(
            signal_id=signal_id,
            strategy_id=payload.strategy_id,
            strategy_type=raw_strategy_type,
            symbol=payload.symbol,
            action=action,
            confidence=payload.insight.confidence,
            timestamp=timestamp,
            horizon=payload.insight.horizon,
            expected_return=expected_return_from_insight_payload(payload, context),
            predicted_risk=context.get("predicted_risk") or context.get("vol"),
            horizon_days=context.get("horizon_days"),
            metadata={
                "magnitude": payload.insight.magnitude,
                "source": "signal_api",
                **context,
            },
            asset_class=context.get("asset_class"),
            sector=context.get("sector"),
            instrument_id=context.get("instrument_id"),
            strategy_version=context.get("strategy_version"),
            run_id=run_id,
            source=context.get("source") or "signal_api",
            entry_price=context.get("entry_price"),
            stop_loss=context.get("stop_loss"),
            take_profit=context.get("take_profit"),
            size_hint=context.get("size_hint"),
            external_signal_id=external_signal_id,
            expires_at=expires_at,
        )

        logger.info("Signal created", sig=sig)

        trace_ctx = build_trace_context(
            run_id=run_id,
            signal_id=sig.signal_id,
            strategy_id=sig.strategy_id,
            symbol=sig.symbol,
            schema_version=SIGNAL_SCHEMA_VERSION,
            db_action=db_action,
        )
        logger.info("Signal trace context", trace_ctx=trace_ctx)

        # Extract classification from context if available
        sector = context.get("sector")
        industry = context.get("industry")
        index = context.get("index")

        # One transaction for signal + scores + execution command(s) + pipeline
        # events: a crash mid-ingest cannot persist a scored signal without its
        # execution command (atomic transactional outbox; delivery is
        # at-least-once to an idempotent execution consumer).
        #
        # The WHOLE unit of work runs as one callable on a worker thread that
        # owns its session end-to-end: ``unit_of_work()`` sets and resets the
        # ``_ACTIVE_SESSION`` ContextVar inside the worker thread, so the
        # event-loop thread's context never carries a live Session and the
        # loop stays responsive during the sync SQLAlchemy I/O. Per-store-call
        # ``to_thread`` offload is forbidden here — it would share the one
        # unit-of-work Session across threads via the copied ContextVar.
        # Async provider lookups cannot run on the worker thread, so they are
        # resolved on the loop first.
        provider_contexts = await _resolve_dispatch_provider_contexts(sig)
        _t0 = perf_counter()

        def _run_ingest_unit() -> dict[str, Any]:
            with engine.store.unit_of_work():
                score = engine.ingest_signal(
                    sig,
                    sector=sector,
                    industry=industry,
                    index=index,
                )
                _record_scored(sig, _t0)
                # Dispatch the signal just ingested. A latest-for-symbol lookup can
                # select another strategy's concurrent signal and lose this decision.
                queued_users = _dispatch_if_configured(sig, score, sector, provider_contexts)
                _emit_pipeline_events(signal=sig, score=score, queued_users=queued_users)
            return score.to_dict()

        return await asyncio.to_thread(_run_ingest_unit)

    @app.post("/api/v1/model-rebalances")
    async def ingest_model_rebalance(
        event: ModelRebalanceSubmissionEvent,
    ) -> dict[str, object]:
        """Atomically score and fan out one typed paper-forward model batch."""

        if rebalance_service is None:
            raise HTTPException(
                status_code=503,
                detail="Rebalance dispatcher is not configured",
            )
        try:
            replay = await asyncio.to_thread(
                rebalance_service.preflight_exact_replay,
                event,
            )
            if replay is not None:
                return replay.to_dict()
            provider_contexts = await _resolve_rebalance_provider_contexts(event)
            result = await asyncio.to_thread(
                rebalance_service.ingest,
                event,
                provider_contexts=provider_contexts,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return result.to_dict()

    # FastAPI requires Query(...) for validation; this call is safe (no side effects) even
    # though ruff flags it for being evaluated at import time.
    @app.get("/scores", response_model=ScoreResponse | None)
    def get_score(
        asset: str = Query(..., description="Asset symbol, e.g., BTCUSD"),
        scope: str = Query("asset", pattern="^(asset|sector|industry|index|market)$"),
    ) -> dict[str, Any] | None:
        score = engine.store.get_latest_score(asset, scope=scope)
        return score.to_dict() if score else None

    @app.get(
        "/bindings",
        response_model=list[BindingResponse],
        dependencies=[Depends(verify_admin_api_key)],
    )
    def list_bindings() -> list[BindingResponse]:
        return [BindingResponse(**b.__dict__) for b in engine.store.list_bindings()]

    @app.get(
        "/instruments/{symbol}",
        response_model=InstrumentConfig | None,
        dependencies=[Depends(verify_admin_api_key)],
    )
    def get_instrument(symbol: str) -> InstrumentConfig | None:
        inst = engine.store.get_instrument(symbol)
        return InstrumentConfig(**inst.__dict__) if inst else None

    @app.get(
        "/admin/outbox/dead-letters",
        dependencies=[Depends(verify_admin_api_key)],
    )
    def list_outbox_dead_letters(
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        records = engine.store.list_outbox_dead_letters(
            topics=["execution.commands", "execution.rebalance.commands"],
            limit=limit,
        )
        return [_outbox_record_payload(record) for record in records]

    @app.post(
        "/admin/outbox/dead-letters/{event_id}/redrive",
        dependencies=[Depends(verify_admin_api_key)],
    )
    def redrive_outbox_dead_letter(
        event_id: str,
        payload: OutboxRedriveRequest,
        x_operator_id: str = Header(..., min_length=1, max_length=200),
    ) -> dict[str, Any]:
        try:
            result = engine.store.redrive_outbox_dead_letter(
                event_id,
                actor=x_operator_id.strip(),
                reason=payload.reason.strip(),
                expected_generation=payload.expected_generation,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not result.acquired:
            raise HTTPException(
                status_code=409,
                detail={
                    "event_id": result.event_id,
                    "generation": result.generation,
                    "outcome": result.outcome,
                },
            )
        return {
            "event_id": result.event_id,
            "generation": result.generation,
            "outcome": result.outcome,
        }

    @app.post("/evaluate", dependencies=[Depends(verify_admin_api_key)])
    def evaluate(
        asset: str,
        sector: str | None = None,
    ) -> dict[str, Any]:
        score = engine.store.get_latest_score(asset, scope="asset")
        if not score:
            raise HTTPException(status_code=404, detail="No score for asset")
        sector_score = engine.store.get_latest_score(sector, scope="sector") if sector else None
        # Fetch market score to properly enforce market_score_threshold in bindings
        market_score = _get_market_score(asset)
        latest_signal = _build_latest_signal(asset)
        decisions = engine.evaluate_bindings(
            score,
            sector_score=sector_score,
            market_score=market_score,
            signal_strategy_id=latest_signal.strategy_id if latest_signal is not None else None,
            signal_strategy_version=(
                latest_signal.strategy_version if latest_signal is not None else None
            ),
            signal_action=latest_signal.action if latest_signal is not None else None,
        )
        return {
            "score": score.to_dict(),
            "sector_score": sector_score.to_dict() if sector_score else None,
            "market_score": market_score.to_dict() if market_score else None,
            "decisions": [d.__dict__ for d in decisions],
        }

    # ``/health`` is wired by ``create_service_app``.
    return app
