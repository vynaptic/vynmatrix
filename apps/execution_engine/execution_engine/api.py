"""Authenticated execution-command API backed by the canonical Signal model."""

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, text
from sqlalchemy.exc import SQLAlchemyError

from lib_application.db.models import InstrumentPrice, PendingOrder
from lib_common.api_security import verify_admin_api_key
from lib_common.app import create_service_app
from lib_common.internal_events import ExecutionCommandEvent, RebalanceExecutionCommandEvent
from lib_common.logging import get_logger
from lib_common.metrics import gauge
from lib_common.observability import build_trace_context, ensure_run_id
from lib_strategy.signals.normalization import normalize_signal_action
from lib_strategy.signals.signal import Signal
from lib_strategy.signals.utils import compute_stable_signal_id, ensure_utc

from .account_execution_serializer import (
    AccountExecutionBusyError,
    AccountExecutionFenceLostError,
)
from .engine import ExecutionEngine
from .execution_result import ExecutionResult, is_retryable_failure
from .rebalance_execution_adapter import (
    ExecutionEngineRebalanceAdapter,
    RebalanceExecutionError,
)
from .rebalance_orchestrator import PortfolioRebalanceOrchestrator
from .rebalance_store import (
    RebalanceLeaseLostError,
    RebalancePlanContractError,
    RebalancePlanStore,
)

logger = get_logger(__name__)

_RECOVERABLE_ORDER_STATUSES = (
    "pending",
    "submission_unknown",
    "submitted",
    "working",
    "partially_filled",
)
_PAPER_LIFECYCLE_STATUSES = ("submitted", "working", "partially_filled")


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _record_execution_progress_metrics(
    *,
    status_counts: dict[str, int],
    oldest_age_seconds: float,
    max_processing_lag_seconds: float,
    reconciliation: dict[str, Any],
    paper_order_lifecycle: dict[str, Any],
) -> None:
    pending_orders = gauge(
        "execution_pending_orders_total",
        "Durable recoverable execution orders by status",
        ("status",),
    )
    oldest_age = gauge(
        "execution_pending_order_oldest_age_seconds",
        "Wall-clock age of the oldest recoverable execution order",
    )
    processing_lag = gauge(
        "execution_paper_order_processing_lag_seconds",
        "Maximum committed market-time lag across durable local-paper orders",
    )
    reconciliation_ready = gauge(
        "execution_reconciliation_initial_complete",
        "Whether initial durable account reconciliation completed",
    )
    reconciliation_partitions = gauge(
        "execution_reconciliation_discovered_partitions",
        "Durable account partitions discovered for reconciliation",
    )
    lifecycle_healthy = gauge(
        "execution_paper_order_lifecycle_healthy",
        "Whether the required durable paper-order lifecycle is healthy",
    )
    lifecycle_failed_orders = gauge(
        "execution_paper_order_lifecycle_failed_orders",
        "Durable local-paper orders with unresolved lifecycle errors",
    )
    if pending_orders is not None:
        for status in _RECOVERABLE_ORDER_STATUSES:
            pending_orders.labels(status=status).set(status_counts.get(status, 0))
    if oldest_age is not None:
        oldest_age.set(oldest_age_seconds)
    if processing_lag is not None:
        processing_lag.set(max_processing_lag_seconds)
    if reconciliation_ready is not None:
        reconciliation_ready.set(1 if reconciliation.get("initial_reconciliation_complete") else 0)
    if reconciliation_partitions is not None:
        reconciliation_partitions.set(int(reconciliation.get("discovered_partitions") or 0))
    if lifecycle_healthy is not None:
        lifecycle_healthy.set(1 if paper_order_lifecycle.get("healthy") else 0)
    if lifecycle_failed_orders is not None:
        lifecycle_failed_orders.set(int(paper_order_lifecycle.get("failed_orders") or 0))


def _paper_order_lifecycle_status(engine: ExecutionEngine) -> dict[str, Any]:
    status = getattr(engine, "paper_order_lifecycle_status", None)
    if not callable(status):
        return {"healthy": True, "failed_orders": 0}
    try:
        result = dict(status() or {})
        result["healthy"] = bool(result.get("healthy"))
        result["failed_orders"] = max(0, int(result.get("failed_orders") or 0))
    except (AttributeError, TypeError, ValueError):
        logger.exception("Paper-order lifecycle readiness probe failed")
        return {"healthy": False, "failed_orders": 0}
    return result


def _execution_progress_ready(engine: ExecutionEngine) -> dict[str, bool]:
    """Gate traffic on ambiguous submissions and committed-candle processing progress."""
    factory = getattr(engine, "_session_factory", None)
    if factory is None:
        return {
            "unknown_submissions_clear": False,
            "paper_order_progress": False,
        }
    runtime = getattr(engine, "_runtime_config", None)
    max_allowed_lag = float(
        getattr(
            getattr(runtime, "paper", None),
            "max_order_processing_lag_seconds",
            300,
        )
    )
    now = datetime.now(tz=UTC)
    status_counts = dict.fromkeys(_RECOVERABLE_ORDER_STATUSES, 0)
    oldest_age = 0.0
    max_processing_lag = 0.0
    missing_watermark = False
    try:
        with factory() as session:
            rows = (
                session.query(PendingOrder)
                .filter(PendingOrder.status.in_(_RECOVERABLE_ORDER_STATUSES))
                .all()
            )
            for row in rows:
                status_counts[str(row.status)] = status_counts.get(str(row.status), 0) + 1
                oldest_age = max(
                    oldest_age,
                    0.0,
                    (now - _as_utc(row.created_at)).total_seconds(),
                )
                if (
                    row.status not in _PAPER_LIFECYCLE_STATUSES
                    or str(row.broker).strip().lower() != "paper"
                    or str(row.broker_environment or "").strip().lower() != "paper"
                    or row.instr_id is None
                    or not row.market_data_source
                    or not row.market_data_timeframe
                    or (row.trigger_price is None and row.limit_price is None)
                ):
                    continue
                if row.last_market_data_ts is None or row.last_market_data_revision is None:
                    missing_watermark = True
                    continue
                latest = (
                    session.query(func.max(InstrumentPrice.ts))
                    .filter(
                        InstrumentPrice.instr_id == row.instr_id,
                        InstrumentPrice.source == row.market_data_source,
                        InstrumentPrice.timeframe == row.market_data_timeframe,
                    )
                    .scalar()
                )
                if latest is not None:
                    max_processing_lag = max(
                        max_processing_lag,
                        0.0,
                        (_as_utc(latest) - _as_utc(row.last_market_data_ts)).total_seconds(),
                    )
    except (SQLAlchemyError, OSError, ValueError, TypeError):
        logger.exception("Execution progress readiness probe failed")
        return {
            "unknown_submissions_clear": False,
            "paper_order_progress": False,
        }

    reconciliation = dict(getattr(engine, "reconciliation_status", dict)() or {})
    paper_order_lifecycle = _paper_order_lifecycle_status(engine)
    _record_execution_progress_metrics(
        status_counts=status_counts,
        oldest_age_seconds=oldest_age,
        max_processing_lag_seconds=max_processing_lag,
        reconciliation=reconciliation,
        paper_order_lifecycle=paper_order_lifecycle,
    )
    return {
        "unknown_submissions_clear": status_counts["submission_unknown"] == 0,
        "paper_order_progress": (not missing_watermark and max_processing_lag <= max_allowed_lag),
    }


def _execution_db_ready(engine: ExecutionEngine) -> bool:
    """Cheap ``SELECT 1`` readiness probe so an execution instance with a dead DB
    pool reports not-ready instead of accepting /execute-command traffic it cannot
    dedup or persist (orders, fills, positions, P&L, and NAV all require the DB).
    Missing configuration and any DB error both mean ``not ready``."""
    factory = getattr(engine, "_session_factory", None)
    if factory is None:
        return False
    try:
        with factory() as session:
            session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.warning("Execution DB readiness probe failed")
        return False
    else:
        return True


class TradingHaltRequest(BaseModel):
    enabled: bool
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RebalanceFailureResolutionRequest(BaseModel):
    """Operator disposition that clears one reviewed terminal-plan alert."""

    resolution_type: Literal["acknowledged", "reconciled", "remediated"]
    reason: str = Field(min_length=1, max_length=2_000)
    evidence: dict[str, object] = Field(default_factory=dict)


def _signal_from_payload(signal_payload: dict[str, Any], header_run_id: str | None) -> Signal:
    signal_payload["run_id"] = ensure_run_id(header_run_id or signal_payload.get("run_id"))
    signal_payload["timestamp"] = ensure_utc(signal_payload["timestamp"])
    if signal_payload.get("expires_at") is not None:
        signal_payload["expires_at"] = ensure_utc(signal_payload["expires_at"])
    signal_payload["action"] = normalize_signal_action(signal_payload["action"])
    if not signal_payload.get("signal_id"):
        signal_payload["signal_id"] = compute_stable_signal_id(
            strategy_id=signal_payload["strategy_id"],
            symbol=signal_payload["symbol"],
            action=signal_payload["action"],
            timestamp=signal_payload["timestamp"],
            run_id=signal_payload.get("run_id"),
            strategy_version=signal_payload.get("strategy_version"),
        )
    return Signal(**signal_payload)


def create_app(  # noqa: PLR0915
    engine: ExecutionEngine,
    lifespan: Any = None,
) -> FastAPI:
    session_factory = getattr(engine, "_session_factory", None)
    execution_services_factory = getattr(engine, "_execution_services", None)
    account_serializer = getattr(engine, "account_execution_serializer", None)
    rebalance_orchestrator = (
        PortfolioRebalanceOrchestrator(
            RebalancePlanStore(session_factory),
            ExecutionEngineRebalanceAdapter(
                execution_services_factory(),
                engine.handle_signal,
            ),
            worker_id="execution-engine",
            account_serializer=account_serializer,
        )
        if session_factory is not None and callable(execution_services_factory)
        else None
    )

    def _readiness() -> dict[str, bool]:
        paper_order_lifecycle = _paper_order_lifecycle_status(engine)
        rebalance_ready = False
        if rebalance_orchestrator is not None:
            try:
                rebalance_ready = bool(rebalance_orchestrator.readiness()["healthy"])
            except (SQLAlchemyError, OSError, ValueError):
                logger.exception("Rebalance readiness probe failed")
        checks = {
            "reconciliation": bool(getattr(engine, "is_reconciliation_healthy", lambda: True)()),
            "database": _execution_db_ready(engine),
            "paper_position_rehydration": bool(
                getattr(engine, "is_paper_rehydration_healthy", lambda: True)()
            ),
            "paper_order_lifecycle": bool(paper_order_lifecycle["healthy"]),
            **_execution_progress_ready(engine),
        }
        if rebalance_orchestrator is not None:
            checks["rebalance_plans"] = rebalance_ready
        return checks

    app = create_service_app(
        title="Execution Engine",
        version="0.1.0",
        lifespan=lifespan,
        readiness_check=_readiness,
    )

    # response_model=None: the handler returns either a dict (200) or a
    # JSONResponse (502 on transient failure, so the relay retries); the union
    # return type is not a pydantic response model.
    @app.post("/execute-command", response_model=None)
    async def execute_command(
        cmd: ExecutionCommandEvent,
        request: Request,
    ) -> dict[str, Any] | JSONResponse:
        try:
            signal_payload = cmd.signal.model_dump()
            sig = _signal_from_payload(signal_payload, request.headers.get("x-run-id"))
            score_context = cmd.score_context.model_dump(exclude_none=True)
            if cmd.execution_policy.execution_mode:
                score_context.setdefault("recommended_mode", cmd.execution_policy.execution_mode)
            profile = dict(cmd.profile)
            profile["_broker_route_snapshot"] = cmd.broker_route.model_dump(mode="json")
            user_strategy_config = dict(cmd.user_strategy_config)
            user_strategy_config["_execution_policy_snapshot"] = cmd.execution_policy.model_dump(
                mode="json"
            )
            # Carry the inbound command's event_id so the emitted ExecutionResultEvent
            # records it as its causation_id (command → result tracing link).
            user_strategy_config["_causation_event_id"] = cmd.event_id
            trace_ctx = build_trace_context(
                run_id=sig.run_id,
                signal_id=sig.signal_id,
                strategy_id=sig.strategy_id,
                symbol=sig.symbol,
                user_id=cmd.user_id,
                event_id=cmd.event_id,
            )
            logger.info("Execution command received", **trace_ctx)
            result = await engine.handle_signal(
                user_id=cmd.user_id,
                profile=profile,
                user_strategy_config=user_strategy_config,
                signal=sig,
                score_context=score_context,
            )
        except AccountExecutionBusyError:
            return JSONResponse(
                status_code=502,
                content={
                    "status": "retry",
                    "event_id": cmd.event_id,
                    "reason": "account_execution_busy",
                },
            )
        except HTTPException:
            raise
        except Exception:
            logger.exception("Execution command failed", user_id=cmd.user_id, event_id=cmd.event_id)
            raise HTTPException(status_code=500, detail="Execution command failed") from None
        else:
            payload = {
                "status": "ok",
                "event_id": cmd.event_id,
                "result": result.to_dict() if hasattr(result, "to_dict") else result,
            }
            if isinstance(result, ExecutionResult) and is_retryable_failure(result):
                # Transient failure (broker unavailable / connect / order rejected):
                # respond 5xx so the outbox relay retries instead of marking the
                # command delivered and silently dropping the order. _finalize_result
                # leaves the dedup row "pending" (reclaimable) for the retry.
                payload["status"] = "retry"
                return JSONResponse(status_code=502, content=payload)
            return payload

    @app.post("/execute-rebalance-command", response_model=None)
    async def execute_rebalance_command(
        cmd: RebalanceExecutionCommandEvent,
    ) -> dict[str, Any] | JSONResponse:
        """Advance one exact paper-forward account plan under a durable fence."""
        if rebalance_orchestrator is None:
            raise HTTPException(
                status_code=503,
                detail="Rebalance execution requires durable database state",
            )
        try:
            result = await rebalance_orchestrator.process(cmd)
        except RebalancePlanContractError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RebalanceExecutionError as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        except (RebalanceLeaseLostError, AccountExecutionFenceLostError):
            logger.exception(
                "Rebalance lease was lost",
                account_plan_id=cmd.account_plan_id,
                user_id=cmd.user_id,
            )
            return JSONResponse(
                status_code=502,
                content={
                    "status": "retry",
                    "event_id": cmd.event_id,
                    "account_plan_id": cmd.account_plan_id,
                    "reason": "rebalance_lease_lost",
                },
            )
        except (AccountExecutionBusyError, SQLAlchemyError, OSError, TimeoutError):
            logger.exception(
                "Rebalance command failed transiently",
                account_plan_id=cmd.account_plan_id,
                user_id=cmd.user_id,
            )
            return JSONResponse(
                status_code=502,
                content={
                    "status": "retry",
                    "event_id": cmd.event_id,
                    "account_plan_id": cmd.account_plan_id,
                },
            )
        payload = {
            "status": "retry" if result.retryable else "ok",
            "event_id": cmd.event_id,
            "result": result.to_dict(),
        }
        if result.retryable:
            return JSONResponse(status_code=502, content=payload)
        return payload

    @app.get("/admin/trading-halt")
    async def get_trading_halt(
        _admin_key: str | None = Depends(verify_admin_api_key),
    ) -> dict[str, Any]:
        del _admin_key
        return {"status": "ok", "halt": engine.trading_halt_state().to_dict()}

    @app.post("/admin/trading-halt")
    async def set_trading_halt(
        req: TradingHaltRequest,
        request: Request,
        _admin_key: str | None = Depends(verify_admin_api_key),
    ) -> dict[str, Any]:
        del _admin_key
        operator = request.headers.get("X-Admin-User") or request.headers.get("X-User-ID")
        state = engine.set_trading_halt(
            enabled=req.enabled,
            reason=req.reason,
            updated_by=operator,
            metadata=req.metadata,
        )
        return {"status": "ok", "halt": state.to_dict()}

    @app.get("/admin/rebalance-readiness")
    async def get_rebalance_readiness(
        _admin_key: str | None = Depends(verify_admin_api_key),
    ) -> dict[str, object]:
        del _admin_key
        if rebalance_orchestrator is None:
            raise HTTPException(status_code=503, detail="Rebalance store unavailable")
        return {"status": "ok", "rebalance": rebalance_orchestrator.readiness()}

    @app.get("/admin/rebalance-plans/{account_plan_id}")
    async def get_rebalance_plan(
        account_plan_id: str,
        _admin_key: str | None = Depends(verify_admin_api_key),
    ) -> dict[str, object]:
        del _admin_key
        if rebalance_orchestrator is None:
            raise HTTPException(status_code=503, detail="Rebalance store unavailable")
        try:
            audit = rebalance_orchestrator.audit(account_plan_id)
        except RebalancePlanContractError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"status": "ok", "rebalance": audit}

    @app.post("/admin/rebalance-plans/{account_plan_id}/resolve-failure")
    async def resolve_rebalance_failure(
        account_plan_id: str,
        req: RebalanceFailureResolutionRequest,
        request: Request,
        _admin_key: str | None = Depends(verify_admin_api_key),
    ) -> dict[str, object]:
        """Append an authenticated resolution; never rewrite terminal plan state."""
        del _admin_key
        if rebalance_orchestrator is None:
            raise HTTPException(status_code=503, detail="Rebalance store unavailable")
        operator = str(
            request.headers.get("X-Admin-User") or request.headers.get("X-User-ID") or ""
        ).strip()
        if not operator:
            raise HTTPException(
                status_code=400,
                detail="X-Admin-User or X-User-ID is required for failure resolution",
            )
        try:
            resolution = rebalance_orchestrator.resolve_failure(
                account_plan_id,
                resolution_type=req.resolution_type,
                reason=req.reason,
                evidence=req.evidence,
                resolved_by=operator,
            )
        except RebalancePlanContractError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (SQLAlchemyError, OSError):
            logger.exception(
                "Failed to resolve terminal rebalance plan",
                account_plan_id=account_plan_id,
                operator=operator,
            )
            raise HTTPException(
                status_code=503,
                detail="Rebalance failure resolution persistence unavailable",
            ) from None
        return {"status": "ok", "resolution": resolution}

    # ``/health`` is wired by ``create_service_app``.
    return app
