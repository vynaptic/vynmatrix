"""Main entry point for the Feedback Loop Engine application."""

from __future__ import annotations

import os
import sys
import threading
from datetime import timedelta

import uvicorn
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from lib_application.db.session import (
    create_engine_for_env,
    dispose_engine,
    get_session_factory,
)
from lib_application.outbox import OutboxStore
from lib_common.app import start_background_health_server
from lib_common.env_utils import build_database_url, parse_int_env
from lib_common.logging import get_logger, setup_logging
from lib_infrastructure.persistence.sqlalchemy.repositories import (
    SQLAlchemySignalPerformanceRepository,
)

from .api import create_app
from .engine import FeedbackLoopEngine
from .mode_performance import ModePerformanceIntegrityError
from .models import EvaluationHorizon
from .price_provider import PriceProvider, SqlOHLCPriceProvider
from .suggestion_review import SuggestionReviewService

logger = get_logger(__name__)


def get_database_url() -> str:
    """Get database URL from environment or default."""
    return str(build_database_url())


def get_wrong_threshold() -> int:
    """Get consecutive wrong threshold from environment or default."""
    return parse_int_env("WRONG_THRESHOLD", default=2, min_value=1, logger=logger)


def get_evaluation_interval() -> int:
    """Get evaluation interval in seconds from environment or default."""
    return parse_int_env("EVALUATION_INTERVAL", default=3600, min_value=1, logger=logger)


def get_feedback_price_max_staleness_days() -> int:
    return parse_int_env(
        "FEEDBACK_PRICE_MAX_STALENESS_DAYS",
        default=5,
        min_value=0,
        logger=logger,
    )


def get_feedback_evaluation_horizons() -> list[EvaluationHorizon]:
    raw = os.environ.get("FEEDBACK_EVALUATION_HORIZONS", "")
    if not raw.strip():
        return list(EvaluationHorizon)
    horizons: list[EvaluationHorizon] = []
    seen: set[EvaluationHorizon] = set()
    for item in raw.split(","):
        value = item.strip().lower()
        if not value:
            msg = "FEEDBACK_EVALUATION_HORIZONS contains an empty value"
            raise ValueError(msg)
        try:
            horizon = EvaluationHorizon(value)
        except ValueError as exc:
            allowed = ", ".join(member.value for member in EvaluationHorizon)
            msg = f"Invalid FEEDBACK_EVALUATION_HORIZONS value {item!r}; expected one of: {allowed}"
            raise ValueError(msg) from exc
        if horizon in seen:
            msg = f"Duplicate FEEDBACK_EVALUATION_HORIZONS value: {horizon.value}"
            raise ValueError(msg)
        seen.add(horizon)
        horizons.append(horizon)
    return horizons


def create_engine_instance() -> tuple[
    FeedbackLoopEngine,
    SuggestionReviewService,
    sessionmaker,
    Engine,
]:
    """Create and configure the FeedbackLoopEngine instance.

    Returns the engine alongside the worker so the caller can call
    :func:`dispose_engine` on shutdown.
    """
    db_url = get_database_url()
    wrong_threshold = get_wrong_threshold()

    logger.info("Database connection established")
    logger.info("Wrong threshold: %d", wrong_threshold)

    engine = create_engine_for_env(db_url=db_url)

    # Price provider: use SQL price table if available, else null provider
    session_local = get_session_factory(engine=engine)

    price_provider: PriceProvider = SqlOHLCPriceProvider(
        session_local,
        max_staleness=timedelta(days=get_feedback_price_max_staleness_days()),
    )
    outbox_store = OutboxStore(session_local)
    signal_performance_repo = SQLAlchemySignalPerformanceRepository(engine=engine)
    suggestion_reviews = SuggestionReviewService(signal_performance_repo)

    return (
        FeedbackLoopEngine(
            engine=engine,
            wrong_threshold=wrong_threshold,
            default_horizon=EvaluationHorizon.D1,
            price_provider=price_provider,
            outbox_store=outbox_store,
            signal_performance_repo=signal_performance_repo,
        ),
        suggestion_reviews,
        session_local,
        engine,
    )


def run_api_server(host: str = "0.0.0.0", port: int = 8002) -> None:
    """Run the FastAPI server.

    Args:
        host: Host to bind to
        port: Port to listen on
    """
    feedback_engine, suggestion_reviews, _session_factory, db_engine = create_engine_instance()
    app = create_app(feedback_engine, suggestion_reviews)

    logger.info("Starting Feedback Loop Engine API on %s:%d", host, port)
    try:
        uvicorn.run(app, host=host, port=port)
    finally:
        dispose_engine(db_engine)


def _start_health_server(
    feedback_engine: FeedbackLoopEngine,
    suggestion_reviews: SuggestionReviewService,
) -> None:
    """Start a lightweight API server in a daemon thread for health checks.

    This allows container orchestrators to probe /health
    while the evaluation loop runs in the main thread.
    """
    app = create_app(feedback_engine, suggestion_reviews)
    start_background_health_server(
        app=app,
        service_name="feedback-loop-engine",
        default_port=8002,
    )


def _refresh_mode_performance(
    feedback_engine: FeedbackLoopEngine,
    *,
    totals: dict[str, int],
) -> int:
    """Refresh rankings and account for a failed projection in run health."""
    try:
        return feedback_engine.update_mode_performance()
    except (ModePerformanceIntegrityError, SQLAlchemyError):
        # Scheduler boundary: signal evaluations have already committed and must
        # not be discarded, but a broken P&L projection is a degraded run and
        # must make the one-shot command fail.
        logger.exception("Failed to update mode_performance")
        totals["errors"] = int(totals.get("errors", 0)) + 1
        return 0


def run_evaluation_loop(interval: int | None = None) -> None:
    """Run continuous evaluation loop.

    This runs in daemon mode, periodically evaluating signals.
    A lightweight API server runs in a background thread for /health probes.

    Args:
        interval: Evaluation interval in seconds (default from env)
    """
    feedback_engine, suggestion_reviews, _session_factory, db_engine = create_engine_instance()
    interval = interval or get_evaluation_interval()

    _start_health_server(feedback_engine, suggestion_reviews)
    logger.info("Starting evaluation loop with interval: %d seconds", interval)

    import signal as _signal  # noqa: PLC0415

    shutdown_event = threading.Event()

    def _handle_shutdown(signum: int, _frame: object) -> None:
        logger.info("Received signal %d, shutting down gracefully...", signum)
        shutdown_event.set()

    _signal.signal(_signal.SIGTERM, _handle_shutdown)
    _signal.signal(_signal.SIGINT, _handle_shutdown)

    while not shutdown_event.is_set():
        try:
            totals = {
                "signals_evaluated": 0,
                "correct_predictions": 0,
                "wrong_predictions": 0,
                "optimizations_triggered": 0,
                "skipped_no_price": 0,
                "errors": 0,
            }
            horizons = get_feedback_evaluation_horizons()
            for horizon in horizons:
                result = feedback_engine.run_evaluation_cycle(
                    horizon=horizon,
                    limit=100,
                )
                for key in totals:
                    totals[key] += int(result.get(key, 0))
                logger.info(
                    "Evaluation cycle complete: horizon=%s evaluated=%d correct=%d "
                    "wrong=%d optimizations=%d",
                    horizon.value,
                    result["signals_evaluated"],
                    result["correct_predictions"],
                    result["wrong_predictions"],
                    result["optimizations_triggered"],
                )
            # Once per iteration (after all horizons), refresh mode_performance
            # from executed outcomes so the scorer's best/auto ranking has data.
            rows = _refresh_mode_performance(feedback_engine, totals=totals)
            feedback_engine.record_run_heartbeat(horizons=horizons, results=totals)
            logger.info(
                "mode_performance refreshed: %d (account,strategy,instrument,mode,horizon) rows",
                rows,
            )
        except Exception:
            logger.exception("Error in evaluation loop")

        # Use event.wait() instead of time.sleep() so shutdown is responsive
        shutdown_event.wait(timeout=interval)

    dispose_engine(db_engine)
    logger.info("Daemon shutdown complete")


def main() -> None:
    """Main entry point."""
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))

    if len(sys.argv) > 1:
        command = sys.argv[1]

        if command == "api":
            # Run API server
            host = os.environ.get("HOST", "0.0.0.0")
            port = parse_int_env(
                "PORT",
                default=8002,
                min_value=1,
                max_value=65535,
                logger=logger,
            )
            run_api_server(host=host, port=port)

        elif command == "evaluate":
            # One scheduled pass across ALL configured horizons (same coverage as the
            # daemon loop), then refresh mode_performance. The Droplet systemd timer
            # fires this one-shot every 5 min; iterating the configured horizons — not
            # just D1 — keeps the sub-hour horizons (MIN15/H1, added for the scalper
            # feedback loop) evaluated in the scheduled topology, matching the
            # always-on daemon (N10).
            feedback_engine, _, _, db_engine = create_engine_instance()
            horizons = get_feedback_evaluation_horizons()
            totals = {
                "signals_evaluated": 0,
                "correct_predictions": 0,
                "wrong_predictions": 0,
                "optimizations_triggered": 0,
                "skipped_no_price": 0,
                "errors": 0,
            }
            try:
                for horizon in horizons:
                    result = feedback_engine.run_evaluation_cycle(horizon=horizon, limit=100)
                    for key in totals:
                        totals[key] += int(result.get(key, 0))
                mode_rows = _refresh_mode_performance(feedback_engine, totals=totals)
                feedback_engine.record_run_heartbeat(horizons=horizons, results=totals)
                print(
                    f"Evaluation complete across {len(horizons)} horizon(s) "
                    f"{[h.value for h in horizons]}: {totals}; mode_performance rows={mode_rows}"
                )
            finally:
                dispose_engine(db_engine)
            if totals["errors"]:
                logger.error(
                    "Feedback evaluation completed with %d processing error(s)",
                    totals["errors"],
                )
                raise SystemExit(1)

        elif command == "daemon":
            # Run continuous evaluation loop
            run_evaluation_loop()

        elif command == "list-suggestions":
            # List pending suggestions
            _, suggestion_reviews, _, db_engine = create_engine_instance()
            try:
                suggestions = suggestion_reviews.list_pending()
                if suggestions:
                    for suggestion in suggestions:
                        print(f"\n--- Suggestion {suggestion['feedback_id']} ---")
                        print(f"Strategy ID: {suggestion['strategy_id']}")
                        print(f"Trigger: {suggestion['trigger_reason']}")
                        print(f"Consecutive wrong: {suggestion['consecutive_wrong']}")
                else:
                    print("No pending suggestions")
            finally:
                dispose_engine(db_engine)

        else:
            print(f"Unknown command: {command}")
            print("Available commands: api, evaluate, daemon, list-suggestions")
            sys.exit(1)
    else:
        # Default: run API server
        run_api_server()


if __name__ == "__main__":
    main()
