"""Section L — Signal Performance & Feedback Loop.

Tables: ``signal_performance``, ``strategy_parameter_feedback``,
``backtest_experiments``, ``backtest_trials``, ``backtest_results``,
``strategy_consecutive_wrong_tracker``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    inspect,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from lib_common.asset_classes import ASSET_CLASS_CHECK_SQL

from ._base import Base, JSONType, SQLiteBigInteger


class SignalPerformance(Base):
    """Track prediction accuracy for each signal to evaluate strategy performance.

    Stores whether a signal's prediction was correct by comparing the predicted
    direction against actual price movement after a configurable horizon.
    """

    __tablename__ = "signal_performance"

    perf_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("canonical_signals.signal_id"), nullable=False
    )
    strategy_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("strategies.strategy_id"), nullable=False
    )
    strat_ver_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("strategy_versions.strat_ver_id")
    )
    instr_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("instruments.instr_id"), nullable=False
    )

    # Signal data at time of prediction
    predicted_direction: Mapped[str] = mapped_column(String(10), nullable=False)  # long, short
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    signal_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))

    # Evaluation window
    evaluation_horizon: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # 15min, 1h, 4h, 1d, 1w, 2w, 1m
    evaluation_ts: Mapped[datetime | None] = mapped_column(
        DateTime
    )  # When we evaluated the outcome
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))

    # Outcome
    actual_direction: Mapped[str | None] = mapped_column(
        String(10)
    )  # long (up), short (down), flat
    price_change_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))  # Percentage change
    is_correct: Mapped[bool | None] = mapped_column(Boolean)  # True if prediction matched actual
    pnl_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))  # P&L if trade was taken
    # Whether the signal actually resulted in a broker execution (DB-2). Grounds
    # the feedback in real trading: signal quality (is_correct/pnl_pct, a
    # hypothetical price-move) can now be analyzed separately from whether the
    # signal was actually traded. NULL = execution status not linked.
    did_execute: Mapped[bool | None] = mapped_column(Boolean)

    # Consecutive tracking (for feedback loop threshold)
    consecutive_wrong_count: Mapped[int | None] = mapped_column(
        Integer, default=0, server_default="0"
    )
    needs_optimization: Mapped[bool | None] = mapped_column(
        Boolean, default=False, server_default="false"
    )

    # Cross-container correlation
    run_id: Mapped[str | None] = mapped_column(
        String(64), index=True
    )  # Propagated from CanonicalSignal

    # Metadata
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime)
    meta: Mapped[Any | None] = mapped_column(JSONType)

    __table_args__ = (
        CheckConstraint(
            "predicted_direction IN ('long', 'short')",
            name="ck_signal_perf_predicted",
        ),
        CheckConstraint(
            "actual_direction IN ('long', 'short', 'flat')",
            name="ck_signal_perf_actual",
        ),
        CheckConstraint(
            "evaluation_horizon IN ('15min', '1h', '4h', '1d', '1w', '2w', '1m')",
            name="ck_signal_perf_horizon",
        ),
        UniqueConstraint(
            "signal_id",
            "evaluation_horizon",
            name="uq_signal_performance_signal_horizon",
        ),
        Index("ix_signal_perf_strategy", "strategy_id", "signal_ts"),
        Index("ix_signal_perf_needs_opt", "needs_optimization", "strategy_id"),
    )


class StrategyParameterFeedback(Base):
    """Store parameter optimization suggestions for strategies.

    When a strategy makes consecutive wrong predictions (threshold: 2),
    the feedback loop engine generates optimization suggestions.
    A trader reviews and approves suggestions for a separate source-controlled
    strategy change. Runtime services never mutate strategy files.
    """

    __tablename__ = "strategy_parameter_feedback"

    feedback_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("strategies.strategy_id"), nullable=False
    )
    strat_ver_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("strategy_versions.strat_ver_id")
    )
    instr_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("instruments.instr_id"))
    horizon: Mapped[str] = mapped_column(String(10), nullable=False)

    # Trigger info
    trigger_reason: Mapped[str] = mapped_column(
        String(100), nullable=False
    )  # consecutive_wrong, low_accuracy, etc.
    consecutive_wrong_signals: Mapped[int | None] = mapped_column(
        Integer, default=0, server_default="0"
    )
    accuracy_window_days: Mapped[int | None] = mapped_column(Integer)
    accuracy_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))

    # Current parameters (snapshot)
    current_params: Mapped[Any] = mapped_column(JSONType, nullable=False)

    # Suggested parameters
    suggested_params: Mapped[Any] = mapped_column(JSONType, nullable=False)
    optimization_method: Mapped[str | None] = mapped_column(String(50))  # bounded_heuristic

    # Explanation
    explanation: Mapped[str | None] = mapped_column(Text)
    supporting_data: Mapped[Any | None] = mapped_column(
        JSONType
    )  # Backtest results, statistics, etc.

    # Approval workflow
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending", server_default="pending"
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(50), ForeignKey("users.user_id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    review_notes: Mapped[str | None] = mapped_column(Text)

    # Timing
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired')",
            name="ck_param_feedback_status",
        ),
        CheckConstraint(
            "trigger_reason IN ('consecutive_wrong', 'low_accuracy', "
            "'high_drawdown', 'manual_review', 'scheduled')",
            name="ck_param_feedback_trigger",
        ),
        CheckConstraint(
            "horizon IN ('15min', '1h', '4h', '1d', '1w', '2w', '1m')",
            name="ck_param_feedback_horizon",
        ),
        Index("ix_param_feedback_strategy_status", "strategy_id", "status"),
        Index(
            "uq_pending_feedback_scope",
            "strategy_id",
            "instr_id",
            "horizon",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )


class BacktestExperiment(Base):
    """Group backtest runs for parameter sweeps and walk-forward experiments."""

    __tablename__ = "backtest_experiments"

    experiment_id: Mapped[int] = mapped_column(
        SQLiteBigInteger, primary_key=True, autoincrement=True
    )
    strategy_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("strategies.strategy_id"), nullable=False
    )
    experiment_type: Mapped[str] = mapped_column(String(30), nullable=False)
    metric: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="running", server_default="running"
    )

    config: Mapped[Any] = mapped_column(JSONType, nullable=False)
    summary: Mapped[Any | None] = mapped_column(JSONType)

    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(tz=UTC)
    )

    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "strategy_id",
            name="uq_backtest_experiment_lineage",
        ),
        CheckConstraint(
            "experiment_type IN ('parameter_sweep', 'walk_forward', 'stress_test', 'monte_carlo')",
            name="ck_backtest_experiment_type",
        ),
        CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_backtest_experiment_status",
        ),
        Index("ix_backtest_experiment_strategy", "strategy_id", "created_at"),
    )


class BacktestTrial(Base):
    """Append-only identity and lifecycle ledger for one backtest attempt.

    The identity fields are frozen after insertion. Lifecycle fields may move
    through the controlled transitions implemented by ``BacktestTrialStore``;
    completed, failed, interrupted, and abandoned attempts remain queryable so
    unsuccessful research cannot disappear from multiplicity accounting.
    """

    __tablename__ = "backtest_trials"

    trial_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    experiment_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    strategy_id: Mapped[str] = mapped_column(String(50), nullable=False)
    strat_ver_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    trial_family: Mapped[str] = mapped_column(String(64), nullable=False)
    fold_id: Mapped[str] = mapped_column(String(64), nullable=False)
    cost_scenario: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters: Mapped[Any] = mapped_column(JSONType, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="registered", server_default="registered"
    )
    error_class: Mapped[str | None] = mapped_column(String(255))
    error_context: Mapped[str | None] = mapped_column(Text)
    result_id: Mapped[int | None] = mapped_column(BigInteger)

    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
        onupdate=lambda: datetime.now(tz=UTC),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["experiment_id", "strategy_id"],
            ["backtest_experiments.experiment_id", "backtest_experiments.strategy_id"],
            name="fk_backtest_trial_experiment_lineage",
        ),
        ForeignKeyConstraint(
            ["strat_ver_id", "strategy_id"],
            ["strategy_versions.strat_ver_id", "strategy_versions.strategy_id"],
            name="fk_backtest_trial_version_lineage",
        ),
        ForeignKeyConstraint(
            ["result_id", "strategy_id", "strat_ver_id", "experiment_id"],
            [
                "backtest_results.result_id",
                "backtest_results.strategy_id",
                "backtest_results.strat_ver_id",
                "backtest_results.experiment_id",
            ],
            name="fk_backtest_trial_result_lineage",
        ),
        CheckConstraint("sequence >= 0", name="ck_backtest_trial_sequence"),
        CheckConstraint(
            "status IN ('registered', 'running', 'completed', 'failed', "
            "'interrupted', 'abandoned')",
            name="ck_backtest_trial_status",
        ),
        CheckConstraint("length(manifest_hash) = 64", name="ck_backtest_trial_manifest_hash"),
        UniqueConstraint("experiment_id", "sequence", name="uq_backtest_trial_experiment_sequence"),
        UniqueConstraint("result_id", name="uq_backtest_trial_result"),
        Index("ix_backtest_trial_experiment_status", "experiment_id", "status"),
        Index(
            "ix_backtest_trial_strategy_version",
            "strategy_id",
            "strat_ver_id",
        ),
        Index("ix_backtest_trial_manifest", "manifest_hash"),
    )


_BACKTEST_TRIAL_IDENTITY_FIELDS = (
    "trial_id",
    "experiment_id",
    "strategy_id",
    "strat_ver_id",
    "sequence",
    "trial_family",
    "fold_id",
    "cost_scenario",
    "parameters",
    "manifest_hash",
)


@event.listens_for(BacktestTrial, "before_update")
def _prevent_backtest_trial_identity_update(_mapper: Any, _connection: Any, target: Any) -> None:
    """Reject ORM updates that would rewrite an attempted trial's identity."""

    state = inspect(target)
    changed = [
        field
        for field in _BACKTEST_TRIAL_IDENTITY_FIELDS
        if state.attrs[field].history.has_changes()
    ]
    if changed:
        fields = ", ".join(changed)
        msg = f"backtest trial identity is immutable: {fields}"
        raise ValueError(msg)


@event.listens_for(BacktestTrial, "before_delete")
def _prevent_backtest_trial_delete(_mapper: Any, _connection: Any, _target: Any) -> None:
    """Keep failed and completed trial evidence append-only through the ORM."""

    msg = "backtest trials are append-only and cannot be deleted"
    raise ValueError(msg)


class BacktestResult(Base):
    """Persist backtest results for strategies.

    Stores performance metrics from backtests for comparison and
    to inform the feedback loop's optimization decisions.
    """

    __tablename__ = "backtest_results"

    result_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("strategies.strategy_id"), nullable=False
    )
    strat_ver_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("strategy_versions.strat_ver_id")
    )
    experiment_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("backtest_experiments.experiment_id"),
        nullable=True,
    )

    # Backtest configuration
    backtest_id: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False
    )  # UUID or LEAN backtest ID
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(20), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    initial_capital: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    timeframe: Mapped[str | None] = mapped_column(String(20))  # 1m, 5m, 15m, 1h, 1d

    # Parameters used
    parameters: Mapped[Any] = mapped_column(JSONType, nullable=False)

    # Performance metrics
    total_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    cagr_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    sharpe_ratio: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    sortino_ratio: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    max_drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    win_rate_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    profit_factor: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    avg_win_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    avg_loss_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    total_trades: Mapped[int | None] = mapped_column(Integer)
    winning_trades: Mapped[int | None] = mapped_column(Integer)
    losing_trades: Mapped[int | None] = mapped_column(Integer)

    # Signal statistics
    total_signals: Mapped[int | None] = mapped_column(Integer)
    long_signals: Mapped[int | None] = mapped_column(Integer)
    short_signals: Mapped[int | None] = mapped_column(Integer)
    correct_predictions: Mapped[int | None] = mapped_column(Integer)
    prediction_accuracy_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))

    # Execution details
    final_equity: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    peak_equity: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    fees_paid: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))

    # Metadata
    engine: Mapped[str] = mapped_column(
        String(50), nullable=False, default="internal", server_default="internal"
    )  # internal, custom
    log_file_path: Mapped[str | None] = mapped_column(String(500))
    trades_json: Mapped[Any | None] = mapped_column(JSONType)  # List of all trades
    equity_curve: Mapped[Any | None] = mapped_column(JSONType)  # Time series of equity
    meta: Mapped[Any | None] = mapped_column(JSONType)

    # Timing
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "result_id",
            "strategy_id",
            "strat_ver_id",
            "experiment_id",
            name="uq_backtest_result_lineage",
        ),
        CheckConstraint(
            ASSET_CLASS_CHECK_SQL,
            name="ck_backtest_asset_class",
        ),
        CheckConstraint(
            "engine IN ('internal', 'custom')",
            name="ck_backtest_engine",
        ),
        Index("ix_backtest_strategy_date", "strategy_id", "end_date"),
        Index("ix_backtest_results_experiment", "experiment_id"),
    )


class StrategyConsecutiveWrongTracker(Base):
    """Track consecutive wrong predictions per strategy per instrument.

    This is a real-time tracker that gets updated after each signal evaluation.
    When count reaches threshold (default: 2), triggers optimization feedback.
    """

    __tablename__ = "strategy_consecutive_wrong_tracker"

    tracker_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("strategies.strategy_id"), nullable=False
    )
    strat_ver_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("strategy_versions.strat_ver_id")
    )
    instr_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("instruments.instr_id"), nullable=False
    )
    # Evaluation horizon (1h/4h/1d/...). The tracker is per
    # (strategy, instrument, horizon) so the horizons do not corrupt each
    # other's consecutive-wrong counters. Every writer supplies this identity
    # explicitly; there is no provenance-fabricating default.
    horizon: Mapped[str] = mapped_column(String(10), nullable=False)

    # Tracking
    consecutive_wrong_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_signal_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("canonical_signals.signal_id")
    )
    last_signal_ts: Mapped[datetime | None] = mapped_column(DateTime)
    last_evaluation_ts: Mapped[datetime | None] = mapped_column(DateTime)

    # Threshold configuration
    wrong_threshold: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    threshold_reached: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    threshold_reached_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Feedback link
    feedback_id: Mapped[int | None] = mapped_column(
        SQLiteBigInteger, ForeignKey("strategy_parameter_feedback.feedback_id")
    )

    # Reset tracking
    last_reset_ts: Mapped[datetime | None] = mapped_column(DateTime)
    reset_reason: Mapped[str | None] = mapped_column(
        String(100)
    )  # correct_prediction, manual_reset, params_updated

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "strategy_id", "instr_id", "horizon", name="uq_strategy_instr_horizon_tracker"
        ),
        Index("ix_tracker_threshold", "threshold_reached", "strategy_id"),
    )


class ServiceHeartbeat(Base):
    """Last-success heartbeat for a service that cannot be Prometheus-scraped
    directly.

    The feedback loop runs as a one-shot (it evaluates a cycle and exits), so an
    in-process gauge is never scraped. It writes a row here on every successful
    cycle; an always-on service or external monitor reads ``last_success_at`` to
    alert when the feedback loop has gone stale. Generic by design (keyed on
    ``service_name``) so other one-shot/cron services can reuse it.
    """

    __tablename__ = "service_heartbeats"

    service_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_success_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ok", server_default="ok"
    )
    detail: Mapped[str | None] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
        onupdate=lambda: datetime.now(tz=UTC),
    )


__all__ = [
    "BacktestExperiment",
    "BacktestResult",
    "BacktestTrial",
    "ServiceHeartbeat",
    "SignalPerformance",
    "StrategyConsecutiveWrongTracker",
    "StrategyParameterFeedback",
]
