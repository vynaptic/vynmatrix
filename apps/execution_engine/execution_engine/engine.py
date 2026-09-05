"""
Execution engine: orchestrates signal handling, order building, and broker routing.

Responsibilities:
- Receive signals from strategy runners
- Look up user configuration and account state
- Build order intents using OrderBuilder
- Route orders to appropriate broker adapters
- Track execution results and emit events
"""

import asyncio
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from lib_common.alerting import AlertSink
from lib_common.config_validation import (
    ExecutionFreshnessConfig,
    ExecutionRuntimeConfig,
)
from lib_common.logging import get_logger
from lib_common.metrics import counter
from lib_strategy.signals.signal import Signal

from ._dispatch import (
    DispatchOutcome,
    DispatchResolutionError,
    DispatchResolver,
    ResolvedDispatch,
    SignalDispatchContext,
    build_dispatch_context,
    claim_dispatch_execution,
    explicit_profile_account_id,
    record_unexpected_dispatch_failure,
)
from ._execute import execute_resolved_signal
from ._services import (
    ExecutionLogStorePort,
    ExecutionMetricsStorePort,
    ExecutionPositionStorePort,
    ExecutionServices,
    MarketDataProviderPort,
    OutboxStorePort,
    SessionFactory,
)
from .account_execution_serializer import AccountExecutionSerializer, AccountWriterKind
from .alerts import AlertPublisher, ExecutionAlert
from .broker_resolver import BrokerResolver
from .brokers.base import BrokerAdapter, BrokerOrderResult
from .canonical_execution_store import CanonicalExecutionStore
from .circuit_breaker import CircuitBreaker
from .circuit_breaker_manager import CircuitBreakerManager
from .config import AccountState, BrokerType, ExecutionMode
from .controls import AuditLogStore, TradingHaltState, TradingHaltStore
from .deduplication import ExecutionDeduplicator
from .execution_gates import ExecutionGatekeeper
from .execution_persistence import ExecutionPersistence
from .execution_result import ExecutionResult, is_retryable_failure
from .execution_routing import ExecutionRouteResolver
from .market_sessions import SqlMarketSessionProvider
from .metrics.fx_rates import (
    CachedFXRateProvider,
    FXRateProvider,
    SqlFXRateProvider,
)
from .metrics.pnl_service import PnLService
from .models import (
    CloseQuantityOverride,
    OptionsIntent,
    OrderIntent,
    TargetPositionQuantityOverride,
)
from .order_builder import OrderBuilder
from .order_executor import OrderExecutor
from .paper_ledger_recovery import recover_paper_ledger
from .pending_orders import PendingOrderRepository
from .reconciliation_tracker import ReconciliationTracker
from .risk_breach_store import RiskBreachStore
from .risk_guard import RiskGuard
from .state_provider import StateProvider

logger = get_logger(__name__)

_EXECUTION_RESULTS_TOTAL = counter(
    "vm_execution_results_total",
    "Execution engine results by mode, broker, and outcome",
    ("execution_mode", "broker", "outcome"),
)


class ExecutionEngine:
    """
    Orchestrates signal handling: profile lookup, sizing, order building, broker routing.

    This is the main entry point for signal execution. It:
    1. Receives canonical signals from indicator strategy runners
    2. Looks up user configuration and broker preferences
    3. Fetches current account state from the broker
    4. Builds order intents using the OrderBuilder
    5. Submits orders to the broker adapter
    6. Returns execution results

    Example:
        engine = ExecutionEngine()
        result = await engine.handle_signal(
            user_id="user123",
            profile={"broker": "paper"},
            user_strategy_config={"execution_mode": "spot", "position_size_pct": 0.02},
            signal=signal,
        )
    """

    def __init__(
        self,
        order_builder: OrderBuilder | None = None,
        market_data_provider: MarketDataProviderPort | None = None,
        execution_log_store: ExecutionLogStorePort | None = None,
        execution_metrics_store: ExecutionMetricsStorePort | None = None,
        execution_position_store: ExecutionPositionStorePort | None = None,
        risk_breach_store: RiskBreachStore | None = None,
        outbox_store: OutboxStorePort | None = None,
        canonical_execution_store: CanonicalExecutionStore | None = None,
        fx_rate_provider: FXRateProvider | None = None,
        default_mode: str = "paper",
        allow_live: bool = False,
        session_factory: SessionFactory | None = None,
        freshness: ExecutionFreshnessConfig | None = None,
        runtime_config: ExecutionRuntimeConfig | None = None,
        alert_sinks: list[AlertSink] | None = None,
    ) -> None:
        """
        Initialize execution engine.

        Args:
            order_builder: OrderBuilder instance (created if not provided)
            market_data_provider: Provider for market data (prices, IV, etc.)
            session_factory: SQLAlchemy session factory for DB-backed deduplication
        """
        runtime = runtime_config or ExecutionRuntimeConfig()
        self._runtime_config = runtime
        self._paper_order_lifecycle_required = runtime.paper.use_local_broker
        self._paper_order_lifecycle_initial_complete = not self._paper_order_lifecycle_required
        self._paper_order_lifecycle_healthy = not self._paper_order_lifecycle_required
        self._paper_order_lifecycle_last_error: str | None = None
        self._paper_order_lifecycle_last_completed_at: datetime | None = None
        self._paper_order_lifecycle_failed_orders = 0
        self.order_builder = order_builder or OrderBuilder()
        self._market_data_provider = market_data_provider
        self._session_factory = session_factory
        self._account_execution_serializer = AccountExecutionSerializer(session_factory)
        if fx_rate_provider is None and session_factory is not None:
            fx_max_age = timedelta(seconds=runtime.fx_max_age_seconds)
            fx_rate_provider = CachedFXRateProvider(
                SqlFXRateProvider(session_factory, max_age=fx_max_age),
                max_observation_age=fx_max_age,
            )
        self._fx_rate_provider = fx_rate_provider
        self._route_resolver = ExecutionRouteResolver(
            default_mode=default_mode,
            session_factory=session_factory,
            canonical_execution_store=canonical_execution_store,
        )
        self._broker_resolver = BrokerResolver(
            ledger_seeder=partial(
                recover_paper_ledger,
                session_factory,
                execution_position_store,
            ),
            fx_rate_provider=fx_rate_provider,
            use_local_paper_broker=runtime.paper.use_local_broker,
            paper_slippage_pct=runtime.paper.slippage_pct,
            paper_commission_pct=runtime.paper.commission_pct,
            paper_fill_committer=(
                canonical_execution_store.apply_broker_result
                if canonical_execution_store is not None
                else None
            ),
        )
        self._execution_log_store = execution_log_store
        self._state_provider = StateProvider(
            execution_log_store=execution_log_store,
            market_data_provider=market_data_provider,
        )
        self._execution_metrics_store = execution_metrics_store
        self._execution_position_store = execution_position_store
        self._outbox_store = outbox_store
        self._default_mode = default_mode
        if allow_live and default_mode != "live":
            # Startup footgun surfaced in D1: ALLOW_LIVE=true with a paper
            # engine mode means any live-environment route is BLOCKED
            # fail-closed (never executed live, never silently paper-traded).
            logger.warning(
                "EXECUTION_ENGINE_ALLOW_LIVE=true while the engine execution mode is "
                "'%s' (EXECUTION_MODE != live). Live-environment routes will be "
                "BLOCKED fail-closed. Set EXECUTION_MODE=live to enable live "
                "submission, or EXECUTION_ENGINE_ALLOW_LIVE=false to clear this warning.",
                default_mode,
            )
        self._deduplicator = ExecutionDeduplicator(
            session_factory=session_factory,
            ttl_seconds=runtime.dedup.ttl_seconds,
            allow_stale_executing_reclaim=runtime.dedup.allow_stale_executing_reclaim,
            use_memory_only=runtime.dedup.use_memory_only,
        )
        self._trading_halt_store = TradingHaltStore(session_factory=session_factory)
        self._audit_log_store = AuditLogStore(session_factory=session_factory)
        self._pending_order_repo = PendingOrderRepository(session_factory)
        self._risk_guard = RiskGuard(
            session_factory=session_factory,
            breach_store=risk_breach_store,
            # Q2 shorting dim 4: asset classes eligible to be shorted. Empty
            # (default) imposes no asset-class restriction — the spot-only soak is
            # unaffected; populate it (e.g. "crypto") to restrict shorting to
            # specific asset classes once non-spot modes are enabled.
            short_eligible_asset_classes=runtime.short_eligible_asset_classes,
        )
        self._risk_guard_enabled = runtime.risk_guard_enabled
        self._pnl_service = PnLService(
            session_factory=session_factory,
            market_data_provider=market_data_provider,
            fx_rate_provider=fx_rate_provider,
        )
        self._persistence = ExecutionPersistence(
            execution_log_store=execution_log_store,
            execution_metrics_store=execution_metrics_store,
            execution_position_store=execution_position_store,
            outbox_store=outbox_store,
            pnl_service=self._pnl_service,
            session_factory=session_factory,
            environment_resolver=self._route_resolver.resolve_environment,
        )
        circuit_breaker = CircuitBreaker(
            threshold=runtime.circuit_breaker.threshold,
            window_sec=runtime.circuit_breaker.window_seconds,
            cooldown_sec=runtime.circuit_breaker.cooldown_seconds,
        )
        self._alerts = AlertPublisher(
            enabled=runtime.alerts_enabled,
            webhook_url=None,
            alert_environments=set(runtime.alert_environments),
            sinks=alert_sinks,
        )
        self._breaker_manager = CircuitBreakerManager(
            circuit_breaker=circuit_breaker,
            alerts=self._alerts,
            risk_breach_store=risk_breach_store,
        )
        freshness_config = freshness or ExecutionFreshnessConfig()
        market_session_provider = (
            SqlMarketSessionProvider(
                session_factory,
                max_observation_age=timedelta(
                    seconds=freshness_config.max_market_session_age_seconds
                ),
            )
            if session_factory is not None
            else None
        )
        self._reconciliation_tracker = ReconciliationTracker(
            pending_order_repo=self._pending_order_repo,
            canonical_execution_store=canonical_execution_store,
            session_factory=session_factory,
        )
        self._gatekeeper = ExecutionGatekeeper(
            allow_live=allow_live,
            risk_guard_enabled=self._risk_guard_enabled,
            freshness=freshness_config,
            require_market_data_for_live=runtime.require_market_data_for_live,
            sandbox_certification_marker=runtime.sandbox_certification_marker,
            trading_halt_store=self._trading_halt_store,
            reconciliation_tracker=self._reconciliation_tracker,
            market_session_provider=market_session_provider,
        )
        self._dispatch_resolver = DispatchResolver(
            default_mode=default_mode,
            route_resolver=self._route_resolver,
            broker_resolver=self._broker_resolver,
            gatekeeper=self._gatekeeper,
            breaker_manager=self._breaker_manager,
        )
        self._order_executor = OrderExecutor(
            tracker=self._reconciliation_tracker,
            canonical_execution_store=canonical_execution_store,
            account_serializer=self._account_execution_serializer,
        )
        self._breaker_manager.restore_all()

    # ------------------------------------------------------------------
    # Reconciliation delegates — see ``ReconciliationTracker``. Method
    # names preserved for tests, the FastAPI app state, and the external
    # reconciliation worker.
    # ------------------------------------------------------------------

    @property
    def account_execution_serializer(self) -> AccountExecutionSerializer:
        """Return the one serializer shared by HTTP and background writers."""
        return self._account_execution_serializer

    def iter_reconciliation_contexts(self) -> list[dict[str, Any]]:
        return self._reconciliation_tracker.iter_contexts()

    def pending_orders(self) -> dict[str, dict[str, Any]]:
        return self._reconciliation_tracker.list_pending_orders()

    def pending_orders_for(self, breaker_key: str) -> dict[str, dict[str, Any]]:
        return self._reconciliation_tracker.pending_orders_for(breaker_key)

    def pending_orders_for_account(
        self,
        broker_account_id: int,
    ) -> dict[str, dict[str, Any]]:
        return self._reconciliation_tracker.pending_orders_for_account(broker_account_id)

    def _restore_pending_orders(self) -> None:
        self._reconciliation_tracker.restore_pending_orders()

    def _load_pending_orders_from_db(
        self,
        *,
        breaker_key: str | None = None,
        statuses: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        return self._reconciliation_tracker.load_pending_orders_from_db(
            breaker_key=breaker_key, statuses=statuses
        )

    def mark_reconciliation_health(
        self,
        *,
        healthy: bool,
        error: str | None = None,
    ) -> None:
        self._reconciliation_tracker.mark_health(healthy=healthy, error=error)

    def is_reconciliation_healthy(self) -> bool:
        return self._reconciliation_tracker.is_healthy()

    def is_paper_rehydration_healthy(self) -> bool:
        return self._broker_resolver.is_paper_rehydration_healthy()

    def reconciliation_status(self) -> dict[str, Any]:
        return self._reconciliation_tracker.status()

    def mark_paper_order_lifecycle_health(
        self,
        *,
        healthy: bool,
        error: str | None = None,
        failed_orders: int | None = None,
    ) -> None:
        """Record local-paper lifecycle progress for the service readiness gate."""
        self._paper_order_lifecycle_initial_complete = True
        self._paper_order_lifecycle_healthy = healthy
        self._paper_order_lifecycle_last_error = error
        self._paper_order_lifecycle_last_completed_at = datetime.now(tz=UTC)
        if failed_orders is not None:
            self._paper_order_lifecycle_failed_orders = max(0, int(failed_orders))

    def is_paper_order_lifecycle_healthy(self) -> bool:
        """Return whether the required committed-candle worker is progressing."""
        if not self._paper_order_lifecycle_required:
            return True
        return self._paper_order_lifecycle_initial_complete and self._paper_order_lifecycle_healthy

    def paper_order_lifecycle_status(self) -> dict[str, Any]:
        """Expose bounded lifecycle state without using process liveness as progress."""
        return {
            "required": self._paper_order_lifecycle_required,
            "healthy": self.is_paper_order_lifecycle_healthy(),
            "initial_pass_complete": self._paper_order_lifecycle_initial_complete,
            "failed_orders": self._paper_order_lifecycle_failed_orders,
            "last_completed_at": (
                self._paper_order_lifecycle_last_completed_at.isoformat()
                if self._paper_order_lifecycle_last_completed_at is not None
                else None
            ),
            "last_error": self._paper_order_lifecycle_last_error,
        }

    def emit_alert(
        self,
        *,
        event_type: str,
        severity: str,
        message: str,
        payload: dict[str, Any],
    ) -> None:
        self._alerts.publish(
            ExecutionAlert(
                event_type=event_type,
                severity=severity,
                message=message,
                payload=payload,
            )
        )

    def is_broker_system_error(
        self,
        *,
        error_message: str | None,
        error_code: str | None = None,
    ) -> bool:
        return self._breaker_manager.is_broker_system_error(
            error_message=error_message, error_code=error_code
        )

    async def _update_daily_nav_snapshot(
        self,
        *,
        user_id: str,
        account_state: AccountState | None,
        account_id: int,
    ) -> None:
        await self._persistence.update_daily_nav_snapshot(
            user_id=user_id,
            account_state=account_state,
            account_id=account_id,
        )

    def open_circuit_breaker(
        self,
        *,
        scope: str,
        breaker_key: str,
        user_id: str,
        strategy_id: str,
        broker: str,
        environment: str | None = None,
        reason: str,
        broker_account_id: int | None = None,
    ) -> None:
        self._breaker_manager.open(
            scope=scope,
            breaker_key=breaker_key,
            user_id=user_id,
            strategy_id=strategy_id,
            broker=broker,
            environment=environment,
            reason=reason,
            broker_account_id=broker_account_id,
        )

    def _short_circuit_result(
        self,
        *,
        dedup_key: str,
        user_id: str,
        signal: Signal,
        score_context: dict[str, float] | None,
        profile: dict[str, Any],
        signal_id: str,
        symbol: str,
        execution_mode: str,
        broker: str,
        error_message: str | None,
        success: bool = False,
        reason: str | None = None,
        block_reason: str | None = None,
        account_state: AccountState | None = None,
        intents: list[OrderIntent | OptionsIntent] | None = None,
        broker_account_id: int | None = None,
        settlement_currency: str | None = None,
    ) -> ExecutionResult:
        """Build a no-orders ExecutionResult and finalize it in one step.

        Replaces ~14 near-identical "blocked / short-circuit" branches in
        ``handle_signal`` that each constructed an ``ExecutionResult`` with
        zero orders/quantity and forwarded it to :py:meth:`_finalize_result`.
        Centralising the shape here eliminates ~170 LOC of repetition and
        makes the orchestrator easier to read top-to-bottom.

        ``reason`` tags *why* a successful short-circuit submitted no order
        (no-op) so RCA can tell a deliberate no-op apart from a real fill (EX-2).
        """
        result = ExecutionResult(
            success=success,
            signal_id=signal_id,
            symbol=symbol,
            execution_mode=execution_mode,
            broker=broker,
            orders_submitted=0,
            orders_filled=0,
            total_quantity=0.0,
            average_price=0.0,
            total_commission=0.0,
            broker_account_id=(
                broker_account_id
                if broker_account_id is not None
                else explicit_profile_account_id(profile)
            ),
            settlement_currency=settlement_currency,
            error_message=error_message,
            reason=reason,
            block_reason=block_reason,
        )
        return self._finalize_result(
            dedup_key,
            user_id,
            signal,
            result,
            score_context,
            account_state=account_state,
            profile=profile,
            intents=intents,
        )

    def _finalize_result(
        self,
        dedup_key: str,
        user_id: str,
        signal: Signal,
        result: ExecutionResult,
        score_context: dict[str, float] | None,
        account_state: AccountState | None,
        profile: dict[str, Any] | None = None,
        intents: list[OrderIntent | OptionsIntent] | None = None,
    ) -> ExecutionResult:
        """Persist execution artifacts and mark deduplication state."""
        execution_log_id = self._persist_execution(
            user_id, signal, result, score_context, account_state, profile, intents
        )
        try:
            self._deduplicator.mark_executed(
                dedup_key,
                signal_id=result.signal_id,
                user_id=user_id,
                symbol=signal.symbol,
                action=signal.action.value.lower(),
                result_id=execution_log_id,
                success=result.success,
                retryable=is_retryable_failure(result),
                blocked=not result.success and result.execution_mode == "blocked",
            )
        except (SQLAlchemyError, OSError):
            # DB / connection-level failures only — programming errors propagate.
            # Memory dedup is already authoritative within-process.
            logger.exception("Failed to record execution dedup state", signal_id=result.signal_id)
        if _EXECUTION_RESULTS_TOTAL is not None:
            # Bucket a successful no-op (no order submitted) separately so it does
            # not inflate the real-fill success rate (EX-2).
            if not result.success:
                outcome = "blocked_or_failed"
            elif result.orders_submitted == 0:
                outcome = "no_op"
            else:
                outcome = "success"
            _EXECUTION_RESULTS_TOTAL.labels(
                result.execution_mode,
                result.broker,
                outcome,
            ).inc()
        return result

    # ------------------------------------------------------------------
    # ``handle_signal`` short-circuit gates
    # ------------------------------------------------------------------
    #
    # The engine's ``handle_signal`` orchestrator runs through three
    # successive validation passes before it ever touches a broker. Each
    # pass is extracted into its own helper so the orchestrator reads
    # top-to-bottom as a list of named stages. The helpers all share the
    # same contract: return ``None`` on success (continue the pipeline),
    # or a user-readable error string on failure (the caller short-circuits
    # with a ``blocked`` ``ExecutionResult``).

    def trading_halt_state(self) -> TradingHaltState:
        """Return the current durable global trading halt state."""
        return self._trading_halt_store.get_state()

    def set_trading_halt(
        self,
        *,
        enabled: bool,
        reason: str | None,
        updated_by: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> TradingHaltState:
        """Persist a global trading halt state transition."""
        state = self._trading_halt_store.set_state(
            enabled=enabled,
            reason=reason,
            updated_by=updated_by,
            metadata=metadata,
        )
        self._audit_log_store.record(
            action="trading_halt.set" if enabled else "trading_halt.clear",
            status="ok",
            user_id=None,
            request_payload={
                "enabled": enabled,
                "reason": reason,
                "operator": updated_by,
                "metadata": metadata or {},
            },
            response_payload=state.to_dict(),
        )
        return state

    def _execution_services(self) -> ExecutionServices:
        """Snapshot the collaborators and callbacks the resolved flow uses.

        Built fresh per ``handle_signal`` call (never cached) so collaborators
        replaced after construction and the methods tests patch on a live
        instance (``_get_broker``, ``_get_account_state`` ...) are captured
        exactly as currently bound.
        """
        return ExecutionServices(
            risk_guard=self._risk_guard,
            risk_guard_enabled=self._risk_guard_enabled,
            breaker_manager=self._breaker_manager,
            gatekeeper=self._gatekeeper,
            dispatch_resolver=self._dispatch_resolver,
            broker_resolver=self._broker_resolver,
            route_resolver=self._route_resolver,
            reconciliation_tracker=self._reconciliation_tracker,
            alerts=self._alerts,
            order_builder=self.order_builder,
            session_factory=self._session_factory,
            fx_rate_provider=self._fx_rate_provider,
            pnl_service=self._pnl_service,
            execution_position_store=self._execution_position_store,
            get_broker=self._get_broker,
            get_account_state=self._get_account_state,
            get_market_data=self._get_market_data,
            execute_orders=self._execute_orders,
            aggregate_results=self._aggregate_results,
            short_circuit_result=self._short_circuit_result,
            finalize_result=self._finalize_result,
            update_daily_nav_snapshot=self._update_daily_nav_snapshot,
        )

    async def handle_signal(
        self,
        user_id: str,
        profile: dict[str, Any],
        user_strategy_config: dict[str, Any],
        signal: Signal,
        credentials: dict[str, str] | None = None,
        score_context: dict[str, float] | None = None,
        *,
        allow_historical_replay: bool = False,
        close_quantity_override: CloseQuantityOverride | None = None,
        target_position_override: TargetPositionQuantityOverride | None = None,
        account_writer_kind: AccountWriterKind | None = None,
    ) -> ExecutionResult:
        """Resolve, gate, and execute one canonical signal under its account fence."""
        dispatch = build_dispatch_context(
            user_id=user_id,
            profile=profile,
            user_strategy_config=user_strategy_config,
            signal=signal,
        )
        profile = dispatch.profile
        user_strategy_config = dispatch.user_strategy_config
        signal = dispatch.signal
        trace_ctx = dispatch.trace_ctx
        logger.info(
            "Handling signal",
            action=signal.action.value.lower(),
            **trace_ctx,
        )

        broker_account_id = explicit_profile_account_id(profile)
        if broker_account_id is None:
            raw_account_id = user_strategy_config.get("broker_account_id")
            if (
                isinstance(raw_account_id, bool)
                or not isinstance(raw_account_id, int)
                or raw_account_id <= 0
            ):
                msg = "Execution dedup requires an explicit positive broker_account_id"
                raise ValueError(msg)
            broker_account_id = raw_account_id
        writer_kind: AccountWriterKind = account_writer_kind or (
            "historical_replay" if allow_historical_replay else "ordinary"
        )
        async with self._account_execution_serializer.hold(
            user_id=user_id,
            broker_account_id=broker_account_id,
            writer_kind=writer_kind,
        ):
            return await self._handle_signal_in_account_generation(
                dispatch=dispatch,
                user_id=user_id,
                broker_account_id=broker_account_id,
                credentials=credentials,
                score_context=score_context,
                allow_historical_replay=allow_historical_replay,
                close_quantity_override=close_quantity_override,
                target_position_override=target_position_override,
            )

    async def _handle_signal_in_account_generation(
        self,
        *,
        dispatch: SignalDispatchContext,
        user_id: str,
        broker_account_id: int,
        credentials: dict[str, str] | None,
        score_context: dict[str, float] | None,
        allow_historical_replay: bool,
        close_quantity_override: CloseQuantityOverride | None,
        target_position_override: TargetPositionQuantityOverride | None,
    ) -> ExecutionResult:
        """Execute after the shared account writer generation is authoritative."""
        profile = dispatch.profile
        user_strategy_config = dispatch.user_strategy_config
        signal = dispatch.signal
        signal_id = dispatch.signal_id
        trace_ctx = dispatch.trace_ctx
        # The dedup claim is sync DB I/O — run it as ONE self-contained unit
        # of work on a worker thread so it never blocks the event loop.
        # ContextVar safety: ``ExecutionDeduplicator`` opens, commits, and
        # closes its own sessions via its own session factory (this service
        # has no request-scoped ContextVar session), so the worker thread owns
        # the session end-to-end. Within-process claim atomicity — previously
        # a side effect of event-loop serialization — is provided by the
        # deduplicator's memory lock.
        claim = await asyncio.to_thread(
            claim_dispatch_execution,
            deduplicator=self._deduplicator,
            dispatch=dispatch,
            user_id=user_id,
            broker_account_id=broker_account_id,
            allow_historical_replay=allow_historical_replay,
        )
        dedup_key = claim.dedup_key
        if claim.duplicate_result is not None:
            return claim.duplicate_result

        resolved: ResolvedDispatch | None = None
        resolution_error: DispatchResolutionError | None = None
        try:
            resolution = self._dispatch_resolver.resolve(
                dispatch=dispatch,
                user_id=user_id,
                dedup_key=dedup_key,
                credentials=credentials,
                score_context=score_context,
                allow_historical_replay=allow_historical_replay,
            )
            if isinstance(resolution, DispatchOutcome):
                return self._short_circuit_result(
                    dedup_key=dedup_key,
                    user_id=user_id,
                    signal=signal,
                    score_context=score_context,
                    profile=profile,
                    signal_id=signal_id,
                    symbol=signal.symbol,
                    execution_mode=resolution.execution_mode,
                    broker=resolution.broker,
                    error_message=resolution.error_message,
                    success=resolution.success,
                    reason=resolution.reason,
                    block_reason=resolution.block_reason,
                    broker_account_id=resolution.broker_account_id,
                    settlement_currency=resolution.settlement_currency,
                )
            resolved = resolution
            return await execute_resolved_signal(
                self._execution_services(),
                resolved,
                close_quantity_override=close_quantity_override,
                target_position_override=target_position_override,
            )
        except DispatchResolutionError as exc:
            resolution_error = exc
            error = exc.cause
        except Exception as exc:
            error = exc

        logger.exception(
            "Error handling signal",
            exc_info=(type(error), error, error.__traceback__),
            **trace_ctx,
        )
        record_unexpected_dispatch_failure(
            breaker_manager=self._breaker_manager,
            user_id=user_id,
            strategy_id=signal.strategy_id,
            error=error,
            resolved=resolved,
            resolution_error=resolution_error,
        )
        return self._short_circuit_result(
            dedup_key=dedup_key,
            user_id=user_id,
            signal=signal,
            score_context=score_context,
            profile=profile,
            signal_id=signal_id,
            symbol=signal.symbol,
            execution_mode=str(user_strategy_config.get("execution_mode", "spot")),
            broker=str(profile.get("broker", "paper")),
            error_message=str(error),
            broker_account_id=(
                resolved.broker_account_id
                if resolved is not None
                else resolution_error.broker_account_id
                if resolution_error is not None
                else broker_account_id
            ),
            settlement_currency=(
                resolved.settlement_currency
                if resolved is not None
                else resolution_error.settlement_currency
                if resolution_error is not None
                else None
            ),
        )

    # ------------------------------------------------------------------
    # Broker resolution — thin delegates to ``BrokerResolver``. Method names
    # preserved for tests that patch ``engine._get_broker`` etc.
    # ------------------------------------------------------------------

    async def _get_broker(
        self,
        broker_type: BrokerType,
        environment: str,
        credential_ref: str,
        credentials: dict[str, str] | None = None,
        *,
        user_id: str,
        broker_account_id: int,
        account_currency: str,
        settlement_currency: str | None,
        paper_initial_equity: float | None = None,
        paper_initial_cash: float | None = None,
    ) -> BrokerAdapter | None:
        if settlement_currency is None:
            msg = "Broker resolution requires instrument settlement_currency"
            raise ValueError(msg)
        return await self._broker_resolver.get_broker(
            broker_type=broker_type,
            environment=environment,
            credential_ref=credential_ref,
            credentials=credentials,
            user_id=user_id,
            broker_account_id=broker_account_id,
            account_currency=account_currency,
            settlement_currency=settlement_currency,
            paper_initial_equity=paper_initial_equity,
            paper_initial_cash=paper_initial_cash,
        )

    async def get_reconciliation_broker(self, context: dict[str, Any]) -> BrokerAdapter | None:
        # The execution path resolves a paper book per user and linked account.
        # Reconciliation must resolve the same cache partition or it sees an
        # empty book and falsely reports every persisted position as missing.
        return await self._get_broker(
            broker_type=BrokerType(str(context["broker_code"])),
            environment=str(context["environment"]),
            credential_ref=str(context["credential_ref"]),
            credentials=context.get("credentials"),
            user_id=str(context["user_id"]),
            broker_account_id=context.get("broker_account_id"),
            account_currency=self._route_resolver.resolve_account_currency(
                user_id=str(context["user_id"]),
                account_id=context.get("broker_account_id"),
            ),
            settlement_currency=context.get("settlement_currency"),
            paper_initial_equity=self._paper_account_value(
                context,
                "paper_initial_equity",
            ),
            paper_initial_cash=self._paper_account_value(
                context,
                "paper_initial_cash",
            ),
        )

    @staticmethod
    def _paper_account_value(context: dict[str, Any], key: str) -> float | None:
        profile = dict(context.get("profile") or {})
        account_id = context.get("broker_account_id")
        account = dict((profile.get("accounts") or {}).get(str(account_id)) or {})
        value = account.get(key)
        return float(value) if value is not None else None

    async def get_aggregate_pnl(
        self,
        *,
        user_id: str,
        account_id: int,
        strategy_id: str | None = None,
    ) -> dict[str, Any]:
        """Return account-scoped aggregate P&L in the account base currency."""
        return await self._pnl_service.get_aggregate_pnl(
            user_id=user_id,
            account_id=account_id,
            strategy_id=strategy_id,
        )

    # ------------------------------------------------------------------
    # Execution persistence boundary used by the extracted orchestration step.
    # ------------------------------------------------------------------

    def _persist_execution(
        self,
        user_id: str,
        signal: Signal,
        result: ExecutionResult,
        score_context: dict[str, float] | None,
        account_state: AccountState | None,
        profile: dict[str, Any] | None,
        intents: list[OrderIntent | OptionsIntent] | None,
    ) -> str | None:
        return self._persistence.persist(
            user_id=user_id,
            signal=signal,
            result=result,
            score_context=score_context,
            account_state=account_state,
            profile=profile,
            intents=intents,
        )

    def _persist_paper_fill_projections(self, **kwargs: Any) -> str:
        """Persist one checkpointed local-paper fill without duplicating events."""
        return self._persistence.persist_paper_fill_projections(**kwargs)

    # ------------------------------------------------------------------
    # Account / market-data lookups — thin delegates to ``StateProvider``.
    # The extracted orchestration step uses this engine-owned boundary.
    # ------------------------------------------------------------------

    async def _get_account_state(
        self,
        user_id: str,
        broker: BrokerAdapter,
        profile: dict[str, Any] | None = None,
        allow_profile_fallback: bool = True,
    ) -> AccountState:
        return await self._state_provider.get_account_state(
            user_id=user_id,
            broker=broker,
            profile=profile,
            allow_profile_fallback=allow_profile_fallback,
        )

    def _get_daily_trade_count(
        self,
        *,
        user_id: str,
        profile: dict[str, Any] | None = None,
    ) -> int:
        return self._state_provider.get_daily_trade_count(user_id=user_id, profile=profile)

    def _account_from_profile(
        self,
        profile: dict[str, Any] | None,
        broker_type: BrokerType | None = None,
    ) -> AccountState:
        return self._state_provider.account_from_profile(profile, broker_type)

    async def _get_market_data(
        self,
        symbol: str,
        *,
        user_id: str,
        environment: str,
    ) -> dict[str, Any]:
        return await self._state_provider.get_market_data(
            symbol,
            user_id=user_id,
            environment=environment,
        )

    # ------------------------------------------------------------------
    # Order-execution boundary used by the extracted orchestration step.
    # ------------------------------------------------------------------

    async def _execute_orders(
        self,
        broker: BrokerAdapter,
        intents: list[OrderIntent | OptionsIntent],
        *,
        breaker_key: str | None = None,
        user_id: str = "",
        signal: Signal | None = None,
        execution_mode: str = "paper",
        broker_code: str = "paper",
        broker_environment: str | None = None,
        credential_ref: str | None = None,
        run_id: str | None = None,
        broker_account_id: int | None = None,
        settlement_currency: str | None = None,
        execution_key: str | None = None,
    ) -> list[BrokerOrderResult]:
        return await self._order_executor.execute(
            broker,
            intents,
            breaker_key=breaker_key,
            user_id=user_id,
            signal=signal,
            execution_mode=execution_mode,
            broker_code=broker_code,
            broker_environment=broker_environment,
            credential_ref=credential_ref,
            run_id=run_id,
            broker_account_id=broker_account_id,
            settlement_currency=settlement_currency,
            execution_key=execution_key,
        )

    def _aggregate_results(
        self,
        signal_id: str,
        symbol: str,
        exec_mode: ExecutionMode,
        broker_type: BrokerType,
        order_results: list[BrokerOrderResult],
        broker_account_id: int | None = None,
        settlement_currency: str | None = None,
    ) -> ExecutionResult:
        return OrderExecutor.aggregate(
            signal_id=signal_id,
            symbol=symbol,
            exec_mode=exec_mode,
            broker_type=broker_type,
            order_results=order_results,
            broker_account_id=broker_account_id,
            settlement_currency=settlement_currency,
        )

    async def close(self) -> None:
        """Close all broker connections."""
        for broker in self._broker_resolver.cache.values():
            try:
                await broker.disconnect()
            except Exception:
                # Shutdown boundary catch: a broker raising any error must not
                # stop us from closing the remaining brokers. KeyboardInterrupt
                # and SystemExit derive from BaseException and propagate.
                logger.exception("Error disconnecting broker")
        try:
            self._alerts.close()
        except Exception:
            # Shutdown boundary catch: alert publisher close must not block
            # the rest of the cleanup. KeyboardInterrupt / SystemExit propagate.
            logger.exception("Error closing alert publisher")
        self._broker_resolver.cache.clear()
