"""Input normalization and pre-execution resolution for signal dispatch."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, NamedTuple

from lib_application.services.instrument_resolution import BrokerInstrumentIdentity
from lib_common.internal_events import BrokerRouteSnapshot, ExecutionPolicySnapshot
from lib_common.logging import get_logger
from lib_common.observability import build_trace_context, ensure_run_id
from lib_strategy.signals.signal import Signal, SignalAction

from .broker_bridge import resolve_credential_ref
from .broker_resolver import BrokerCapabilityError, BrokerResolver
from .circuit_breaker_manager import CircuitBreakerManager
from .config import BrokerType, ExecutionMode
from .deduplication import ExecutionDeduplicator
from .execution_gates import ExecutionGatekeeper
from .execution_result import ExecutionResult
from .execution_routing import ExecutionRouteResolver

logger = get_logger(__name__)


class SignalDispatchContext(NamedTuple):
    """Per-signal identifiers + normalized inputs for the execution dispatch."""

    profile: dict[str, Any]
    user_strategy_config: dict[str, Any]
    signal: Signal
    signal_id: str
    run_id: str
    trace_ctx: dict[str, Any]
    # Stable identity for dedup/idempotency. ``signal_id`` can be regenerated
    # per POST (fresh run_id), so it is never a safe execution identity.
    dedup_identity: str


@dataclass(frozen=True)
class DispatchOutcome:
    """A resolved no-order outcome that the engine persists and returns."""

    execution_mode: str
    broker: str
    error_message: str | None
    broker_account_id: int | None = None
    settlement_currency: str | None = None
    success: bool = False
    reason: str | None = None
    block_reason: str | None = None


@dataclass(frozen=True)
class ResolvedDispatch:
    """All immutable facts required by the broker execution phase."""

    user_id: str
    signal: Signal
    signal_id: str
    run_id: str
    trace_ctx: dict[str, Any]
    dedup_key: str
    profile: dict[str, Any]
    user_strategy_config: dict[str, Any]
    score_context: dict[str, float] | None
    score_ctx: dict[str, Any]
    credentials: dict[str, str] | None
    broker_type: BrokerType
    environment: str
    credential_ref: str
    exec_mode: ExecutionMode
    strategy_breaker_key: str
    broker_breaker_key: str
    broker_account_id: int
    account_currency: str
    settlement_currency: str | None
    broker_instrument: BrokerInstrumentIdentity | None
    paper_initial_equity: float | None
    paper_initial_cash: float | None
    allow_historical_replay: bool


class DispatchResolutionError(Exception):
    """Unexpected resolver failure with enough context for breaker attribution."""

    def __init__(
        self,
        cause: Exception,
        *,
        broker_type: BrokerType | None,
        environment: str | None,
        strategy_breaker_key: str | None,
        broker_breaker_key: str | None,
        broker_account_id: int | None,
        settlement_currency: str | None,
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.broker_type = broker_type
        self.environment = environment
        self.strategy_breaker_key = strategy_breaker_key
        self.broker_breaker_key = broker_breaker_key
        self.broker_account_id = broker_account_id
        self.settlement_currency = settlement_currency


@dataclass
class _ResolutionState:
    """Mutable facts passed between fail-closed resolution stages."""

    dispatch: SignalDispatchContext
    user_id: str
    dedup_key: str
    credentials: dict[str, str] | None
    score_context: dict[str, float] | None
    allow_historical_replay: bool
    user_strategy_config: dict[str, Any]
    environment: str | None = None
    score_ctx: dict[str, Any] | None = None
    exec_mode: ExecutionMode | None = None
    broker_type: BrokerType | None = None
    broker_account_id: int | None = None
    settlement_currency: str | None = None
    account_currency: str | None = None
    credential_ref: str | None = None
    strategy_breaker_key: str | None = None
    broker_breaker_key: str | None = None
    broker_instrument: BrokerInstrumentIdentity | None = None


class DispatchClaim(NamedTuple):
    """Deduplication key and optional already-claimed no-op result."""

    dedup_key: str
    duplicate_result: ExecutionResult | None


def explicit_profile_account_id(profile: dict[str, Any]) -> int | None:
    """Read only an explicitly routed account identity from the profile."""
    route = dict(profile.get("_broker_route_snapshot") or {})
    raw_account_id = route.get("broker_account_id")
    if raw_account_id is None:
        raw_account_id = profile.get("broker_account_id")
    if isinstance(raw_account_id, bool) or not isinstance(raw_account_id, (int, str)):
        return None
    try:
        account_id = int(raw_account_id)
    except (TypeError, ValueError):
        return None
    return account_id if account_id > 0 else None


def claim_dispatch_execution(
    *,
    deduplicator: ExecutionDeduplicator,
    dispatch: SignalDispatchContext,
    user_id: str,
    broker_account_id: int,
    allow_historical_replay: bool,
) -> DispatchClaim:
    """Claim a stable execution identity without touching a broker."""
    signal = dispatch.signal
    action = signal.action.value.lower()
    dedup_identity = dispatch.dedup_identity
    if allow_historical_replay:
        dedup_identity = f"historical-replay:v1:{dedup_identity}"
    dedup_key = deduplicator.get_execution_key(
        external_signal_id=dedup_identity,
        user_id=user_id,
        broker_account_id=broker_account_id,
        symbol=signal.symbol,
        action=action,
    )
    if deduplicator.claim_execution(
        dedup_key,
        signal_id=dispatch.signal_id,
        user_id=user_id,
        symbol=signal.symbol,
        action=action,
    ):
        return DispatchClaim(dedup_key, None)
    message = "Duplicate execution request skipped"
    logger.warning(message, **dispatch.trace_ctx)
    return DispatchClaim(
        dedup_key,
        ExecutionResult(
            success=True,
            signal_id=dispatch.signal_id,
            symbol=signal.symbol,
            execution_mode="dedup",
            broker=str(dispatch.profile.get("broker", "paper")),
            orders_submitted=0,
            orders_filled=0,
            total_quantity=0.0,
            average_price=0.0,
            total_commission=0.0,
            broker_account_id=broker_account_id,
            error_message=message,
        ),
    )


def record_unexpected_dispatch_failure(
    *,
    breaker_manager: CircuitBreakerManager,
    user_id: str,
    strategy_id: str,
    error: Exception,
    resolved: ResolvedDispatch | None,
    resolution_error: DispatchResolutionError | None,
) -> None:
    """Attribute an unexpected resolver/execution error to one breaker scope."""
    broker_type = (
        resolved.broker_type
        if resolved is not None
        else resolution_error.broker_type
        if resolution_error is not None
        else None
    )
    if broker_type is None:
        return
    environment = (
        resolved.environment
        if resolved is not None
        else resolution_error.environment
        if resolution_error is not None
        else None
    )
    strategy_key = (
        resolved.strategy_breaker_key
        if resolved is not None
        else resolution_error.strategy_breaker_key
        if resolution_error is not None
        else None
    )
    broker_key = (
        resolved.broker_breaker_key
        if resolved is not None
        else resolution_error.broker_breaker_key
        if resolution_error is not None
        else None
    )
    scope = (
        "broker" if breaker_manager.is_broker_system_error(error_message=str(error)) else "strategy"
    )
    breaker_key = broker_key if scope == "broker" else strategy_key
    if breaker_key is None:
        return
    account_id = (
        resolved.broker_account_id
        if resolved is not None
        else resolution_error.broker_account_id
        if resolution_error is not None
        else None
    )
    breaker_manager.record_failure(
        scope=scope,
        breaker_key=breaker_key,
        user_id=user_id,
        strategy_id=strategy_id,
        broker=broker_type.value,
        environment=environment,
        reason=str(error),
        broker_account_id=account_id,
    )


class DispatchResolver:
    """Resolve policy, route, account, and breaker state before broker access."""

    def __init__(
        self,
        *,
        default_mode: str,
        route_resolver: ExecutionRouteResolver,
        broker_resolver: BrokerResolver,
        gatekeeper: ExecutionGatekeeper,
        breaker_manager: CircuitBreakerManager,
    ) -> None:
        self._default_mode = default_mode
        self._route_resolver = route_resolver
        self._broker_resolver = broker_resolver
        self._gatekeeper = gatekeeper
        self._breaker_manager = breaker_manager

    def resolve(
        self,
        *,
        dispatch: SignalDispatchContext,
        user_id: str,
        dedup_key: str,
        credentials: dict[str, str] | None,
        score_context: dict[str, float] | None,
        allow_historical_replay: bool,
    ) -> ResolvedDispatch | DispatchOutcome:
        """Resolve a dispatch or return its deterministic short-circuit outcome."""
        initial_account_id = explicit_profile_account_id(dispatch.profile)
        if initial_account_id is None:
            raw_account_id = dispatch.user_strategy_config.get("broker_account_id")
            if isinstance(raw_account_id, int) and not isinstance(raw_account_id, bool):
                initial_account_id = raw_account_id if raw_account_id > 0 else None
        state = _ResolutionState(
            dispatch=dispatch,
            user_id=user_id,
            dedup_key=dedup_key,
            credentials=credentials,
            score_context=score_context,
            allow_historical_replay=allow_historical_replay,
            user_strategy_config=dispatch.user_strategy_config,
            broker_account_id=initial_account_id,
        )
        try:
            for stage in (
                self._resolve_mode_and_broker,
                self._resolve_account_route,
                self._check_policy_gates,
                self._resolve_execution_action,
            ):
                outcome = stage(state)
                if outcome is not None:
                    return outcome
            return self._build_resolved_dispatch(state)
        except Exception as exc:
            raise DispatchResolutionError(
                exc,
                broker_type=state.broker_type,
                environment=state.environment,
                strategy_breaker_key=state.strategy_breaker_key,
                broker_breaker_key=state.broker_breaker_key,
                broker_account_id=state.broker_account_id,
                settlement_currency=state.settlement_currency,
            ) from exc

    def _resolve_mode_and_broker(
        self,
        state: _ResolutionState,
    ) -> DispatchOutcome | None:
        dispatch = state.dispatch
        profile = dispatch.profile
        raw_mode = (
            state.user_strategy_config.get("mode") or profile.get("mode") or self._default_mode
        )
        logger.debug(
            "Execution inputs",
            raw_mode=raw_mode,
            action=dispatch.signal.action.value.lower(),
            **dispatch.trace_ctx,
        )
        mode = self._route_resolver.normalize_mode(
            raw_mode,
            trace_ctx=dispatch.trace_ctx,
        )
        state.environment = self._route_resolver.resolve_environment(
            mode=mode,
            profile=profile,
        )
        gate_outcome = self._check_signal_and_live_gates(state, mode)
        if gate_outcome is not None:
            return gate_outcome
        state.score_ctx = dict(
            state.score_context or state.user_strategy_config.get("score_context", {}) or {}
        )
        state.exec_mode = self._resolve_execution_mode(
            user_strategy_config=state.user_strategy_config,
            score_ctx=state.score_ctx,
            trace_ctx=dispatch.trace_ctx,
        )
        try:
            state.broker_type = self._broker_resolver.resolve_broker_type(
                exec_mode=state.exec_mode,
                profile=profile,
                user_strategy_config=state.user_strategy_config,
                asset_class=dispatch.signal.asset_class,
                environment=state.environment,
            )
        except BrokerCapabilityError as exc:
            logger.warning(str(exc), **dispatch.trace_ctx)
            return DispatchOutcome(
                execution_mode="blocked",
                broker=str(profile.get("broker", "paper")),
                error_message=str(exc),
                broker_account_id=state.broker_account_id,
            )
        return self._check_allowed_broker(state)

    def _check_signal_and_live_gates(
        self,
        state: _ResolutionState,
        mode: str,
    ) -> DispatchOutcome | None:
        dispatch = state.dispatch
        environment = state.environment
        assert environment is not None
        if not state.allow_historical_replay:
            error = self._gatekeeper.check_signal_freshness(
                signal=dispatch.signal,
                environment=environment,
                trace_ctx=dispatch.trace_ctx,
            )
            if error is not None:
                return DispatchOutcome(
                    execution_mode="blocked",
                    broker=str(dispatch.profile.get("broker", "paper")),
                    error_message=error,
                    broker_account_id=state.broker_account_id,
                )
        error = self._gatekeeper.check_live_mode(
            environment=environment,
            mode=mode,
            profile=dispatch.profile,
            user_strategy_config=state.user_strategy_config,
            trace_ctx=dispatch.trace_ctx,
        )
        if error is None:
            return None
        pre_resolution_prefixes = (
            "Live mode blocked: EXECUTION_ENGINE_ALLOW_LIVE",
            "Live mode blocked: user profile live_enabled",
        )
        broker = (
            "paper"
            if error.startswith(pre_resolution_prefixes)
            else str(dispatch.profile.get("broker", "paper"))
        )
        return DispatchOutcome(
            execution_mode="blocked",
            broker=broker,
            error_message=error,
            broker_account_id=state.broker_account_id,
        )

    def _check_allowed_broker(
        self,
        state: _ResolutionState,
    ) -> DispatchOutcome | None:
        broker_type = state.broker_type
        exec_mode = state.exec_mode
        assert broker_type is not None
        assert exec_mode is not None
        allowed = self._broker_resolver.resolve_allowed_brokers(
            state.dispatch.profile,
            state.user_strategy_config,
        )
        if allowed and "*" not in allowed and broker_type.value not in allowed:
            message = f"Broker {broker_type.value} not allowed; allowed_brokers={sorted(allowed)}"
            logger.warning(
                message,
                **state.dispatch.trace_ctx,
                broker=broker_type.value,
                allowed_brokers=allowed,
            )
            return DispatchOutcome(
                execution_mode="blocked",
                broker=broker_type.value,
                error_message=message,
                broker_account_id=state.broker_account_id,
            )
        logger.info(
            "Execution broker resolved",
            **state.dispatch.trace_ctx,
            execution_mode=exec_mode.value,
            broker=broker_type.value,
            allowed_brokers=allowed or None,
        )
        return None

    def _resolve_account_route(
        self,
        state: _ResolutionState,
    ) -> DispatchOutcome | None:
        dispatch = state.dispatch
        broker_type = state.broker_type
        environment = state.environment
        assert broker_type is not None
        assert environment is not None
        try:
            state.broker_account_id = self._route_resolver.resolve_account_id(
                user_id=state.user_id,
                broker_type=broker_type,
                environment=environment,
                profile=dispatch.profile,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            return self._route_error(
                state,
                exc,
                log_message="Broker account validation blocked execution",
                block_reason="broker_account_invalid",
            )
        try:
            state.settlement_currency = self._route_resolver.resolve_settlement_currency(
                dispatch.signal
            )
        except (RuntimeError, ValueError) as exc:
            return self._route_error(
                state,
                exc,
                log_message="Settlement-currency validation blocked execution",
                block_reason="settlement_currency_invalid",
            )
        try:
            state.account_currency = self._route_resolver.resolve_account_currency(
                user_id=state.user_id,
                account_id=state.broker_account_id,
            )
        except (RuntimeError, ValueError) as exc:
            return self._route_error(
                state,
                exc,
                log_message="Broker account currency validation blocked execution",
                block_reason="account_currency_invalid",
            )
        return None

    @staticmethod
    def _route_error(
        state: _ResolutionState,
        error: Exception,
        *,
        log_message: str,
        block_reason: str,
    ) -> DispatchOutcome:
        broker_type = state.broker_type
        assert broker_type is not None
        logger.warning(
            log_message,
            error=str(error),
            **state.dispatch.trace_ctx,
        )
        return DispatchOutcome(
            execution_mode="blocked",
            broker=broker_type.value,
            error_message=str(error),
            broker_account_id=state.broker_account_id,
            settlement_currency=state.settlement_currency,
            block_reason=block_reason,
        )

    def _check_policy_gates(
        self,
        state: _ResolutionState,
    ) -> DispatchOutcome | None:
        dispatch = state.dispatch
        broker_type = state.broker_type
        environment = state.environment
        account_id = state.broker_account_id
        assert broker_type is not None
        assert environment is not None
        assert account_id is not None
        state.credential_ref = resolve_credential_ref(
            broker_type=broker_type,
            profile=dispatch.profile,
            user_strategy_config=state.user_strategy_config,
            environment=environment,
            broker_account_id=account_id,
        )
        state.strategy_breaker_key = self._breaker_manager.strategy_key(
            user_id=state.user_id,
            strategy_id=dispatch.signal.strategy_id,
            broker_code=broker_type.value,
            credential_ref=state.credential_ref,
        )
        state.broker_breaker_key = self._breaker_manager.broker_key(
            broker_code=broker_type.value,
            environment=environment,
        )
        breaker_error = self._breaker_manager.check_execution(
            user_id=state.user_id,
            strategy_id=dispatch.signal.strategy_id,
            broker_type=broker_type,
            environment=environment,
            credential_ref=state.credential_ref,
            strategy_breaker_key=state.strategy_breaker_key,
            broker_breaker_key=state.broker_breaker_key,
            broker_account_id=account_id,
            reduce_only=dispatch.signal.action == SignalAction.CLOSE,
            trace_ctx=dispatch.trace_ctx,
        )
        if breaker_error is not None:
            return DispatchOutcome(
                execution_mode="blocked",
                broker=broker_type.value,
                error_message=breaker_error,
                broker_account_id=state.broker_account_id,
                settlement_currency=state.settlement_currency,
            )
        score_error = self._gatekeeper.check_score_thresholds(
            score_ctx=state.score_ctx or {},
            user_strategy_config=state.user_strategy_config,
            trace_ctx=dispatch.trace_ctx,
        )
        if score_error is not None:
            return DispatchOutcome(
                execution_mode="blocked",
                broker=broker_type.value,
                error_message=score_error,
                broker_account_id=state.broker_account_id,
                settlement_currency=state.settlement_currency,
            )
        state.user_strategy_config = {
            **state.user_strategy_config,
            "broker": broker_type.value,
        }
        return None

    def _resolve_execution_action(
        self,
        state: _ResolutionState,
    ) -> DispatchOutcome | None:
        dispatch = state.dispatch
        broker_type = state.broker_type
        exec_mode = state.exec_mode
        environment = state.environment
        account_id = state.broker_account_id
        assert broker_type is not None
        assert exec_mode is not None
        assert environment is not None
        assert account_id is not None
        if exec_mode == ExecutionMode.NOTIFY_ONLY:
            logger.info(
                "NOTIFY_ONLY mode - signal recorded but not executed",
                **dispatch.trace_ctx,
            )
            return DispatchOutcome(
                execution_mode=exec_mode.value,
                broker=broker_type.value,
                error_message=None,
                broker_account_id=state.broker_account_id,
                settlement_currency=state.settlement_currency,
                success=True,
                reason="notify_only",
            )
        if dispatch.signal.action == SignalAction.HOLD:
            logger.info("HOLD signal - no action taken", **dispatch.trace_ctx)
            return DispatchOutcome(
                execution_mode=exec_mode.value,
                broker=broker_type.value,
                error_message=None,
                broker_account_id=state.broker_account_id,
                settlement_currency=state.settlement_currency,
                success=True,
                reason="hold_no_action",
            )
        tradability_error = self._gatekeeper.check_instrument_tradability(
            signal=dispatch.signal,
            environment=environment,
            trace_ctx=dispatch.trace_ctx,
        )
        if tradability_error is not None:
            reason, message = tradability_error
            return DispatchOutcome(
                execution_mode="blocked",
                broker=broker_type.value,
                error_message=message,
                broker_account_id=state.broker_account_id,
                settlement_currency=state.settlement_currency,
                block_reason=reason,
            )
        session_error = self._gatekeeper.check_market_session(
            signal=dispatch.signal,
            environment=environment,
            trace_ctx=dispatch.trace_ctx,
            allow_historical_replay=state.allow_historical_replay,
        )
        if session_error is not None:
            reason, message = session_error
            return DispatchOutcome(
                execution_mode="blocked",
                broker=broker_type.value,
                error_message=message,
                broker_account_id=state.broker_account_id,
                settlement_currency=state.settlement_currency,
                block_reason=reason,
            )
        try:
            state.broker_instrument = self._route_resolver.resolve_broker_instrument(
                user_id=state.user_id,
                account_id=account_id,
                broker_type=broker_type,
                signal=dispatch.signal,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            return self._route_error(
                state,
                exc,
                log_message="Broker instrument validation blocked execution",
                block_reason="broker_instrument_invalid",
            )
        return None

    @staticmethod
    def _build_resolved_dispatch(state: _ResolutionState) -> ResolvedDispatch:
        dispatch = state.dispatch
        broker_type = state.broker_type
        environment = state.environment
        credential_ref = state.credential_ref
        exec_mode = state.exec_mode
        strategy_breaker_key = state.strategy_breaker_key
        broker_breaker_key = state.broker_breaker_key
        account_id = state.broker_account_id
        account_currency = state.account_currency
        assert broker_type is not None
        assert environment is not None
        assert credential_ref is not None
        assert exec_mode is not None
        assert strategy_breaker_key is not None
        assert broker_breaker_key is not None
        assert account_id is not None
        assert account_currency is not None
        account_snapshot = dict((dispatch.profile.get("accounts") or {}).get(str(account_id)) or {})
        paper_equity = account_snapshot.get("paper_initial_equity")
        paper_cash = account_snapshot.get("paper_initial_cash")
        return ResolvedDispatch(
            user_id=state.user_id,
            signal=dispatch.signal,
            signal_id=dispatch.signal_id,
            run_id=dispatch.run_id,
            trace_ctx=dispatch.trace_ctx,
            dedup_key=state.dedup_key,
            profile=dispatch.profile,
            user_strategy_config=state.user_strategy_config,
            score_context=state.score_context,
            score_ctx=state.score_ctx or {},
            credentials=state.credentials,
            broker_type=broker_type,
            environment=environment,
            credential_ref=credential_ref,
            exec_mode=exec_mode,
            strategy_breaker_key=strategy_breaker_key,
            broker_breaker_key=broker_breaker_key,
            broker_account_id=account_id,
            account_currency=account_currency,
            settlement_currency=state.settlement_currency,
            broker_instrument=state.broker_instrument,
            paper_initial_equity=(float(paper_equity) if paper_equity is not None else None),
            paper_initial_cash=(float(paper_cash) if paper_cash is not None else None),
            allow_historical_replay=state.allow_historical_replay,
        )

    @staticmethod
    def _resolve_execution_mode(
        *,
        user_strategy_config: dict[str, Any],
        score_ctx: dict[str, Any],
        trace_ctx: dict[str, Any],
    ) -> ExecutionMode:
        mode_value = (
            user_strategy_config.get("execution_mode")
            or score_ctx.get("recommended_mode")
            or "spot"
        )
        if isinstance(mode_value, ExecutionMode):
            return mode_value
        normalized = str(mode_value).lower()
        if normalized == "direct":
            normalized = "spot"
        try:
            return ExecutionMode(normalized)
        except ValueError as exc:
            msg = f"Unknown instrument execution_mode {normalized!r}"
            logger.warning(msg, **trace_ctx)
            raise ValueError(msg) from exc


def build_dispatch_context(
    *,
    user_id: str,
    profile: dict[str, Any],
    user_strategy_config: dict[str, Any],
    signal: Signal,
) -> SignalDispatchContext:
    """Normalize inputs and derive per-signal identifiers + trace context.

    Copies profile/config, derives ``signal_id``/``run_id`` (rewriting the
    signal if the run_id changed), threads the execution-policy snapshot onto
    the profile, and builds the trace context.
    """
    profile = dict(profile or {})
    user_strategy_config = dict(user_strategy_config or {})
    route_snapshot = profile.get("_broker_route_snapshot")
    if route_snapshot:
        profile["_broker_route_snapshot"] = BrokerRouteSnapshot.model_validate(
            route_snapshot
        ).model_dump(mode="json")
    policy_source = (
        user_strategy_config if user_strategy_config.get("_execution_policy_snapshot") else profile
    )
    policy_snapshot = policy_source.get("_execution_policy_snapshot")
    if policy_snapshot:
        policy_source["_execution_policy_snapshot"] = ExecutionPolicySnapshot.model_validate(
            policy_snapshot
        ).model_dump(mode="json")
    signal_ts = getattr(signal, "timestamp", None)
    signal_id_suffix = (
        signal_ts.timestamp()
        if isinstance(signal_ts, datetime)
        else datetime.now(tz=UTC).timestamp()
    )
    signal_id = signal.signal_id or f"{signal.strategy_id}_{signal_id_suffix}"
    dedup_identity = signal.external_signal_id
    if not dedup_identity:
        msg = f"Signal {signal_id} has no canonical external_signal_id"
        raise ValueError(msg)
    run_id = ensure_run_id(signal.run_id)
    if signal.run_id != run_id:
        signal = replace(signal, run_id=run_id)
    profile["_execution_user_strategy_config"] = dict(user_strategy_config)
    if (
        "_execution_policy_snapshot" in user_strategy_config
        and "_execution_policy_snapshot" not in profile
    ):
        profile["_execution_policy_snapshot"] = dict(
            user_strategy_config.get("_execution_policy_snapshot") or {}
        )
    trace_ctx = build_trace_context(
        run_id=run_id,
        signal_id=signal_id,
        strategy_id=signal.strategy_id,
        symbol=signal.symbol,
        user_id=user_id,
    )
    return SignalDispatchContext(
        profile=profile,
        user_strategy_config=user_strategy_config,
        signal=signal,
        signal_id=signal_id,
        run_id=run_id,
        trace_ctx=trace_ctx,
        dedup_identity=dedup_identity,
    )


__all__ = [
    "DispatchClaim",
    "DispatchOutcome",
    "DispatchResolutionError",
    "DispatchResolver",
    "ResolvedDispatch",
    "SignalDispatchContext",
    "build_dispatch_context",
    "claim_dispatch_execution",
    "explicit_profile_account_id",
    "record_unexpected_dispatch_failure",
]
