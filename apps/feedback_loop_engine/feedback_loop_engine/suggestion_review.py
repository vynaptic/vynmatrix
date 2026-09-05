"""Review workflow for persisted parameter optimization suggestions."""

from __future__ import annotations

from typing import Any

from lib_common.logging import get_logger
from lib_strategy.ports.signal_performance_port import (
    ISignalPerformanceRepository,
    PendingSuggestion,
)

logger = get_logger(__name__)


class SuggestionReviewService:
    """Expose suggestion review without coupling it to signal evaluation."""

    def __init__(self, repository: ISignalPerformanceRepository) -> None:
        self._repository = repository

    @staticmethod
    def _render(suggestion: PendingSuggestion) -> dict[str, Any]:
        return {
            "feedback_id": suggestion.feedback_id,
            "strategy_id": suggestion.strategy_id,
            "strat_ver_id": suggestion.strat_ver_id,
            "instr_id": suggestion.instr_id,
            "horizon": suggestion.horizon,
            "trigger_reason": suggestion.trigger_reason,
            "consecutive_wrong": suggestion.consecutive_wrong,
            "current_params": suggestion.current_params,
            "suggested_params": suggestion.suggested_params,
            "explanation": suggestion.explanation,
            "created_at": (
                suggestion.created_at.isoformat() if suggestion.created_at is not None else None
            ),
        }

    @staticmethod
    def _reviewer_id(value: str) -> str:
        reviewer_id = value.strip()
        if not reviewer_id:
            message = "reviewer_user_id must not be blank"
            raise ValueError(message)
        return reviewer_id

    def list_pending(self, strategy_id: str | None = None) -> list[dict[str, Any]]:
        """List pending suggestions, optionally scoped to one strategy."""
        normalized_strategy_id = strategy_id.strip() if strategy_id is not None else None
        if strategy_id is not None and not normalized_strategy_id:
            message = "strategy_id must not be blank"
            raise ValueError(message)
        return [
            self._render(suggestion)
            for suggestion in self._repository.list_pending_suggestions(normalized_strategy_id)
        ]

    def get_pending(self, feedback_id: int) -> dict[str, Any] | None:
        """Return one pending suggestion by its positive identifier."""
        if feedback_id <= 0:
            return None
        suggestion = self._repository.get_pending_suggestion(feedback_id)
        return self._render(suggestion) if suggestion is not None else None

    def approve(self, *, feedback_id: int, reviewer_user_id: str) -> bool:
        """Record approval for a separate source-controlled promotion."""
        reviewer_id = self._reviewer_id(reviewer_user_id)
        suggestion = self.get_pending(feedback_id)
        if suggestion is None:
            return False
        if (
            not suggestion["current_params"]
            or not suggestion["suggested_params"]
            or suggestion["current_params"] == suggestion["suggested_params"]
        ):
            logger.error("Refusing approval of non-actionable suggestion %s", feedback_id)
            return False
        return self._repository.approve_suggestion(
            feedback_id=feedback_id,
            reviewer_user_id=reviewer_id,
        )

    def reject(
        self,
        *,
        feedback_id: int,
        reviewer_user_id: str,
        reason: str | None = None,
    ) -> bool:
        """Record rejection without modifying runtime strategy configuration."""
        reviewer_id = self._reviewer_id(reviewer_user_id)
        normalized_reason = reason.strip() if reason is not None else None
        return self._repository.reject_suggestion(
            feedback_id=feedback_id,
            reviewer_user_id=reviewer_id,
            reason=normalized_reason or None,
        )
