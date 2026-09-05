"""Unit tests for ``FeedbackLoopEngine`` consecutive-wrong tracker via port stub.

Sprint F (Plan §6.9): introduces ``ISignalPerformanceRepository`` so the
engine can be tested without a live PostgreSQL session. These tests use
an in-memory stub of the port to exercise the engine's wrap/unwrap
logic and prove that the consecutive-wrong / has-pending-optimization
paths flow through the port rather than reaching into
``lib_application.db.models`` directly. Suggestion queue tests exercise the
focused review service over the same port.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from feedback_loop_engine.engine import FeedbackLoopEngine
from feedback_loop_engine.models import EvaluationHorizon
from feedback_loop_engine.suggestion_review import SuggestionReviewService
from lib_strategy.ports.signal_performance_port import (
    ConsecutiveWrongTracker,
    ISignalPerformanceRepository,
    PendingSuggestion,
)


class _StubSignalPerformanceRepo(ISignalPerformanceRepository):
    """In-memory stub. Records every call and returns canned trackers."""

    def __init__(self) -> None:
        self.update_calls: list[dict] = []
        self.pending_calls: list[str] = []
        self.link_calls: list[dict] = []
        self.list_calls: list[str | None] = []
        self.get_calls: list[int] = []
        self.approve_calls: list[dict] = []
        self.reject_calls: list[dict] = []
        self._pending_strategies: set[str] = set()
        self._tracker_state: dict[tuple[str, int, str], dict] = {}
        self._pending_suggestions: list[PendingSuggestion] = []
        self._existing_feedback: set[int] = set()

    def set_pending(self, strategy_id: str) -> None:
        self._pending_strategies.add(strategy_id)

    def add_pending_suggestion(self, suggestion: PendingSuggestion) -> None:
        self._pending_suggestions.append(suggestion)
        self._existing_feedback.add(suggestion.feedback_id)

    def update_consecutive_wrong_tracker(  # type: ignore[override]
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
        self.update_calls.append(
            {
                "strategy_id": strategy_id,
                "strat_ver_id": strat_ver_id,
                "instr_id": instr_id,
                "signal_id": signal_id,
                "signal_ts": signal_ts,
                "is_correct": is_correct,
                "wrong_threshold": wrong_threshold,
                "horizon": horizon,
            }
        )

        key = (strategy_id, instr_id, horizon)
        state = self._tracker_state.get(
            key,
            {
                "consecutive_wrong_count": 0,
                "threshold_reached": False,
                "threshold_reached_at": None,
            },
        )
        if is_correct:
            state["consecutive_wrong_count"] = 0
            state["threshold_reached"] = False
            state["threshold_reached_at"] = None
        else:
            state["consecutive_wrong_count"] = int(state["consecutive_wrong_count"]) + 1
            if state["consecutive_wrong_count"] >= wrong_threshold:
                state["threshold_reached"] = True
                state["threshold_reached_at"] = datetime.now(tz=UTC)
        self._tracker_state[key] = state

        return ConsecutiveWrongTracker(
            strategy_id=strategy_id,
            strat_ver_id=strat_ver_id,
            instr_id=instr_id,
            horizon=horizon,
            consecutive_wrong_count=int(state["consecutive_wrong_count"]),
            wrong_threshold=wrong_threshold,
            threshold_reached=bool(state["threshold_reached"]),
            threshold_reached_at=state["threshold_reached_at"],
            feedback_id=None,
            last_signal_id=signal_id,
            last_signal_ts=signal_ts,
            last_evaluation_ts=datetime.now(tz=UTC),
        )

    def has_pending_optimization(  # type: ignore[override]
        self,
        strategy_id: str,
        instr_id: int | None = None,
        horizon: str | None = None,
        *,
        max_age_days: int = 14,
    ) -> bool:
        self.pending_calls.append(strategy_id)
        return strategy_id in self._pending_strategies

    def link_feedback_to_tracker(  # type: ignore[override]
        self, *, strategy_id: str, instr_id: int, horizon: str, feedback_id: int
    ) -> bool:
        self.link_calls.append(
            {
                "strategy_id": strategy_id,
                "instr_id": instr_id,
                "horizon": horizon,
                "feedback_id": feedback_id,
            }
        )
        return True

    def list_unlinked_reached_trackers(
        self,
        *,
        limit: int = 100,
    ) -> list[ConsecutiveWrongTracker]:
        del limit
        return []

    def list_pending_suggestions(  # type: ignore[override]
        self, strategy_id: str | None = None
    ) -> list[PendingSuggestion]:
        self.list_calls.append(strategy_id)
        if strategy_id is None:
            return list(self._pending_suggestions)
        return [s for s in self._pending_suggestions if s.strategy_id == strategy_id]

    def get_pending_suggestion(self, feedback_id: int) -> PendingSuggestion | None:
        self.get_calls.append(feedback_id)
        return next(
            (
                suggestion
                for suggestion in self._pending_suggestions
                if suggestion.feedback_id == feedback_id
            ),
            None,
        )

    def approve_suggestion(  # type: ignore[override]
        self, *, feedback_id: int, reviewer_user_id: str
    ) -> bool:
        self.approve_calls.append(
            {"feedback_id": feedback_id, "reviewer_user_id": reviewer_user_id}
        )
        return feedback_id in self._existing_feedback

    def reject_suggestion(  # type: ignore[override]
        self, *, feedback_id: int, reviewer_user_id: str, reason: str | None = None
    ) -> bool:
        self.reject_calls.append(
            {
                "feedback_id": feedback_id,
                "reviewer_user_id": reviewer_user_id,
                "reason": reason,
            }
        )
        return feedback_id in self._existing_feedback


def _engine(repo: ISignalPerformanceRepository) -> FeedbackLoopEngine:
    """Build a FeedbackLoopEngine with the stubbed port (no DB connection).

    ``__new__`` skips the real constructor — we just set the bare
    minimum the consecutive-wrong path reads.
    """
    eng = FeedbackLoopEngine.__new__(FeedbackLoopEngine)
    eng._engine = None  # type: ignore[assignment]
    eng.wrong_threshold = 3
    eng.default_horizon = EvaluationHorizon.D1
    eng._symbol_cache = {42: "BTC-USD"}
    eng._signal_performance_repo = repo  # type: ignore[attr-defined]
    return eng


def test_consecutive_wrong_tracker_wraps_port_record_into_status() -> None:
    """The engine adds ``symbol`` from its cache; everything else is the port's.

    The engine's method is named ``_update_consecutive_tracker`` (without
    ``wrong``); the test pins the wrap-into-``ConsecutiveWrongStatus`` logic
    by exercising it through that method.
    """
    repo = _StubSignalPerformanceRepo()
    eng = _engine(repo)

    status = eng._update_consecutive_tracker(
        strategy_id="strat_a",
        strat_ver_id=1,
        instr_id=42,
        signal_id=100,
        signal_ts=datetime(2026, 5, 7, 12, 0, tzinfo=UTC),
        is_correct=False,
    )

    assert len(repo.update_calls) == 1
    call = repo.update_calls[0]
    # The engine forwards self.wrong_threshold as the port arg.
    assert call["wrong_threshold"] == 3
    # The status carries the canonical symbol from the engine cache.
    assert status.symbol == "BTC-USD"
    assert status.consecutive_wrong_count == 1
    assert status.threshold_reached is False


def test_threshold_reached_flips_after_wrong_threshold_consecutive_misses() -> None:
    """After ``wrong_threshold`` consecutive failures, ``threshold_reached`` is True."""
    repo = _StubSignalPerformanceRepo()
    eng = _engine(repo)

    last = None
    for sig_id in (100, 101, 102):
        last = eng._update_consecutive_tracker(
            strategy_id="strat_a",
            strat_ver_id=1,
            instr_id=42,
            signal_id=sig_id,
            signal_ts=datetime(2026, 5, 7, 12, sig_id - 100, tzinfo=UTC),
            is_correct=False,
        )
    assert last is not None
    assert last.consecutive_wrong_count == 3
    assert last.threshold_reached is True
    assert last.threshold_reached_at is not None


def test_correct_prediction_resets_counter() -> None:
    repo = _StubSignalPerformanceRepo()
    eng = _engine(repo)

    eng._update_consecutive_tracker(
        strategy_id="strat_a",
        strat_ver_id=1,
        instr_id=42,
        signal_id=100,
        signal_ts=datetime(2026, 5, 7, 12, 0, tzinfo=UTC),
        is_correct=False,
    )
    eng._update_consecutive_tracker(
        strategy_id="strat_a",
        strat_ver_id=1,
        instr_id=42,
        signal_id=101,
        signal_ts=datetime(2026, 5, 7, 12, 1, tzinfo=UTC),
        is_correct=False,
    )
    after_correct = eng._update_consecutive_tracker(
        strategy_id="strat_a",
        strat_ver_id=1,
        instr_id=42,
        signal_id=102,
        signal_ts=datetime(2026, 5, 7, 12, 2, tzinfo=UTC),
        is_correct=True,
    )
    assert after_correct.consecutive_wrong_count == 0
    assert after_correct.threshold_reached is False


def test_has_pending_optimization_routes_through_port() -> None:
    repo = _StubSignalPerformanceRepo()
    repo.set_pending("strat_with_pending")
    eng = _engine(repo)

    assert eng._has_pending_optimization("strat_with_pending") is True
    assert eng._has_pending_optimization("strat_no_pending") is False
    assert repo.pending_calls == ["strat_with_pending", "strat_no_pending"]


def test_engine_no_longer_imports_strategy_consecutive_wrong_tracker() -> None:
    """Hard regression: the engine module must not actually import
    ``StrategyConsecutiveWrongTracker`` or ``StrategyParameterFeedback``
    — that's the port adapter's responsibility (Plan §6.9). The check
    targets ``import`` statements specifically (not docstrings, not
    error-message strings)."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "feedback_loop_engine" / "engine.py"
    text = src.read_text(encoding="utf-8")

    # Strip docstrings before grepping so explanatory references don't trigger.
    no_docstrings = re.sub(r'""".*?"""', "", text, flags=re.DOTALL)

    bad_patterns = [
        re.compile(r"\bimport\s+StrategyConsecutiveWrongTracker\b"),
        re.compile(r"\bStrategyConsecutiveWrongTracker\s*[,\)]"),
        re.compile(r"\bimport\s+StrategyParameterFeedback\b"),
        re.compile(r"\bStrategyParameterFeedback\s*[,\)]"),
    ]
    for pat in bad_patterns:
        match = pat.search(no_docstrings)
        assert match is None, (
            f"feedback_loop_engine/engine.py still references the ORM model "
            f"({match.group(0)!r}) outside docstrings. Route the access through "
            "ISignalPerformanceRepository instead."
        )


def test_link_feedback_to_tracker_routes_through_port() -> None:
    repo = _StubSignalPerformanceRepo()
    eng = _engine(repo)

    eng._link_feedback_to_tracker("strat_a", 42, "1h", 999)

    assert repo.link_calls == [
        {
            "strategy_id": "strat_a",
            "instr_id": 42,
            "horizon": "1h",
            "feedback_id": 999,
        }
    ]


def test_suggestion_review_service_renders_pending_port_records() -> None:
    """The review service owns the API-facing pending-suggestion shape."""
    created = datetime(2026, 5, 7, 10, 0, tzinfo=UTC)
    repo = _StubSignalPerformanceRepo()
    repo.add_pending_suggestion(
        PendingSuggestion(
            feedback_id=1,
            strategy_id="strat_a",
            strat_ver_id=1,
            instr_id=42,
            horizon="1d",
            trigger_reason="consecutive_wrong",
            consecutive_wrong=3,
            current_params={"x": 1},
            suggested_params={"x": 2},
            explanation="loosen threshold",
            created_at=created,
        )
    )
    repo.add_pending_suggestion(
        PendingSuggestion(
            feedback_id=2,
            strategy_id="strat_b",
            strat_ver_id=2,
            instr_id=43,
            horizon="1d",
            trigger_reason="manual",
            consecutive_wrong=0,
        )
    )
    reviews = SuggestionReviewService(repo)

    all_pending = reviews.list_pending()
    assert [s["feedback_id"] for s in all_pending] == [1, 2]
    assert all_pending[0]["created_at"] == created.isoformat()
    assert all_pending[0]["current_params"] == {"x": 1}
    assert all_pending[0]["suggested_params"] == {"x": 2}
    # ``created_at=None`` renders as ``None`` (not "" or stringified).
    assert all_pending[1]["created_at"] is None

    # Filter by strategy
    only_a = reviews.list_pending("strat_a")
    assert len(only_a) == 1
    assert only_a[0]["strategy_id"] == "strat_a"
    assert reviews.get_pending(1) == all_pending[0]
    assert reviews.get_pending(999) is None


def test_suggestion_review_transitions_route_through_port() -> None:
    repo = _StubSignalPerformanceRepo()
    repo.add_pending_suggestion(
        PendingSuggestion(
            feedback_id=42,
            strategy_id="strat_a",
            strat_ver_id=1,
            instr_id=42,
            horizon="1d",
            trigger_reason="consecutive_wrong",
            consecutive_wrong=3,
            current_params={"ema_period": 10},
            suggested_params={"ema_period": 15},
        )
    )
    reviews = SuggestionReviewService(repo)

    assert reviews.approve(feedback_id=42, reviewer_user_id="reviewer_a") is True
    assert reviews.approve(feedback_id=999, reviewer_user_id="reviewer_a") is False
    assert repo.approve_calls == [
        {"feedback_id": 42, "reviewer_user_id": "reviewer_a"},
    ]

    assert (
        reviews.reject(
            feedback_id=42,
            reviewer_user_id="reviewer_b",
            reason="not safe",
        )
        is True
    )
    assert reviews.reject(feedback_id=999, reviewer_user_id="reviewer_b") is False
    assert repo.reject_calls == [
        {"feedback_id": 42, "reviewer_user_id": "reviewer_b", "reason": "not safe"},
        {"feedback_id": 999, "reviewer_user_id": "reviewer_b", "reason": None},
    ]


def test_suggestion_review_refuses_blank_reviewer_and_noop_approval() -> None:
    repo = _StubSignalPerformanceRepo()
    repo.add_pending_suggestion(
        PendingSuggestion(
            feedback_id=7,
            strategy_id="strat_a",
            strat_ver_id=1,
            instr_id=42,
            horizon="1d",
            trigger_reason="consecutive_wrong",
            consecutive_wrong=3,
            current_params={"ema_period": 10},
            suggested_params={"ema_period": 10},
        )
    )
    reviews = SuggestionReviewService(repo)

    with pytest.raises(ValueError, match="must not be blank"):
        reviews.approve(feedback_id=7, reviewer_user_id=" ")
    assert reviews.approve(feedback_id=7, reviewer_user_id="reviewer") is False
    assert repo.approve_calls == []
