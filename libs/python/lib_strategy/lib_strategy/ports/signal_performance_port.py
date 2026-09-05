"""Signal-performance repository port (interface).

This port defines the contract for the persistence operations the
feedback loop needs to track per-strategy / per-instrument signal
quality:

* Increment / reset the "consecutive wrong" counter and read its
  current state.
* Persist the parameter-feedback queue — pending detection, suggestion
  listing, and atomic approve / reject transitions.
* Link feedback rows back to the consecutive-wrong tracker that
  triggered them.

Concrete adapters live in ``lib_infrastructure``; the SQLAlchemy
adapter (``SQLAlchemySignalPerformanceRepository``) reads/writes the
``StrategyConsecutiveWrongTracker`` and ``StrategyParameterFeedback``
tables.

Sprint F (Plan §6.9) — narrow port introduction so the
``FeedbackLoopEngine`` can be unit-tested with a stub instead of a live
PostgreSQL session, and so the engine no longer reaches into
``lib_application.db.models`` via inline lazy imports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ConsecutiveWrongTracker:
    """Persistence-layer record of consecutive-wrong predictions.

    Pure value object — no behaviour, no SQLAlchemy. The engine wraps
    this into its own ``ConsecutiveWrongStatus`` (which adds the
    canonical instrument symbol from a separate cache) before returning
    to callers.
    """

    strategy_id: str
    strat_ver_id: int | None
    instr_id: int
    horizon: str
    consecutive_wrong_count: int
    wrong_threshold: int
    threshold_reached: bool
    threshold_reached_at: datetime | None
    feedback_id: int | None
    last_signal_id: int | None
    last_signal_ts: datetime | None
    last_evaluation_ts: datetime | None


@dataclass(frozen=True)
class PendingSuggestion:
    """A single pending parameter-feedback suggestion.

    Pure value object returned by
    :meth:`ISignalPerformanceRepository.list_pending_suggestions`. The
    suggestion-review service renders the dict/JSON shape its API consumers
    expect; the port only commits to fields, not to a particular wire format.
    """

    feedback_id: int
    strategy_id: str
    strat_ver_id: int | None
    instr_id: int | None
    horizon: str
    trigger_reason: str
    consecutive_wrong: int
    current_params: dict[str, Any] = field(default_factory=dict)
    suggested_params: dict[str, Any] = field(default_factory=dict)
    explanation: str | None = None
    created_at: datetime | None = None


class ISignalPerformanceRepository(ABC):
    """Persistence port for per-strategy / per-instrument signal-quality state."""

    @abstractmethod
    def update_consecutive_wrong_tracker(
        self,
        *,
        strategy_id: str,
        strat_ver_id: int | None,
        instr_id: int,
        signal_id: int,
        signal_ts: datetime,
        is_correct: bool,
        wrong_threshold: int,
        horizon: str = "1d",
    ) -> ConsecutiveWrongTracker:
        """Update the tracker with the latest evaluation; return new state.

        Semantics:

        * If no row exists yet, insert a new tracker. New tracker starts
          at ``consecutive_wrong_count = 0`` if the first prediction was
          correct, ``= 1`` otherwise.
        * If a row exists, ``is_correct=True`` resets the counter to 0
          and clears ``threshold_reached``.
        * ``is_correct=False`` increments the counter; if the new count
          reaches ``wrong_threshold``, ``threshold_reached`` flips to
          ``True`` and ``threshold_reached_at`` records the timestamp.

        The returned ``ConsecutiveWrongTracker`` always reflects the
        post-update state.
        """

    @abstractmethod
    def has_pending_optimization(
        self,
        strategy_id: str,
        instr_id: int | None = None,
        horizon: str | None = None,
        *,
        max_age_days: int = 14,
    ) -> bool:
        """Return True if a fresh pending parameter-feedback row exists.

        Scoped to ``instr_id`` and ``horizon`` when given. Pending rows older
        than ``max_age_days`` are expired so an un-reviewed suggestion cannot
        block future optimization forever.
        """

    @abstractmethod
    def link_feedback_to_tracker(
        self,
        *,
        strategy_id: str,
        instr_id: int,
        horizon: str,
        feedback_id: int,
    ) -> bool:
        """Link the exact triggering tracker; return whether one row matched."""

    @abstractmethod
    def list_unlinked_reached_trackers(
        self,
        *,
        limit: int = 100,
    ) -> list[ConsecutiveWrongTracker]:
        """Return reached trackers whose suggestion link is missing."""

    @abstractmethod
    def list_pending_suggestions(
        self,
        strategy_id: str | None = None,
    ) -> list[PendingSuggestion]:
        """List pending suggestions; optionally filter by strategy."""

    @abstractmethod
    def get_pending_suggestion(self, feedback_id: int) -> PendingSuggestion | None:
        """Return one pending suggestion by identifier, or ``None``."""

    @abstractmethod
    def approve_suggestion(
        self,
        *,
        feedback_id: int,
        reviewer_user_id: str,
    ) -> bool:
        """Mark a suggestion as approved. Returns False if the row is missing."""

    @abstractmethod
    def reject_suggestion(
        self,
        *,
        feedback_id: int,
        reviewer_user_id: str,
        reason: str | None = None,
    ) -> bool:
        """Mark a suggestion as rejected. Returns False if the row is missing."""
