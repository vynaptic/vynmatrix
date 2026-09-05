"""FastAPI endpoints for the Feedback Loop Engine."""

import os
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from lib_application.db.models import ServiceHeartbeat
from lib_application.db.session import get_session_factory
from lib_common.app import create_service_app
from lib_common.env_utils import parse_int_env
from lib_common.logging import get_logger
from lib_strategy.signals.utils import ensure_utc

from .engine import FeedbackLoopEngine
from .models import EvaluationHorizon
from .suggestion_review import SuggestionReviewService

logger = get_logger(__name__)


def _engine_db_ready(engine: FeedbackLoopEngine) -> bool:
    """Cheap ``SELECT 1`` readiness probe so a feedback instance with a dead DB
    pool reports not-ready instead of accepting evaluation traffic it can't
    serve (G14). Non-fatal: any DB error means 'not ready'."""
    sa_engine = getattr(engine, "_engine", None)
    if sa_engine is None:
        return True
    try:
        with sa_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.warning("Feedback DB readiness probe failed")
        return False
    else:
        return True


def _engine_progress_ready(engine: FeedbackLoopEngine, *, max_age_seconds: int) -> bool:
    """Read the committed run heartbeat; a live HTTP thread is not evaluation progress."""
    sa_engine = getattr(engine, "_engine", None)
    if sa_engine is None:
        return False
    try:
        with get_session_factory(engine=sa_engine)() as session:
            row = session.get(ServiceHeartbeat, "feedback_loop_engine")
            if row is None or row.last_status != "ok":
                return False
            age = (datetime.now(tz=UTC) - ensure_utc(row.last_success_at)).total_seconds()
            return 0 <= age <= max_age_seconds
    except SQLAlchemyError:
        logger.warning("Feedback durable progress readiness probe failed")
        return False


class EvaluationCycleRequest(BaseModel):
    """Request to run an evaluation cycle."""

    horizon: str | None = Field(
        default="1d",
        description="Evaluation horizon (1h, 4h, 1d, 1w, 2w, 1m)",
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Maximum signals to process",
    )


class EvaluationCycleResponse(BaseModel):
    """Response from an evaluation cycle."""

    signals_evaluated: int
    correct_predictions: int
    wrong_predictions: int
    optimizations_triggered: int
    errors: int = 0


class SuggestionResponse(BaseModel):
    """Response for a single suggestion."""

    feedback_id: int
    strategy_id: str
    strat_ver_id: int | None
    instr_id: int | None
    horizon: str
    trigger_reason: str
    consecutive_wrong: int
    current_params: dict[str, Any]
    suggested_params: dict[str, Any]
    explanation: str | None
    created_at: str | None


class ApprovalRequest(BaseModel):
    """Request to approve/reject a suggestion."""

    reviewer_user_id: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="User ID of the reviewer",
    )
    reason: str | None = Field(
        None,
        max_length=2000,
        description="Optional reason for rejection",
    )


class ApprovalResponse(BaseModel):
    """Response for approval/rejection."""

    success: bool
    message: str


def create_app(
    engine: FeedbackLoopEngine,
    suggestion_reviews: SuggestionReviewService,
) -> FastAPI:
    """Create the FastAPI application.

    Args:
        engine: FeedbackLoopEngine instance
        suggestion_reviews: Focused parameter-suggestion review workflow

    Returns:
        Configured FastAPI app
    """
    require_progress = os.getenv("FEEDBACK_RUN_MODE") == "daemon"
    interval = parse_int_env("EVALUATION_INTERVAL", default=3600, min_value=1, strict=True)
    progress_max_age = parse_int_env(
        "FEEDBACK_HEARTBEAT_MAX_AGE_SECONDS",
        default=interval + max(60, interval // 2),
        min_value=1,
        strict=True,
    )

    def readiness() -> dict[str, bool]:
        checks = {"database": _engine_db_ready(engine)}
        if require_progress:
            checks["evaluation_progress"] = _engine_progress_ready(
                engine, max_age_seconds=progress_max_age
            )
        return checks

    app = create_service_app(
        title="Feedback Loop Engine",
        version="0.1.0",
        description="Strategy signal performance monitoring and parameter optimization",
        readiness_check=readiness,
    )

    @app.post("/evaluate", response_model=EvaluationCycleResponse)
    def run_evaluation(request: EvaluationCycleRequest) -> dict[str, Any]:
        """Run a signal evaluation cycle.

        Evaluates pending signals against actual price movement and
        generates optimization suggestions when consecutive wrong
        predictions reach the threshold.
        """
        try:
            horizon = EvaluationHorizon(request.horizon)
        except ValueError as e:
            msg = f"Invalid horizon: {request.horizon}"
            raise HTTPException(status_code=400, detail=msg) from e

        return engine.run_evaluation_cycle(
            horizon=horizon,
            limit=request.limit,
        )

    @app.get("/suggestions", response_model=list[SuggestionResponse])
    def list_suggestions(
        strategy_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List pending optimization suggestions."""
        try:
            return suggestion_reviews.list_pending(strategy_id=strategy_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/suggestions/{feedback_id}", response_model=SuggestionResponse)
    def get_suggestion(feedback_id: int) -> dict[str, Any]:
        """Get a specific suggestion by ID."""
        suggestion = suggestion_reviews.get_pending(feedback_id)
        if suggestion is None:
            raise HTTPException(status_code=404, detail="Suggestion not found")
        return suggestion

    @app.post("/suggestions/{feedback_id}/approve", response_model=ApprovalResponse)
    def approve_suggestion(feedback_id: int, request: ApprovalRequest) -> dict[str, Any]:
        """Approve a suggestion for a separate source-controlled promotion."""
        try:
            success = suggestion_reviews.approve(
                feedback_id=feedback_id,
                reviewer_user_id=request.reviewer_user_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if success:
            return {"success": True, "message": "Suggestion approved"}
        return {"success": False, "message": "Failed to approve suggestion"}

    @app.post("/suggestions/{feedback_id}/reject", response_model=ApprovalResponse)
    def reject_suggestion(feedback_id: int, request: ApprovalRequest) -> dict[str, Any]:
        """Reject an optimization suggestion."""
        try:
            success = suggestion_reviews.reject(
                feedback_id=feedback_id,
                reviewer_user_id=request.reviewer_user_id,
                reason=request.reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if success:
            return {"success": True, "message": "Suggestion rejected"}
        return {"success": False, "message": "Failed to reject suggestion"}

    return app
