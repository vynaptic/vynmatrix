"""Feedback Loop Engine - Main orchestrator for signal performance monitoring.

Coordinates:
1. Signal evaluation (comparing predictions to actual price movement)
2. Consecutive wrong tracking (per strategy per instrument)
3. Parameter optimization suggestion generation

Flow:
1. Periodically fetch signals ready for evaluation
2. Evaluate each signal against actual price data
3. Update consecutive wrong tracker
4. If threshold reached, generate optimization suggestion
5. Persist everything to database
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from lib_application.services.heartbeat_store import HeartbeatStore
from lib_common.internal_events import FeedbackEvaluationEvent
from lib_common.logging import get_logger
from lib_strategy.ports.signal_performance_port import (
    ISignalPerformanceRepository,
    PendingSuggestion,
)

from .evaluator import SignalEvaluator
from .mode_performance import ModePerformanceWriter
from .models import (
    ConsecutiveWrongStatus,
    EvaluationHorizon,
    SignalEvaluation,
    StrategyPerformanceStats,
    TriggerReason,
)
from .optimizer import ParameterOptimizer, SuggestionGenerationError
from .price_provider import (
    NullPriceProvider,
    PriceObservation,
    PriceObservationOrigin,
    PriceProvider,
    evaluation_target,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine

logger = get_logger(__name__)


class FeedbackLoopEngine:
    """Main engine for the feedback loop system.

    Monitors signal performance and generates optimization suggestions
    when strategies make consecutive wrong predictions.
    """

    def __init__(
        self,
        engine: Engine,
        wrong_threshold: int = 2,
        default_horizon: EvaluationHorizon = EvaluationHorizon.D1,
        price_provider: PriceProvider | None = None,
        outbox_store: Any | None = None,
        signal_performance_repo: ISignalPerformanceRepository | None = None,
    ) -> None:
        """Initialize the feedback loop engine.

        Args:
            engine: SQLAlchemy engine for database access
            wrong_threshold: Number of consecutive wrong predictions before triggering
            default_horizon: Default evaluation time horizon
            signal_performance_repo: Port adapter for the
                ``StrategyConsecutiveWrongTracker`` /
                ``StrategyParameterFeedback`` persistence concerns
                (Sprint F / Plan §6.9). When ``None``, the engine
                lazily constructs the SQLAlchemy adapter against
                ``self._engine``. Tests can inject a stub instead.
        """
        self._engine = engine
        self.wrong_threshold = wrong_threshold
        self.default_horizon = default_horizon
        self._outbox_store = outbox_store
        # DB-backed liveness: the feedback loop is a one-shot, so an in-process
        # gauge is never scraped. Each successful cycle records a heartbeat row
        # that a monitor reads to alert on staleness.
        self._heartbeat_store = HeartbeatStore(engine)
        # FB-5: populate mode_performance from executed outcomes so the scorer's
        # best/auto mode ranking has data instead of always falling back.
        self._mode_performance_writer = ModePerformanceWriter(engine)

        self._symbol_cache: dict[int, str] = {}  # instr_id → canonical symbol

        self.evaluator = SignalEvaluator(
            engine=engine,
            default_horizon=default_horizon,
            price_provider=price_provider or NullPriceProvider(),
        )
        self.optimizer = ParameterOptimizer(engine=engine)

        if signal_performance_repo is not None:
            self._signal_performance_repo = signal_performance_repo
        else:
            # Lazy import keeps the ``lib_infrastructure`` dependency optional
            # for libs-only test rigs that import this module but never run
            # the live consecutive-wrong tracker path.
            from lib_infrastructure.persistence.sqlalchemy.repositories import (  # noqa: PLC0415
                SQLAlchemySignalPerformanceRepository,
            )

            self._signal_performance_repo = SQLAlchemySignalPerformanceRepository(engine=engine)

    def run_evaluation_cycle(
        self,
        horizon: EvaluationHorizon | None = None,
        limit: int = 100,
    ) -> dict:
        """Run a single evaluation cycle.

        Fetches pending signals, evaluates them, updates trackers,
        and generates optimization suggestions if needed.

        Args:
            horizon: Evaluation horizon (defaults to default_horizon)
            limit: Maximum signals to process in this cycle

        Returns:
            Summary of the evaluation cycle
        """
        horizon = horizon or self.default_horizon
        with self._evaluation_cycle_lease(horizon):
            return self._run_evaluation_cycle_under_lease(horizon=horizon, limit=limit)

    def _run_evaluation_cycle_under_lease(
        self,
        *,
        horizon: EvaluationHorizon,
        limit: int,
    ) -> dict[str, int]:
        """Process one horizon while its PostgreSQL cycle fence is held."""

        logger.info("Starting evaluation cycle for horizon: %s", horizon.value)

        # Get pending signals
        pending_signals = self.evaluator.get_pending_signals_for_evaluation(
            horizon=horizon,
            limit=limit,
        )
        recovered_optimizations, recovery_errors = self._reconcile_unlinked_trackers()

        if not pending_signals:
            logger.info("No pending signals for evaluation")
            empty = {
                "signals_evaluated": 0,
                "correct_predictions": 0,
                "wrong_predictions": 0,
                "optimizations_triggered": recovered_optimizations,
                "skipped_no_price": 0,
                "errors": recovery_errors,
            }
            # An empty cycle still ran successfully — record liveness.
            self._record_cycle_heartbeat(horizon=horizon, results=empty)
            return empty

        logger.info("Found %d pending signals for evaluation", len(pending_signals))

        # Bulk-prefetch symbols to avoid N+1 queries during evaluation
        instr_ids = [s["instr_id"] for s in pending_signals if s.get("instr_id")]
        if instr_ids:
            self._prefetch_symbols(instr_ids)

        # Process each signal
        results = {
            "signals_evaluated": 0,
            "correct_predictions": 0,
            "wrong_predictions": 0,
            "optimizations_triggered": recovered_optimizations,
            # Signals skipped because a price was unavailable (e.g. the exit
            # horizon is still in the future, or no bar covers the window) —
            # an expected, non-error outcome kept separate from ``errors`` so a
            # genuine fault is not masked by routine skips.
            "skipped_no_price": 0,
            "errors": recovery_errors,
        }

        for signal in pending_signals:
            try:
                # Get price data for evaluation, preferring the same source
                # the strategy used when emitting the signal.
                sig_meta = signal.get("signal_meta") or {}
                entry_observation, exit_observation = self._get_price_observations_for_evaluation(
                    canonical_signal_id=signal["signal_id"],
                    instr_id=signal["instr_id"],
                    signal_ts=signal["signal_ts"],
                    horizon=horizon,
                    entry_price=signal.get("entry_price"),
                    price_source=sig_meta.get("price_source"),
                    price_timeframe=sig_meta.get("price_timeframe"),
                    signal_metadata=sig_meta,
                )

                if entry_observation is None or exit_observation is None:
                    # Not an error: the exit horizon may not have elapsed yet, or
                    # no price bar covers the window. Count as a skip so it does
                    # not inflate the error metric.
                    logger.debug(
                        "Skipping signal %s: price unavailable (entry=%s exit=%s)",
                        signal["signal_id"],
                        entry_observation,
                        exit_observation,
                    )
                    results["skipped_no_price"] += 1
                    continue

                target_ts = evaluation_target(signal["signal_ts"], horizon.value)
                price_meta = {
                    "price_provenance_schema": "v1",
                    "evaluation_target_ts": target_ts.isoformat(),
                    "entry_price_provenance": entry_observation.to_metadata(),
                    "exit_price_provenance": exit_observation.to_metadata(),
                }
                execution_lineage = self._signal_execution_lineage(signal["signal_id"])
                price_meta["execution_lineage_schema"] = "v1"
                price_meta["execution_lineage"] = execution_lineage

                # Evaluate the signal
                evaluation = self.evaluator.evaluate_signal(
                    signal_id=signal["signal_id"],
                    entry_price=entry_observation.price,
                    exit_price=exit_observation.price,
                    predicted_direction=signal["action"],
                    evaluation_horizon=horizon,
                    signal_ts=signal["signal_ts"],
                    strategy_id=signal["strategy_id"],
                    strat_ver_id=signal["strat_ver_id"],
                    instr_id=signal["instr_id"],
                    symbol=self._get_symbol(signal["instr_id"]),
                    confidence=signal.get("confidence", 1.0),
                )

                # Update tracker and check threshold
                tracker_status = self._update_consecutive_tracker(
                    strategy_id=signal["strategy_id"],
                    strat_ver_id=signal["strat_ver_id"],
                    instr_id=signal["instr_id"],
                    signal_id=signal["signal_id"],
                    signal_ts=signal["signal_ts"],
                    is_correct=evaluation.is_correct,
                    horizon=horizon,
                )
                tracker_applied = tracker_status.last_signal_id == signal["signal_id"]
                if not tracker_applied:
                    logger.warning(
                        "Signal %s was evaluated out of tracker order; "
                        "persisting performance without changing optimization state",
                        signal["signal_id"],
                    )
                evaluation_consecutive_wrong = (
                    tracker_status.consecutive_wrong_count if tracker_applied else 0
                )
                evaluation_needs_optimization = (
                    tracker_status.threshold_reached if tracker_applied else False
                )

                # Persist evaluation and co-commit the feedback event in the same
                # transaction (transactional outbox) so a crash cannot persist the
                # SignalPerformance row without its downstream event.
                def _enqueue_feedback_event(
                    perf_id: int,
                    session: Session,
                    *,
                    evaluation: SignalEvaluation = evaluation,
                    consecutive_wrong_count: int = evaluation_consecutive_wrong,
                    needs_optimization: bool = evaluation_needs_optimization,
                    run_id: str | None = signal.get("run_id"),
                ) -> None:
                    message = self._build_feedback_event_message(
                        evaluation=evaluation,
                        consecutive_wrong_count=consecutive_wrong_count,
                        needs_optimization=needs_optimization,
                        run_id=run_id,
                        performance_id=perf_id,
                    )
                    if message is not None and self._outbox_store is not None:
                        self._outbox_store.enqueue_on_session(session, **message)

                _performance_id, inserted = self.evaluator.persist_evaluation(
                    evaluation=evaluation,
                    consecutive_wrong_count=evaluation_consecutive_wrong,
                    needs_optimization=evaluation_needs_optimization,
                    run_id=signal.get("run_id"),
                    did_execute=bool(execution_lineage["fill_ids"]),
                    on_persisted=_enqueue_feedback_event,
                    meta=price_meta,
                )
                if not inserted:
                    logger.info(
                        "Signal %s horizon %s was already evaluated; replay is a no-op",
                        signal["signal_id"],
                        horizon.value,
                    )
                    continue

                results["signals_evaluated"] += 1
                if evaluation.is_correct:
                    results["correct_predictions"] += 1
                else:
                    results["wrong_predictions"] += 1

                # Generate optimization if threshold reached
                if (
                    tracker_applied
                    and tracker_status.threshold_reached
                    and not self._has_pending_optimization(
                        signal["strategy_id"],
                        signal["instr_id"],
                        horizon.value,
                    )
                ):
                    self._generate_and_persist_optimization(
                        strategy_id=signal["strategy_id"],
                        strat_ver_id=signal["strat_ver_id"],
                        instr_id=signal["instr_id"],
                        horizon=horizon,
                        consecutive_wrong=tracker_status.consecutive_wrong_count,
                    )
                    results["optimizations_triggered"] += 1

            except (
                ArithmeticError,
                KeyError,
                SQLAlchemyError,
                SuggestionGenerationError,
                TypeError,
                ValueError,
            ):
                # Known per-signal data, persistence, and suggestion failures
                # do not prevent independent signals from being evaluated.
                # Unexpected programming faults propagate and fail the run.
                logger.exception("Error evaluating signal %s", signal["signal_id"])
                results["errors"] += 1

        logger.info("Evaluation cycle complete: %s", results)
        self._record_cycle_heartbeat(horizon=horizon, results=results)
        return results

    @contextmanager
    def _evaluation_cycle_lease(
        self,
        horizon: EvaluationHorizon,
    ) -> Iterator[None]:
        """Serialize a complete evaluation cycle per PostgreSQL horizon.

        The tracker update and ``SignalPerformance`` insert intentionally use
        separate short transactions for crash recovery. Without a wider fence,
        a stale concurrent worker can replay an older signal after another
        worker advanced the tracker, incrementing or resetting the sequence
        even though the evaluation insert later loses its natural-key race.

        A transaction-scoped advisory lock avoids a new coordinator service and
        is automatically released when the connection dies. SQLite remains
        unfenced for deterministic single-process unit tests.
        """

        if self._engine.dialect.name != "postgresql":
            yield
            return

        with Session(self._engine) as lease_session:
            lease_session.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtext('vynmatrix.feedback.evaluation-cycle'), "
                    "hashtext(:horizon))"
                ),
                {"horizon": horizon.value},
            )
            logger.debug("Acquired feedback evaluation lease for %s", horizon.value)
            try:
                yield
            finally:
                # The lease transaction owns no application writes. Rolling it
                # back releases the advisory lock on both success and failure.
                lease_session.rollback()

    def update_mode_performance(self, *, lookback_days: int = 90) -> int:
        """Recompute ``mode_performance`` from executed outcomes (FB-5).

        Returns the number of (account, strategy, instrument, mode, horizon)
        rows upserted. Persistence or lineage-integrity failures propagate so
        the scheduled runner can mark its aggregate heartbeat degraded instead
        of reporting a successful zero-row refresh.
        """
        return self._mode_performance_writer.update(lookback_days=lookback_days)

    def _record_cycle_heartbeat(
        self, *, horizon: EvaluationHorizon, results: dict[str, Any]
    ) -> None:
        """Record a DB liveness heartbeat after a completed cycle.

        Best-effort: a heartbeat-write failure must not fail the cycle (the
        evaluation already committed its work).
        """
        try:
            errors = int(results.get("errors", 0))
            self._heartbeat_store.record(
                service_name="feedback_loop_engine",
                status="ok" if errors == 0 else "degraded",
                detail=(
                    f"horizon={horizon.value} "
                    f"evaluated={results.get('signals_evaluated', 0)} "
                    f"errors={errors}"
                ),
                preserve_degraded=True,
            )
        except SQLAlchemyError:
            logger.exception("Failed to record feedback heartbeat")

    def record_run_heartbeat(
        self,
        *,
        horizons: list[EvaluationHorizon],
        results: dict[str, int],
    ) -> None:
        """Persist the aggregate result after a multi-horizon scheduled run."""

        try:
            errors = int(results.get("errors", 0))
            self._heartbeat_store.record(
                service_name="feedback_loop_engine",
                status="ok" if errors == 0 else "degraded",
                detail=(
                    f"horizons={','.join(horizon.value for horizon in horizons)} "
                    f"evaluated={results.get('signals_evaluated', 0)} "
                    f"errors={errors}"
                ),
            )
        except SQLAlchemyError:
            logger.exception("Failed to record aggregate feedback heartbeat")

    def _build_feedback_event_message(
        self,
        *,
        evaluation: SignalEvaluation,
        consecutive_wrong_count: int,
        needs_optimization: bool,
        run_id: str | None,
        performance_id: int,
    ) -> dict[str, Any] | None:
        """Build the outbox enqueue kwargs for the FeedbackEvaluationEvent.

        Returns None when the outbox is unwired. Suitable for both
        ``OutboxStore.enqueue`` and ``enqueue_on_session`` (the latter co-commits
        it with the SignalPerformance row).
        """
        if self._outbox_store is None:
            return None
        event = FeedbackEvaluationEvent(
            run_id=run_id,
            correlation_id=str(evaluation.signal_id),
            # The evaluated canonical signal is the cause of this feedback event.
            causation_id=str(evaluation.signal_id),
            producer="feedback_loop_engine",
            signal_id=evaluation.signal_id,
            strategy_id=evaluation.strategy_id,
            symbol=evaluation.symbol,
            evaluation_horizon=evaluation.evaluation_horizon.value,
            is_correct=evaluation.is_correct,
            price_change_pct=float(evaluation.price_change_pct),
            pnl_pct=float(evaluation.pnl_pct),
            consecutive_wrong_count=consecutive_wrong_count,
            needs_optimization=needs_optimization,
            evaluated_at=evaluation.evaluation_ts,
        )
        return {
            "topic": event.topic,
            "event_type": event.event_type,
            "payload": event.model_dump(mode="json"),
            "schema_version": event.schema_version,
            "aggregate_type": "feedback_evaluation",
            "aggregate_id": str(evaluation.signal_id),
            "event_key": f"feedback-evaluation:{evaluation.signal_id}:{event.evaluation_horizon}",
            "ordering_key": evaluation.strategy_id,
            "headers": {"run_id": run_id or "", "performance_id": str(performance_id)},
        }

    def _get_price_observations_for_evaluation(
        self,
        canonical_signal_id: int,
        instr_id: int,
        signal_ts: datetime,
        horizon: EvaluationHorizon,
        entry_price: float | None,
        *,
        price_source: str | None = None,
        price_timeframe: str | None = None,
        signal_metadata: dict[str, Any] | None = None,
    ) -> tuple[PriceObservation | None, PriceObservation | None]:
        """Get provenance-bearing entry and exit observations.

        Uses the configured PriceProvider to fetch data. If the provider
        cannot supply data, returns (None, None) to skip evaluation.

        Args:
            canonical_signal_id: Database identity of the exact signal under evaluation
            instr_id: Instrument ID
            signal_ts: Signal timestamp
            horizon: Evaluation horizon
            entry_price: Entry price stored on the signal (may be None)
            price_source: Preferred price source from signal metadata
            price_timeframe: Preferred price timeframe from signal metadata
        """
        provider: PriceProvider = getattr(self.evaluator, "price_provider", NullPriceProvider())
        entry_observation: PriceObservation | None

        if (signal_metadata or {}).get("price_store") == "equity_observations":
            metadata = signal_metadata or {}
            entry_observation = provider.get_equity_entry_observation(
                instr_id=instr_id,
                as_of=signal_ts,
                metadata=metadata,
                canonical_signal_id=canonical_signal_id,
            )
            exit_observation = provider.get_equity_exit_observation(
                instr_id=instr_id,
                as_of=signal_ts,
                horizon=horizon.value,
                metadata=metadata,
                canonical_signal_id=canonical_signal_id,
            )
            return entry_observation, exit_observation

        # Prefer entry_price stored on the signal; fall back to provider
        if entry_price is not None:
            entry_observation = PriceObservation(
                price_id=None,
                price=float(entry_price),
                bar_open_ts=None,
                bar_close_ts=None,
                source=price_source,
                timeframe=price_timeframe,
                origin=PriceObservationOrigin.CANONICAL_SIGNAL,
            )
        else:
            entry_observation = provider.get_entry_observation(
                instr_id=instr_id,
                as_of=signal_ts,
                price_source=price_source,
                price_timeframe=price_timeframe,
            )

        exit_observation = provider.get_exit_observation(
            instr_id=instr_id,
            as_of=signal_ts,
            horizon=horizon.value,
            price_source=price_source,
            price_timeframe=price_timeframe,
        )

        return entry_observation, exit_observation

    def _get_symbol(self, instr_id: int) -> str:
        """Get symbol name for an instrument ID (cached to avoid N+1 queries)."""
        cached = self._symbol_cache.get(instr_id)
        if cached is not None:
            return cached

        from lib_application.db.models import Instrument  # noqa: PLC0415

        with Session(self._engine) as session:
            instr = session.get(Instrument, instr_id)
            symbol = instr.canonical if instr else f"INSTR_{instr_id}"
            self._symbol_cache[instr_id] = symbol
            return symbol

    def _prefetch_symbols(self, instr_ids: list[int]) -> None:
        """Bulk-load symbols for a batch of instrument IDs into cache."""
        missing = [iid for iid in instr_ids if iid not in self._symbol_cache]
        if not missing:
            return

        from lib_application.db.models import Instrument  # noqa: PLC0415

        with Session(self._engine) as session:
            rows = (
                session.query(Instrument.instr_id, Instrument.canonical)
                .filter(Instrument.instr_id.in_(missing))
                .all()
            )
            for iid, canonical in rows:
                self._symbol_cache[iid] = canonical
            # Fill fallback for any IDs not found
            for iid in missing:
                if iid not in self._symbol_cache:
                    self._symbol_cache[iid] = f"INSTR_{iid}"

    def _update_consecutive_tracker(
        self,
        strategy_id: str,
        strat_ver_id: int | None,
        instr_id: int,
        signal_id: int,
        signal_ts: datetime,
        is_correct: bool,
        horizon: str = "1d",
    ) -> ConsecutiveWrongStatus:
        """Update the consecutive wrong prediction tracker.

        If prediction was correct, reset counter.
        If wrong, increment counter and check threshold.

        Sprint F (Plan §6.9): the persistence work moved into
        :class:`ISignalPerformanceRepository`. The engine wraps the
        port's ``ConsecutiveWrongTracker`` value object into its own
        ``ConsecutiveWrongStatus`` (which adds the canonical instrument
        symbol from a separate cache).
        """
        tracker = self._signal_performance_repo.update_consecutive_wrong_tracker(
            strategy_id=strategy_id,
            strat_ver_id=strat_ver_id,
            instr_id=instr_id,
            signal_id=signal_id,
            signal_ts=signal_ts,
            is_correct=is_correct,
            wrong_threshold=self.wrong_threshold,
            horizon=horizon,
        )
        return ConsecutiveWrongStatus(
            strategy_id=tracker.strategy_id,
            strat_ver_id=tracker.strat_ver_id,
            instr_id=tracker.instr_id,
            symbol=self._get_symbol(tracker.instr_id),
            horizon=EvaluationHorizon(tracker.horizon),
            consecutive_wrong_count=tracker.consecutive_wrong_count,
            wrong_threshold=tracker.wrong_threshold,
            threshold_reached=tracker.threshold_reached,
            threshold_reached_at=tracker.threshold_reached_at,
            feedback_id=tracker.feedback_id,
            last_signal_id=tracker.last_signal_id,
            last_signal_ts=tracker.last_signal_ts,
            last_evaluation_ts=tracker.last_evaluation_ts,
        )

    def _has_pending_optimization(
        self,
        strategy_id: str,
        instr_id: int | None = None,
        horizon: str | None = None,
    ) -> bool:
        """Check if a pending optimization already exists for this strategy+instrument."""
        return self._signal_performance_repo.has_pending_optimization(
            strategy_id,
            instr_id,
            horizon,
        )

    def _signal_execution_lineage(self, signal_id: int) -> dict[str, Any]:
        """Return exact canonical order/fill lineage for one signal.

        ``run_id`` is deliberately excluded: a single ingestion run can contain
        several signals and each signal can fan out to several user accounts.
        """
        from lib_application.db.models import (  # noqa: PLC0415
            Execution,
            ExecutionDecisionLog,
            Order,
            OrderIntent,
        )

        with Session(self._engine) as session:
            decision_rows = session.execute(
                select(
                    ExecutionDecisionLog.decision_id,
                    ExecutionDecisionLog.user_id,
                    ExecutionDecisionLog.broker_account_id,
                    ExecutionDecisionLog.binding_id,
                    ExecutionDecisionLog.should_execute,
                    ExecutionDecisionLog.status,
                )
                .where(ExecutionDecisionLog.canonical_signal_id == signal_id)
                .order_by(
                    ExecutionDecisionLog.broker_account_id,
                    ExecutionDecisionLog.decision_id,
                )
            ).all()
            intent_rows = session.execute(
                select(
                    OrderIntent.intent_id,
                    OrderIntent.account_id,
                    OrderIntent.user_id,
                )
                .where(OrderIntent.canonical_signal_id == signal_id)
                .order_by(OrderIntent.account_id, OrderIntent.intent_id)
            ).all()
            order_rows = session.execute(
                select(Order.order_id, Order.intent_id, Order.account_id)
                .join(OrderIntent, OrderIntent.intent_id == Order.intent_id)
                .where(OrderIntent.canonical_signal_id == signal_id)
                .order_by(Order.account_id, Order.order_id)
            ).all()
            fill_rows = session.execute(
                select(Execution.exec_id, Execution.order_id)
                .join(Order, Order.order_id == Execution.order_id)
                .join(OrderIntent, OrderIntent.intent_id == Order.intent_id)
                .where(OrderIntent.canonical_signal_id == signal_id)
                .order_by(Order.account_id, Order.order_id, Execution.exec_id)
            ).all()

        decision_accounts = {
            int(row.broker_account_id) for row in decision_rows if row.broker_account_id is not None
        }
        intent_accounts = {int(row.account_id) for row in intent_rows}
        return {
            "canonical_signal_id": signal_id,
            "decision_ids": [int(row.decision_id) for row in decision_rows],
            "decisions": [
                {
                    "decision_id": int(row.decision_id),
                    "user_id": str(row.user_id),
                    "broker_account_id": (
                        int(row.broker_account_id) if row.broker_account_id is not None else None
                    ),
                    "binding_id": int(row.binding_id) if row.binding_id is not None else None,
                    "should_execute": row.should_execute,
                    "status": str(row.status),
                }
                for row in decision_rows
            ],
            "account_ids": sorted(decision_accounts | intent_accounts),
            "user_ids": sorted(
                {str(row.user_id) for row in decision_rows}
                | {str(row.user_id) for row in intent_rows}
            ),
            "intent_ids": [int(row.intent_id) for row in intent_rows],
            "order_ids": [int(row.order_id) for row in order_rows],
            "fill_ids": [int(row.exec_id) for row in fill_rows],
        }

    def _generate_and_persist_optimization(
        self,
        strategy_id: str,
        strat_ver_id: int | None,
        instr_id: int,
        horizon: EvaluationHorizon,
        consecutive_wrong: int,
    ) -> int:
        """Generate, persist, and link one actionable optimization suggestion.

        Any failure propagates to the cycle boundary. The tracker was committed
        before this method runs, so a later cycle can retry it without losing
        the diagnostic or creating duplicate pending work.
        """
        # Get strategy code
        strategy_code = self._get_strategy_code(strategy_id)

        # Get performance stats
        performance_stats = self._get_strategy_performance_stats(
            strategy_id=strategy_id,
            instr_id=instr_id,
        )

        # Generate suggestion
        if strat_ver_id is None:
            message = (
                "Cannot generate optimization suggestion without exact strategy version: "
                f"{strategy_id}"
            )
            raise SuggestionGenerationError(message)
        suggestion = self.optimizer.generate_suggestion(
            strategy_id=strategy_id,
            strategy_code=strategy_code,
            strat_ver_id=strat_ver_id,
            instr_id=instr_id,
            symbol=self._get_symbol(instr_id),
            trigger_reason=TriggerReason.CONSECUTIVE_WRONG,
            horizon=horizon,
            consecutive_wrong=consecutive_wrong,
            performance_stats=performance_stats,
        )

        # Persist
        feedback_id = self.optimizer.persist_suggestion(suggestion)
        if feedback_id is None:
            message = (
                "Optimization suggestion was not persisted for eligible exact version "
                f"{strategy_id}/{strat_ver_id}"
            )
            raise SuggestionGenerationError(message)

        logger.info(
            "Generated optimization suggestion %d for strategy %s",
            feedback_id,
            strategy_code,
        )

        # Update tracker with feedback ID
        if not self._link_feedback_to_tracker(
            strategy_id,
            instr_id,
            horizon.value,
            feedback_id,
        ):
            message = (
                f"Suggestion {feedback_id} persisted but triggering tracker was not linkable "
                f"for {strategy_id}/{instr_id}/{horizon.value}"
            )
            raise SuggestionGenerationError(message)

        return feedback_id

    def _get_strategy_code(self, strategy_id: str) -> str:
        """Get strategy code from database."""
        from lib_application.db.models import Strategy  # noqa: PLC0415

        with Session(self._engine) as session:
            strategy = session.get(Strategy, strategy_id)
            return strategy.strategy_id if strategy else f"strategy_{strategy_id}"

    def _get_strategy_performance_stats(
        self,
        strategy_id: str,
        instr_id: int | None = None,
        _days: int = 30,
    ) -> StrategyPerformanceStats | None:
        """Get aggregated performance statistics for a strategy."""
        from lib_application.db.models import (  # noqa: PLC0415
            SignalPerformance,
            Strategy,
        )

        with Session(self._engine) as session:
            strategy = session.get(Strategy, strategy_id)
            if not strategy:
                return None

            # Build query
            stmt = select(SignalPerformance).where(SignalPerformance.strategy_id == strategy_id)
            if instr_id:
                stmt = stmt.where(SignalPerformance.instr_id == instr_id)

            rows = session.execute(stmt).scalars().all()

            if not rows:
                return None

            total = len(rows)
            correct = sum(1 for r in rows if r.is_correct)
            wrong = total - correct

            # Recent stats (last N signals)
            recent_count = min(10, total)
            recent_rows = sorted(rows, key=lambda r: r.signal_ts, reverse=True)[:recent_count]
            recent_correct = sum(1 for r in recent_rows if r.is_correct)

            # Consecutive tracking
            current_consecutive = 0
            max_consecutive = 0
            temp_consecutive = 0
            for r in sorted(rows, key=lambda r: r.signal_ts):
                if not r.is_correct:
                    temp_consecutive += 1
                    max_consecutive = max(max_consecutive, temp_consecutive)
                else:
                    temp_consecutive = 0
            current_consecutive = temp_consecutive

            return StrategyPerformanceStats(
                strategy_id=strategy_id,
                strategy_code=strategy.strategy_id,
                strat_ver_id=None,
                instr_id=instr_id,
                symbol=self._get_symbol(instr_id) if instr_id else None,
                total_signals=total,
                correct_predictions=correct,
                wrong_predictions=wrong,
                accuracy_pct=correct / total if total > 0 else 0.0,
                recent_signals=recent_count,
                recent_correct=recent_correct,
                recent_accuracy_pct=recent_correct / recent_count if recent_count > 0 else 0.0,
                current_consecutive_wrong=current_consecutive,
                max_consecutive_wrong=max_consecutive,
                period_start=min(r.signal_ts for r in rows),
                period_end=max(r.signal_ts for r in rows),
            )

    def _link_feedback_to_tracker(
        self,
        strategy_id: str,
        instr_id: int,
        horizon: str,
        feedback_id: int,
    ) -> bool:
        """Link feedback ID to the consecutive wrong tracker.

        Sprint F (Plan §6.9): delegates to
        :meth:`ISignalPerformanceRepository.link_feedback_to_tracker`.
        """
        return self._signal_performance_repo.link_feedback_to_tracker(
            strategy_id=strategy_id,
            instr_id=instr_id,
            horizon=horizon,
            feedback_id=feedback_id,
        )

    def _reconcile_unlinked_trackers(self, *, limit: int = 100) -> tuple[int, int]:
        """Repair the crash window between suggestion persistence and linkage.

        Reached trackers are durable before suggestion generation. Every cycle,
        including an otherwise empty cycle, therefore retries unlinked work.
        Existing exact pending suggestions are linked first; only a genuinely
        missing suggestion is generated.
        """

        generated = 0
        errors = 0
        trackers = self._signal_performance_repo.list_unlinked_reached_trackers(limit=limit)
        for tracker in trackers:
            try:
                has_pending = self._has_pending_optimization(
                    tracker.strategy_id,
                    tracker.instr_id,
                    tracker.horizon,
                )
                pending: list[PendingSuggestion] = []
                if has_pending:
                    pending = list(
                        self._signal_performance_repo.list_pending_suggestions(tracker.strategy_id)
                    )
                existing = next(
                    (
                        suggestion
                        for suggestion in pending
                        if suggestion.strat_ver_id == tracker.strat_ver_id
                        and suggestion.instr_id == tracker.instr_id
                        and suggestion.horizon == tracker.horizon
                    ),
                    None,
                )
                if existing is not None:
                    if not self._link_feedback_to_tracker(
                        tracker.strategy_id,
                        tracker.instr_id,
                        tracker.horizon,
                        existing.feedback_id,
                    ):
                        logger.error(
                            "Existing pending suggestion could not be linked to tracker %s/%s/%s",
                            tracker.strategy_id,
                            tracker.instr_id,
                            tracker.horizon,
                        )
                        errors += 1
                    continue

                if has_pending:
                    logger.error(
                        "Pending suggestion scope conflicts with unlinked tracker %s/%s/%s",
                        tracker.strategy_id,
                        tracker.instr_id,
                        tracker.horizon,
                    )
                    errors += 1
                    continue

                self._generate_and_persist_optimization(
                    strategy_id=tracker.strategy_id,
                    strat_ver_id=tracker.strat_ver_id,
                    instr_id=tracker.instr_id,
                    horizon=EvaluationHorizon(tracker.horizon),
                    consecutive_wrong=tracker.consecutive_wrong_count,
                )
                generated += 1
            except (SuggestionGenerationError, SQLAlchemyError, ValueError):
                logger.exception(
                    "Failed to reconcile feedback tracker %s/%s/%s",
                    tracker.strategy_id,
                    tracker.instr_id,
                    tracker.horizon,
                )
                errors += 1
        return generated, errors
