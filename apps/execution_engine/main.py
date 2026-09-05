"""
Entry point for the execution engine service.
Run with: uvicorn apps.execution_engine.main:app --reload
"""

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

import uvicorn
from fastapi import FastAPI

from execution_engine.api import create_app
from execution_engine.canonical_execution_store import CanonicalExecutionStore
from execution_engine.engine import ExecutionEngine
from execution_engine.execution_log_store import ExecutionLogStore
from execution_engine.execution_metrics_store import ExecutionMetricsStore
from execution_engine.execution_position_store import ExecutionPositionStore
from execution_engine.market_data import (
    SqlExecutionQuoteProvider,
    SqlOwnerScopedDelayedBBOProvider,
    SqlPriceQuoteProvider,
)
from execution_engine.order_builder import OrderBuilder
from execution_engine.paper_order_lifecycle import PaperOrderLifecycleWorker
from execution_engine.reconciliation import ReconciliationWorker
from execution_engine.risk_breach_store import RiskBreachStore
from lib_application.db.session import (
    create_engine_for_env,
    dispose_engine,
    get_session_factory,
)
from lib_common.alerting import AlertSink, build_sinks_from_env
from lib_common.config_validation import (
    ExecutionEngineConfig,
    load_execution_engine_config,
)
from lib_common.env_utils import parse_int_env
from lib_common.logging import get_logger, setup_logging
from lib_common.metrics import gauge
from lib_common.observability import init_tracing
from lib_common.shutdown import GracefulShutdown, get_shutdown_coordinator
from lib_common.task_supervision import supervise_background_task

logger = get_logger(__name__)


def _create_engine(
    config: ExecutionEngineConfig,
    *,
    alert_sinks: list[AlertSink] | None = None,
) -> tuple[ExecutionEngine, "Engine | None"]:
    """Build the ExecutionEngine and return the underlying SQLAlchemy engine.

    The SQLAlchemy engine reference is returned so the lifespan handler can
    call :func:`dispose_engine` on shutdown. Construction remains database
    optional for isolated unit tests, but the service lifespan rejects that
    configuration before accepting traffic.
    """
    order_builder = OrderBuilder()
    db_url = config.database.url if config.database is not None else None

    session_factory = None
    db_engine: Engine | None = None
    if db_url:
        db_engine = create_engine_for_env(db_url=db_url)
        session_factory = get_session_factory(engine=db_engine)
    log_store = ExecutionLogStore(session_factory=session_factory) if session_factory else None
    metrics_store = (
        ExecutionMetricsStore(session_factory=session_factory) if session_factory else None
    )
    position_store = (
        ExecutionPositionStore(session_factory=session_factory) if session_factory else None
    )
    risk_breach_store = (
        RiskBreachStore(session_factory=session_factory) if session_factory else None
    )
    canonical_execution_store = (
        CanonicalExecutionStore(session_factory=session_factory) if session_factory else None
    )
    # Shared feeds remain the first execution authority. If no shared quote is
    # available in paper mode, route to normalized immutable delayed-BBO evidence
    # for the exact binding owner. Provider clients and credentials remain in
    # ingestion, and the router never permits this personal-use fallback live.
    market_data_provider = None
    if session_factory is not None:
        market_data_provider = SqlExecutionQuoteProvider(
            public_provider=SqlPriceQuoteProvider(session_factory=session_factory),
            owner_delayed_provider=SqlOwnerScopedDelayedBBOProvider(
                session_factory=session_factory,
                max_staleness=timedelta(
                    seconds=config.freshness.max_delayed_paper_market_data_age_seconds
                ),
            ),
        )

    return (
        ExecutionEngine(
            order_builder=order_builder,
            market_data_provider=market_data_provider,
            execution_log_store=log_store,
            execution_metrics_store=metrics_store,
            execution_position_store=position_store,
            risk_breach_store=risk_breach_store,
            canonical_execution_store=canonical_execution_store,
            default_mode=config.mode.value,
            allow_live=config.allow_live,
            freshness=config.freshness,
            runtime_config=config.runtime,
            alert_sinks=alert_sinks,
            session_factory=session_factory,
        ),
        db_engine,
    )


# Global references for shutdown handling
_engine: ExecutionEngine | None = None
_shutdown: GracefulShutdown | None = None
_reconciliation_task: asyncio.Task[None] | None = None
_paper_order_task: asyncio.Task[None] | None = None
_PAPER_ORDER_LIFECYCLE_UP = gauge(
    "vm_execution_paper_order_lifecycle_up",
    "1 while the durable local-paper order lifecycle task is running, else 0",
)


def _verify_database_configured(engine: ExecutionEngine) -> None:
    """Refuse to start the deployed execution service without its ledger.

    Direct ``ExecutionEngine`` construction remains useful for focused unit
    tests, but every service instance must persist deduplication, orders,
    canonical fills, positions, P&L, and NAV in one authoritative database.
    """
    if getattr(engine, "_session_factory", None) is None:
        msg = (
            "Refusing to start execution_engine without DATABASE_URL: "
            "canonical order, fill, position, P&L, and NAV persistence is required"
        )
        raise RuntimeError(msg)


def _verify_alert_sink_readiness(
    config: ExecutionEngineConfig,
    *,
    has_sink: bool,
) -> None:
    """Fail-closed readiness gate (O1): a live execution engine must be able to
    page a human on circuit-breaker / reconciliation alerts.

    In the soak a ~16h post-reset breaker outage reached nobody because no alert
    sink was configured — the only acceptance-gate FAIL. The engine's alert path
    (AlertPublisher) needs BOTH ``EXECUTION_ALERTS_ENABLED=true`` AND at least one
    sink (``ALERT_WEBHOOK_URL``, ``ALERT_TELEGRAM_*``, or ``ALERT_EMAIL_*`` via
    ``build_sinks_from_env``). In paper mode a missing sink is a loud warning; in
    live mode it is fatal so the service refuses to start half-blind. Override
    the requirement with ``EXECUTION_REQUIRE_ALERT_SINK``.
    """
    alerts_enabled = config.runtime.alerts_enabled
    if alerts_enabled and has_sink:
        return

    is_live = config.mode.value == "live" or config.allow_live
    require = (
        config.runtime.require_alert_sink
        if config.runtime.require_alert_sink is not None
        else is_live
    )
    detail = (
        "operational alerts (circuit breaker, reconciliation) will not reach a "
        "human — set EXECUTION_ALERTS_ENABLED=true and one of "
        "ALERT_WEBHOOK_URL / ALERT_TELEGRAM_* / ALERT_EMAIL_*"
    )
    if require:
        msg = f"Refusing to start a live execution engine without an alert sink: {detail} (O1)"
        raise RuntimeError(msg)
    logger.warning("Alert sink not configured: %s (O1)", detail)


def _make_lifespan(
    engine: ExecutionEngine,
    db_engine: "Engine | None",
    config: ExecutionEngineConfig,
    *,
    has_alert_sink: bool,
) -> Callable[[FastAPI], Any]:
    """Create lifespan context manager that uses the provided engine instance."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """FastAPI lifespan for startup and shutdown handling."""
        global _engine, _paper_order_task, _reconciliation_task, _shutdown  # noqa: PLW0603

        # Startup
        logger.info("Execution engine starting up")

        # The deployed service has no database-free or signal-only mode.
        _verify_database_configured(engine)

        # Readiness gate: never run a live engine that cannot page a human (O1).
        _verify_alert_sink_readiness(
            config,
            has_sink=has_alert_sink,
        )

        # Use the engine passed to create_application, not a new one
        _engine = engine
        _shutdown = get_shutdown_coordinator(timeout_seconds=30)
        # NOTE: Do NOT call register_signals() here - uvicorn handles SIGTERM/SIGINT
        # and registering our own handlers would override uvicorn's graceful shutdown

        position_store = getattr(engine, "_execution_position_store", None)
        account_serializer = engine.account_execution_serializer
        reconciliation_worker = None
        paper_order_worker = None
        if position_store is not None:
            reconciliation_worker = ReconciliationWorker(
                engine=engine,
                position_store=position_store,
                risk_breach_store=getattr(engine, "_risk_breach_store", None),
                interval_sec=config.runtime.reconciliation_interval_seconds,
                # FIFO-ledger-vs-broker position drift check (PL-2). Independent of
                # the broker-mirror position check; warn-only, surfaced via alerts +
                # risk_breaches.
                pnl_service=getattr(engine, "_pnl_service", None),
                position_drift_tolerance=(config.runtime.reconciliation_position_drift_tolerance),
                account_serializer=account_serializer,
            )
            await reconciliation_worker.reconcile_once()
            _reconciliation_task = asyncio.create_task(
                reconciliation_worker.run(),
                name="execution-reconciliation",
            )
        if config.runtime.paper.use_local_broker:
            paper_order_worker = PaperOrderLifecycleWorker(
                engine=engine,
                session_factory=engine._session_factory,
                account_serializer=account_serializer,
            )
            await paper_order_worker.process_once()
            _paper_order_task = asyncio.create_task(
                paper_order_worker.run(),
                name="execution-paper-order-lifecycle",
            )
            supervise_background_task(
                _paper_order_task,
                name="Durable paper-order lifecycle task",
                up_gauge=_PAPER_ORDER_LIFECYCLE_UP,
                is_expected_stop=lambda: paper_order_worker.is_stopping,
                cancelled_is_error=True,
                on_failure=lambda error: engine.mark_paper_order_lifecycle_health(
                    healthy=False,
                    error=error,
                ),
            )

        # Register cleanup handler for graceful shutdown
        async def cleanup() -> None:
            logger.info("Cleaning up execution engine resources")
            # Uvicorn drains in-flight requests before entering lifespan teardown.
            # Signal background workers before the first suspension point so a
            # short container grace period cannot let either worker begin another
            # database pass while shutdown is already in progress.
            if reconciliation_worker is not None:
                await reconciliation_worker.stop()
            if paper_order_worker is not None:
                await paper_order_worker.stop()
            if _paper_order_task is not None:
                try:
                    await asyncio.wait_for(_paper_order_task, timeout=5.0)
                except TimeoutError:
                    _paper_order_task.cancel()
                    await asyncio.gather(_paper_order_task, return_exceptions=True)
            if _reconciliation_task is not None:
                try:
                    await asyncio.wait_for(_reconciliation_task, timeout=5.0)
                except TimeoutError:
                    _reconciliation_task.cancel()
                    await asyncio.gather(_reconciliation_task, return_exceptions=True)
            try:
                await engine.close()
            except Exception:
                logger.exception("Failed to close execution engine resources")
            try:
                dispose_engine(db_engine)
            except Exception:
                logger.exception("Failed to dispose SQLAlchemy engine")

        _shutdown.on_shutdown(cleanup)

        # Store engine in app state for access by handlers
        app.state.engine = _engine
        app.state.shutdown = _shutdown
        app.state.reconciliation_worker = reconciliation_worker
        app.state.paper_order_worker = paper_order_worker

        logger.info(
            "Execution engine ready",
            extra={
                "mode": config.mode.value,
                "allow_live": config.allow_live,
                "reconciliation_status": engine.reconciliation_status(),
                "paper_order_lifecycle_status": engine.paper_order_lifecycle_status(),
            },
        )

        yield

        # Shutdown - uvicorn triggers this on SIGTERM/SIGINT
        logger.info("Execution engine shutting down")

        if _shutdown:
            await _shutdown.execute()

        logger.info("Execution engine shutdown complete")

    return lifespan


def create_application() -> FastAPI:
    """Create the FastAPI application with lifespan management."""
    config = load_execution_engine_config(require_database=False)
    alert_sinks = build_sinks_from_env()
    # Create single engine instance used by both API and lifespan
    engine, db_engine = _create_engine(config, alert_sinks=alert_sinks)
    lifespan = _make_lifespan(
        engine,
        db_engine,
        config,
        has_alert_sink=bool(alert_sinks),
    )
    return create_app(engine, lifespan=lifespan)


# Create app for uvicorn
app: FastAPI = create_application()


def main() -> None:
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    # No-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set + otel deps installed (G7).
    init_tracing("execution-engine")
    host = os.getenv("HOST", "0.0.0.0")
    port = parse_int_env("PORT", default=8000, min_value=1, max_value=65535, logger=logger)
    uvicorn.run(app, host=host, port=port, log_config=None)


if __name__ == "__main__":
    main()
