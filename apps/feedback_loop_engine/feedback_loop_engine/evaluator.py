"""Signal Evaluator - Evaluates signal prediction accuracy.

Compares predicted direction against actual price movement after
a configurable time horizon to determine if prediction was correct.

The feedback loop currently measures signal quality only. It does not
consume ``execution.results`` and therefore does not model slippage,
fill quality, or broker latency.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from lib_common.logging import get_logger

from .models import Direction, EvaluationHorizon, SignalEvaluation
from .price_provider import NullPriceProvider, PriceProvider

if TYPE_CHECKING:
    from sqlalchemy import Engine

logger = get_logger(__name__)

# Per-strategy horizon gating: which evaluation horizons make sense for a signal's
# intended holding period (canonical_signals.horizon_seconds). A 1-minute scalper
# scored at 1w/1m measures a different regime, not its trade — and every such
# bogus evaluation inflates the consecutive-wrong tracker. Signals without an
# explicit holding period are ineligible because assigning a horizon would
# fabricate evaluation semantics.
_INTRADAY_MAX_S = 3600  # <= 1h
_SWING_MAX_S = 86400  # <= 1d


def eligible_horizons_for(horizon_seconds: float | None) -> set[EvaluationHorizon]:
    """Evaluation horizons appropriate for a signal's declared holding period."""
    if horizon_seconds is None:
        return set()
    if horizon_seconds <= _INTRADAY_MAX_S:
        return {EvaluationHorizon.MIN15, EvaluationHorizon.H1}
    if horizon_seconds <= _SWING_MAX_S:
        return {EvaluationHorizon.H4, EvaluationHorizon.D1}
    return {EvaluationHorizon.W1, EvaluationHorizon.W2, EvaluationHorizon.M1}


# Minimum price change to consider non-flat (0.1%)
MIN_PRICE_CHANGE_PCT = 0.001


class SignalEvaluator:
    """Evaluates signal predictions against actual price movement."""

    def __init__(
        self,
        engine: Engine,
        default_horizon: EvaluationHorizon = EvaluationHorizon.D1,
        flat_threshold_pct: float = MIN_PRICE_CHANGE_PCT,
        price_provider: PriceProvider | None = None,
    ) -> None:
        """Initialize the signal evaluator.

        Args:
            engine: SQLAlchemy engine for database access
            default_horizon: Default evaluation time horizon
            flat_threshold_pct: Price change threshold below which direction is 'flat'
        """
        self._engine = engine
        self.default_horizon = default_horizon
        self.flat_threshold_pct = flat_threshold_pct
        self.price_provider = price_provider or NullPriceProvider()

    def evaluate_signal(
        self,
        signal_id: int,
        entry_price: float,
        exit_price: float,
        predicted_direction: str,
        evaluation_horizon: EvaluationHorizon,
        signal_ts: datetime,
        strategy_id: str,
        strat_ver_id: int | None,
        instr_id: int,
        symbol: str,
        confidence: float = 1.0,
    ) -> SignalEvaluation:
        """Evaluate a single signal's prediction accuracy.

        Args:
            signal_id: Database ID of the signal
            entry_price: Price at signal time
            exit_price: Price at evaluation time
            predicted_direction: 'long' or 'short'
            evaluation_horizon: Time horizon for evaluation
            signal_ts: Timestamp of the signal
            strategy_id: Strategy that generated the signal
            strat_ver_id: Strategy version ID
            instr_id: Instrument ID
            symbol: Symbol name
            confidence: Signal confidence score

        Returns:
            SignalEvaluation with prediction accuracy details
        """
        if exit_price is None:
            msg = "Exit price is required for evaluation"
            raise ValueError(msg)

        # Calculate price change
        price_change_pct = (exit_price - entry_price) / entry_price

        # Determine actual direction
        if abs(price_change_pct) < self.flat_threshold_pct:
            actual_direction = Direction.FLAT
        elif price_change_pct > 0:
            actual_direction = Direction.LONG
        else:
            actual_direction = Direction.SHORT

        # Determine if prediction was correct
        predicted_dir = Direction(predicted_direction)
        is_correct = self._is_prediction_correct(predicted_dir, actual_direction)

        # Calculate PnL if trade was taken
        pnl_pct = price_change_pct if predicted_dir == Direction.LONG else -price_change_pct

        evaluation_ts = datetime.now(tz=UTC)

        return SignalEvaluation(
            signal_id=signal_id,
            strategy_id=strategy_id,
            strat_ver_id=strat_ver_id,
            instr_id=instr_id,
            symbol=symbol,
            predicted_direction=predicted_dir,
            confidence=confidence,
            signal_ts=signal_ts,
            entry_price=entry_price,
            evaluation_horizon=evaluation_horizon,
            evaluation_ts=evaluation_ts,
            exit_price=exit_price,
            actual_direction=actual_direction,
            price_change_pct=price_change_pct,
            is_correct=is_correct,
            pnl_pct=pnl_pct,
        )

    def _is_prediction_correct(self, predicted: Direction, actual: Direction) -> bool:
        """Determine if prediction matches actual direction.

        A prediction is considered correct if:
        - LONG prediction and price went up (LONG actual)
        - SHORT prediction and price went down (SHORT actual)

        A prediction is **incorrect** if:
        - LONG or SHORT prediction but price was FLAT (within threshold).
          A directional prediction on a flat market is not a win — it should
          not inflate accuracy metrics or suppress the consecutive-wrong
          counter.  Only a HOLD prediction would be appropriate for a flat
          market, but HOLD signals are filtered out before evaluation (see
          ``get_pending_signals_for_evaluation``), so flat outcomes should
          never be counted as correct for directional predictions.
        """
        if actual == Direction.FLAT:
            # Directional predictions on flat markets are NOT correct.
            # This prevents accuracy inflation and ensures the consecutive
            # wrong tracker properly triggers parameter optimization.
            return False
        return predicted == actual

    def persist_evaluation(
        self,
        evaluation: SignalEvaluation,
        consecutive_wrong_count: int = 0,
        needs_optimization: bool = False,
        run_id: str | None = None,
        did_execute: bool | None = None,
        on_persisted: Callable[[int, Session], None] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> tuple[int, bool]:
        """Persist signal evaluation to database.

        When ``on_persisted`` is supplied it is invoked with (perf_id, session)
        after the SignalPerformance row is flushed but BEFORE commit, so a
        caller can co-commit a transactional-outbox event in the same
        transaction (the row and its downstream event are all-or-nothing).

        Args:
            evaluation: The signal evaluation result
            consecutive_wrong_count: Current consecutive wrong count
            needs_optimization: Whether optimization is needed
            meta: Versioned price-observation provenance for this evaluation

        Returns:
            Database ID of the persisted record and whether this call inserted
            it. A replay of the same signal/horizon returns the original ID
            with ``False`` and does not emit a duplicate outbox event.
        """
        from lib_application.db.models import (  # noqa: PLC0415
            SignalPerformance,
        )

        with Session(self._engine) as session:
            values = {
                "signal_id": evaluation.signal_id,
                "strategy_id": evaluation.strategy_id,
                "strat_ver_id": evaluation.strat_ver_id,
                "instr_id": evaluation.instr_id,
                "predicted_direction": evaluation.predicted_direction.value,
                "confidence": float(evaluation.confidence),
                "signal_ts": evaluation.signal_ts,
                "entry_price": float(evaluation.entry_price),
                "evaluation_horizon": evaluation.evaluation_horizon.value,
                "evaluation_ts": evaluation.evaluation_ts,
                "exit_price": float(evaluation.exit_price),
                "actual_direction": evaluation.actual_direction.value,
                "price_change_pct": float(evaluation.price_change_pct),
                "is_correct": evaluation.is_correct,
                "pnl_pct": float(evaluation.pnl_pct),
                "consecutive_wrong_count": consecutive_wrong_count,
                "needs_optimization": needs_optimization,
                "run_id": run_id,
                "did_execute": did_execute,
                "evaluated_at": datetime.now(tz=UTC),
                "meta": meta,
            }
            dialect_name = session.get_bind().dialect.name
            if dialect_name == "postgresql":
                statement = (
                    postgresql_insert(SignalPerformance)
                    .values(**values)
                    .on_conflict_do_nothing(constraint="uq_signal_performance_signal_horizon")
                    .returning(SignalPerformance.perf_id)
                )
            elif dialect_name == "sqlite":
                statement = (
                    sqlite_insert(SignalPerformance)
                    .values(**values)
                    .on_conflict_do_nothing(
                        index_elements=[
                            SignalPerformance.signal_id,
                            SignalPerformance.evaluation_horizon,
                        ]
                    )
                    .returning(SignalPerformance.perf_id)
                )
            else:
                msg = f"Unsupported feedback persistence dialect: {dialect_name}"
                raise RuntimeError(msg)

            inserted_perf_id = session.execute(statement).scalar_one_or_none()
            if inserted_perf_id is not None:
                inserted = True
                perf_id_result = int(inserted_perf_id)
            else:
                inserted = False
                existing_perf_id = session.scalar(
                    select(SignalPerformance.perf_id).where(
                        SignalPerformance.signal_id == evaluation.signal_id,
                        SignalPerformance.evaluation_horizon == evaluation.evaluation_horizon.value,
                    )
                )
                if existing_perf_id is None:
                    msg = "Feedback upsert lost its natural-key row"
                    raise RuntimeError(msg)
                perf_id_result = int(existing_perf_id)

            if inserted and on_persisted is not None:
                on_persisted(perf_id_result, session)
            session.commit()
            return perf_id_result, inserted

    def get_pending_signals_for_evaluation(
        self,
        horizon: EvaluationHorizon,
        limit: int = 100,
    ) -> list[dict]:
        """Get signals that are ready for evaluation but haven't been evaluated.

        Args:
            horizon: Evaluation horizon to check
            limit: Maximum number of signals to return

        Returns:
            List of signal dictionaries ready for evaluation
        """
        # Import here to avoid circular imports
        from lib_application.db.models import (  # noqa: PLC0415
            CanonicalSignal,
            Instrument,
            SignalPerformance,
        )

        horizon_delta = horizon.duration
        cutoff_time = datetime.now(tz=UTC) - horizon_delta

        with Session(self._engine) as session:
            # Get signals older than horizon that haven't been evaluated
            existing_signal_ids = select(SignalPerformance.signal_id).where(
                SignalPerformance.evaluation_horizon == horizon.value
            )

            stmt = (
                select(CanonicalSignal, Instrument.canonical)
                .join(Instrument, CanonicalSignal.instr_id == Instrument.instr_id)
                .where(
                    CanonicalSignal.ts <= cutoff_time,
                    CanonicalSignal.action.in_(["long", "short"]),
                    CanonicalSignal.signal_id.not_in(existing_signal_ids),
                )
                .order_by(CanonicalSignal.ts, CanonicalSignal.signal_id)
                .limit(limit)
            )

            rows = session.execute(stmt).all()

            signals = []
            for row, symbol in rows:
                # Per-strategy horizon gating: skip signals whose declared holding
                # period does not map to this evaluation horizon (a 1m scalp is not
                # scored at 1w/1m). Missing horizon metadata fails closed.
                if horizon not in eligible_horizons_for(row.horizon_seconds):
                    continue
                signals.append(
                    {
                        "signal_id": row.signal_id,
                        "strategy_id": row.strategy_id,
                        "strat_ver_id": row.strat_ver_id,
                        "instr_id": row.instr_id,
                        "symbol": symbol,
                        "action": row.action,
                        "confidence": float(row.confidence) if row.confidence else 1.0,
                        "signal_ts": row.ts,
                        "entry_price": (
                            float(row.entry_price) if row.entry_price is not None else None
                        ),
                        "run_id": row.run_id,
                        "horizon_seconds": (
                            float(row.horizon_seconds) if row.horizon_seconds is not None else None
                        ),
                        "signal_meta": row.signal_meta if row.signal_meta else {},
                    }
                )

            return signals
