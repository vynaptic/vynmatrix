"""Data models for the Feedback Loop Engine."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any


class EvaluationHorizon(str, Enum):
    """Time horizons for signal evaluation.

    NOTE: ``M1 = "1m"`` means one MONTH (30 days), not one minute — a 1-minute
    scalper is evaluated at the sub-hour ``MIN15`` bucket, not ``M1``.
    """

    MIN15 = "15min"  # sub-hour bucket for 1m scalpers (15 minutes)
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"
    W2 = "2w"
    M1 = "1m"  # one MONTH (30 days), not one minute

    @property
    def duration(self) -> timedelta:
        """SINGLE source of truth for horizon length.

        An unknown horizon string must fail loudly at
        ``EvaluationHorizon(<value>)`` — never default silently — so a
        malformed horizon can never be evaluated against the wrong window.
        """
        return _HORIZON_DURATIONS[self]


_HORIZON_DURATIONS: dict[EvaluationHorizon, timedelta] = {
    EvaluationHorizon.MIN15: timedelta(minutes=15),
    EvaluationHorizon.H1: timedelta(hours=1),
    EvaluationHorizon.H4: timedelta(hours=4),
    EvaluationHorizon.D1: timedelta(days=1),
    EvaluationHorizon.W1: timedelta(weeks=1),
    EvaluationHorizon.W2: timedelta(weeks=2),
    EvaluationHorizon.M1: timedelta(days=30),
}


class Direction(str, Enum):
    """Trade direction."""

    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class TriggerReason(str, Enum):
    """Reasons for triggering parameter optimization."""

    CONSECUTIVE_WRONG = "consecutive_wrong"
    LOW_ACCURACY = "low_accuracy"
    HIGH_DRAWDOWN = "high_drawdown"
    MANUAL_REVIEW = "manual_review"
    SCHEDULED = "scheduled"


class OptimizationMethod(str, Enum):
    """Methods for parameter optimization."""

    BOUNDED_HEURISTIC = "bounded_heuristic"


class FeedbackStatus(str, Enum):
    """Status of parameter feedback."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class SignalEvaluation:
    """Result of evaluating a signal's prediction accuracy."""

    signal_id: int
    strategy_id: str
    strat_ver_id: int | None
    instr_id: int
    symbol: str

    # Prediction
    predicted_direction: Direction
    confidence: float
    signal_ts: datetime
    entry_price: float

    # Evaluation
    evaluation_horizon: EvaluationHorizon
    evaluation_ts: datetime
    exit_price: float

    # Outcome
    actual_direction: Direction
    price_change_pct: float
    is_correct: bool
    pnl_pct: float


@dataclass
class ConsecutiveWrongStatus:
    """Status of consecutive wrong prediction tracking."""

    strategy_id: str
    strat_ver_id: int | None
    instr_id: int
    symbol: str
    horizon: EvaluationHorizon

    consecutive_wrong_count: int
    wrong_threshold: int
    threshold_reached: bool
    threshold_reached_at: datetime | None
    feedback_id: int | None

    last_signal_id: int | None
    last_signal_ts: datetime | None
    last_evaluation_ts: datetime | None


@dataclass
class OptimizationSuggestion:
    """Suggested parameter optimization for a strategy."""

    strategy_id: str
    strategy_code: str
    strat_ver_id: int | None
    instr_id: int | None
    symbol: str | None
    horizon: EvaluationHorizon

    # Trigger info
    trigger_reason: TriggerReason
    consecutive_wrong_signals: int
    accuracy_window_days: int | None
    accuracy_pct: float | None

    # Parameters
    current_params: dict[str, Any]
    suggested_params: dict[str, Any]
    optimization_method: OptimizationMethod

    # Explanation
    explanation: str
    supporting_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyPerformanceStats:
    """Aggregated performance statistics for a strategy."""

    strategy_id: str
    strategy_code: str
    strat_ver_id: int | None
    instr_id: int | None
    symbol: str | None

    # Stats
    total_signals: int
    correct_predictions: int
    wrong_predictions: int
    accuracy_pct: float

    # Recent performance
    recent_signals: int
    recent_correct: int
    recent_accuracy_pct: float

    # Consecutive tracking
    current_consecutive_wrong: int
    max_consecutive_wrong: int

    # Time range
    period_start: datetime
    period_end: datetime
