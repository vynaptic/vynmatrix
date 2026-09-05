"""Post-gating execution phase for ``ExecutionEngine.handle_signal``.

``execute_resolved_signal`` runs everything *after* the pre-trade gates have
passed and the broker/mode/credential/circuit-breaker state has been resolved:
connect the broker, fetch account + market state, build and risk-check intents,
submit orders, then aggregate, finalize and snapshot. The pre-trade gating
stays in ``engine.handle_signal``; this is the second half of that pipeline,
extracted to a sibling module so the engine stays under the architecture LOC
cap (mirrors ``_dispatch.build_dispatch_context``).

The flow receives an ``ExecutionServices`` snapshot (built by the engine per
``handle_signal`` call) instead of the engine instance, so its collaborator
surface is the explicit dataclass contract in ``_services``. The methods the
engine's own tests patch (``_get_broker``, ``_get_account_state`` ...) are
still honored: the snapshot captures whatever is bound at call time.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, cast

from sqlalchemy.exc import SQLAlchemyError

from lib_application.services.instrument_resolution import BrokerInstrumentIdentity
from lib_common.logging import get_logger
from lib_strategy.signals.signal import SignalAction

from ._dispatch import ResolvedDispatch
from .alerts import ExecutionAlert
from .broker_resolver import PaperAccountStateError
from .brokers.base import BrokerAdapter, BrokerOrderResult
from .config import AccountState, BrokerType, ExecutionMode
from .execution_result import ExecutionResult
from .execution_routing import CurrentAuthorityError
from .metrics.fx_rates import FXRateUnavailableError, normalize_currency
from .models import (
    HISTORICAL_REPLAY_FILL_POLICY,
    CloseQuantityOverride,
    ExecutionRequest,
    OptionsIntent,
    OrderCurrencyContext,
    OrderIntent,
    TargetPositionQuantityOverride,
)
from .order_builder import OrderSizingUnavailableError, UnsupportedExecutionModeError
from .risk_baseline import (
    AccountEquityObservation,
    RiskBaseline,
    RiskBaselineUnavailableError,
    resolve_risk_baseline,
)
from .risk_guard import (
    is_exact_target_reduction_intent,
    is_typed_target_reduction_request,
)

if TYPE_CHECKING:
    from ._services import ExecutionServices
    from .metrics.fx_rates import FXRateProvider

logger = get_logger(__name__)


def _attach_broker_instrument_identity(
    intents: list[OrderIntent | OptionsIntent],
    identity: BrokerInstrumentIdentity | None,
) -> None:
    """Attach DB-authoritative venue facts to every broker-bound order leg."""
    if identity is None:
        return
    resolved = {
        "broker_symbol": identity.broker_symbol,
        "broker_instrument_id": identity.broker_instrument_id,
        "broker_instrument_type": identity.broker_instrument_type,
        "lot_size": str(identity.lot_size) if identity.lot_size is not None else None,
    }
    for intent in intents:
        metadata = dict(intent.metadata or {})
        for field_name, value in resolved.items():
            if value is None:
                continue
            supplied = metadata.get(field_name)
            if supplied is not None and str(supplied) != str(value):
                msg = (
                    f"Order metadata {field_name}={supplied!r} conflicts with "
                    f"broker catalogue value {value!r}"
                )
                raise ValueError(msg)
            metadata[field_name] = value
        intent.metadata = metadata


def _attach_historical_replay_fill(
    intents: list[OrderIntent | OptionsIntent],
    resolved: ResolvedDispatch,
) -> None:
    """Attach trusted next-bar provenance only for controlled spot replay."""
    if not resolved.allow_historical_replay:
        return
    if resolved.broker_type != BrokerType.PAPER or resolved.exec_mode != ExecutionMode.SPOT:
        msg = "Historical execution replay supports only the local-paper spot route"
        raise ValueError(msg)
    source = dict(resolved.signal.metadata or {})
    source_price_id = source.get("source_price_id")
    source_revision = source.get("source_content_revision")
    source_price_ts = source.get("source_price_ts")
    if (
        not isinstance(source_price_id, int)
        or isinstance(source_price_id, bool)
        or source_price_id <= 0
        or not isinstance(source_revision, int)
        or isinstance(source_revision, bool)
        or source_revision <= 0
        or not isinstance(source_price_ts, str)
        or not source_price_ts.strip()
    ):
        msg = "Historical execution replay requires exact source-bar provenance"
        raise ValueError(msg)
    immediate = [
        intent
        for intent in intents
        if not isinstance(intent, OptionsIntent) and str(intent.order_type).lower() == "market"
    ]
    if len(immediate) != 1:
        msg = "Historical execution replay requires exactly one immediate spot order"
        raise ValueError(msg)
    metadata = dict(immediate[0].metadata or {})
    metadata.update(
        {
            "historical_replay": True,
            "source_price_id": source_price_id,
            "source_content_revision": source_revision,
            "source_price_ts": source_price_ts,
            "trigger_policy_version": HISTORICAL_REPLAY_FILL_POLICY,
        }
    )
    immediate[0].metadata = metadata


def _resolve_order_currency_context(
    fx_rate_provider: FXRateProvider | None,
    resolved: ResolvedDispatch,
) -> OrderCurrencyContext | None:
    """Resolve the point-in-time account-to-settlement conversion for sizing."""
    if resolved.settlement_currency is None:
        # Isolated tests can run without canonical persistence. Production
        # routes resolve settlement currency from the instrument catalogue and
        # are already blocked by canonical persistence when it is absent.
        return None
    account_currency = normalize_currency(resolved.account_currency)
    settlement_currency = normalize_currency(resolved.settlement_currency)
    requested_at = resolved.signal.timestamp
    if account_currency == settlement_currency:
        return OrderCurrencyContext(
            account_currency=account_currency,
            settlement_currency=settlement_currency,
            account_to_settlement_rate=Decimal("1"),
            requested_at=requested_at,
            observed_at=requested_at,
            source="identity",
        )
    provider = fx_rate_provider
    if provider is None:
        msg = (
            f"Execution sizing requires an observed {account_currency}/"
            f"{settlement_currency} FX rate"
        )
        raise FXRateUnavailableError(msg)
    quote = provider.get_rate(
        base_currency=account_currency,
        quote_currency=settlement_currency,
        as_of=requested_at,
    )
    if quote is None:
        msg = (
            f"No observed FX rate for {account_currency}/{settlement_currency} "
            f"at {requested_at.isoformat()}"
        )
        raise FXRateUnavailableError(msg)
    if quote.base_currency != account_currency or quote.quote_currency != settlement_currency:
        msg = "FX provider returned a currency pair that does not match the execution route"
        raise FXRateUnavailableError(msg)
    return OrderCurrencyContext(
        account_currency=account_currency,
        settlement_currency=settlement_currency,
        account_to_settlement_rate=quote.rate,
        requested_at=requested_at,
        observed_at=quote.observed_at,
        source=quote.source,
    )


def _account_equity_observation(
    resolved: ResolvedDispatch,
    account_state: AccountState,
) -> AccountEquityObservation:
    """Bind one broker-state observation to its reported account identity."""
    raw_account_id = account_state.account_id
    if isinstance(raw_account_id, bool) or raw_account_id is None:
        msg = "Broker account state has no attributable account identity"
        raise RiskBaselineUnavailableError(msg)
    broker_reported_account_id = str(raw_account_id).strip()
    if not broker_reported_account_id:
        msg = "Broker account state has an invalid account identity"
        raise RiskBaselineUnavailableError(msg)
    return AccountEquityObservation(
        user_id=resolved.user_id,
        account_id=resolved.broker_account_id,
        broker_reported_account_id=broker_reported_account_id,
        currency=account_state.currency,
        equity=float(account_state.equity),
        observed_at=account_state.fetched_at,
        source=account_state.source,
    )


def _require_current_equity_observation(
    baseline: RiskBaseline,
    expected: AccountEquityObservation,
) -> None:
    if baseline.current_equity_observation != expected:
        msg = "Risk baseline did not preserve the current equity observation"
        raise RiskBaselineUnavailableError(msg)


class _ResolvedExecutionFlow:
    """Run a resolved dispatch through small, fail-closed execution stages."""

    def __init__(
        self,
        services: ExecutionServices,
        resolved: ResolvedDispatch,
        *,
        close_quantity_override: CloseQuantityOverride | None = None,
        target_position_override: TargetPositionQuantityOverride | None = None,
    ) -> None:
        self.services = services
        self.resolved = resolved
        self.close_quantity_override = close_quantity_override
        self.target_position_override = target_position_override
        self.profile = resolved.profile
        self.broker: BrokerAdapter | None = None
        self.account_state: AccountState | None = None
        self.market_data: dict[str, object] = {}
        self.intents: list[OrderIntent | OptionsIntent] = []

    def _short_circuit(
        self,
        *,
        error_message: str,
        block_reason: str | None = None,
        execution_mode: str | None = None,
    ) -> ExecutionResult:
        resolved = self.resolved
        return self.services.short_circuit_result(
            dedup_key=resolved.dedup_key,
            user_id=resolved.user_id,
            signal=resolved.signal,
            score_context=resolved.score_context,
            profile=self.profile,
            signal_id=resolved.signal_id,
            symbol=resolved.signal.symbol,
            execution_mode=execution_mode or resolved.exec_mode.value,
            broker=resolved.broker_type.value,
            error_message=error_message,
            block_reason=block_reason,
            broker_account_id=resolved.broker_account_id,
            settlement_currency=resolved.settlement_currency,
        )

    def _record_broker_failure(self, message: str) -> None:
        resolved = self.resolved
        self.services.breaker_manager.record_failure(
            scope="broker",
            breaker_key=resolved.broker_breaker_key,
            user_id=resolved.user_id,
            strategy_id=resolved.signal.strategy_id,
            broker=resolved.broker_type.value,
            environment=resolved.environment,
            reason=message,
            broker_account_id=resolved.broker_account_id,
        )

    async def _revalidate_current_authority(
        self,
        *,
        submission_boundary: bool,
    ) -> ExecutionResult | None:
        """Fail closed on revocation without touching a stale broker adapter."""
        if self.services.session_factory is None:
            # Non-durable isolated/backtest callers have no control plane to
            # re-read. Production paper/live construction always supplies it.
            return None
        resolved = self.resolved
        policy_snapshot = dict(
            resolved.user_strategy_config.get("_execution_policy_snapshot")
            or self.profile.get("_execution_policy_snapshot")
            or {}
        )
        raw_binding_id = resolved.user_strategy_config.get(
            "binding_id",
            policy_snapshot.get("binding_id"),
        )
        try:
            binding_id = int(raw_binding_id) if raw_binding_id is not None else None
        except (TypeError, ValueError):
            binding_id = None
        try:
            use_reduction_authority = (
                is_typed_target_reduction_request(
                    resolved.signal,
                    self.target_position_override,
                )
                and self.close_quantity_override is None
            )
            if submission_boundary:
                use_reduction_authority = (
                    use_reduction_authority
                    and is_exact_target_reduction_intent(
                        resolved.signal,
                        self.target_position_override,
                        self.intents,
                    )
                )
            validate_authority = (
                self.services.route_resolver.validate_current_rebalance_reduction_authority
                if use_reduction_authority
                else self.services.route_resolver.validate_current_authority
            )
            authority = validate_authority(
                user_id=resolved.user_id,
                binding_id=binding_id,
                strategy_id=resolved.signal.strategy_id,
                strategy_version=resolved.signal.strategy_version,
                account_id=resolved.broker_account_id,
                broker_type=resolved.broker_type,
                environment=resolved.environment,
                credential_ref=resolved.credential_ref,
                action=resolved.signal.action.value,
                instrument_id=resolved.signal.instrument_id,
            )
            changed = await self.services.broker_resolver.ensure_current_authority(
                user_id=resolved.user_id,
                broker_account_id=resolved.broker_account_id,
                credential_version=authority.credential_version,
            )
            if submission_boundary and changed:
                msg = "Credential generation changed during execution preparation"
                raise CurrentAuthorityError(msg)  # noqa: TRY301
        except CurrentAuthorityError as exc:
            await self.services.broker_resolver.evict_account(
                user_id=resolved.user_id,
                broker_account_id=resolved.broker_account_id,
            )
            message = str(exc)
            logger.warning(
                "Current execution authorization rejected",
                error=message,
                submission_boundary=submission_boundary,
                **resolved.trace_ctx,
            )
            return self._short_circuit(
                execution_mode="blocked",
                error_message=message,
                block_reason="current_authorization_rejected",
            )
        return None

    async def _prepare_broker_and_account(self) -> ExecutionResult | None:  # noqa: PLR0911
        resolved = self.resolved
        authority_error = await self._revalidate_current_authority(
            submission_boundary=False,
        )
        if authority_error is not None:
            return authority_error
        try:
            self.broker = await self.services.get_broker(
                broker_type=resolved.broker_type,
                environment=resolved.environment,
                credential_ref=resolved.credential_ref,
                credentials=resolved.credentials,
                user_id=resolved.user_id,
                broker_account_id=resolved.broker_account_id,
                account_currency=resolved.account_currency,
                settlement_currency=resolved.settlement_currency,
                paper_initial_equity=resolved.paper_initial_equity,
                paper_initial_cash=resolved.paper_initial_cash,
            )
        except PaperAccountStateError as exc:
            message = str(exc)
            logger.warning(
                message,
                block_reason="account_state_unavailable",
                **resolved.trace_ctx,
            )
            return self._short_circuit(
                execution_mode="blocked",
                error_message=message,
                block_reason="account_state_unavailable",
            )
        if self.broker is None:
            message = f"No broker adapter available for {resolved.broker_type.value}"
            logger.error(message, **resolved.trace_ctx)
            self._record_broker_failure(message)
            return self._short_circuit(error_message=message)
        if not self.broker.is_connected and not await self.broker.connect():
            message = f"Failed to connect to broker {resolved.broker_type.value}"
            self._record_broker_failure(message)
            return self._short_circuit(error_message=message)

        self._track_reconciliation_context()
        try:
            self.account_state = await self.services.get_account_state(
                resolved.user_id,
                self.broker,
                self.profile,
                allow_profile_fallback=resolved.environment != "live",
            )
        except RuntimeError as exc:
            message = str(exc)
            logger.warning(
                message,
                block_reason="account_state_unavailable",
                **resolved.trace_ctx,
            )
            return self._short_circuit(
                execution_mode="blocked",
                error_message=message,
                block_reason="account_state_unavailable",
            )
        freshness_error = self.services.gatekeeper.check_account_state_freshness(
            account_state=self.account_state,
            environment=resolved.environment,
            trace_ctx=resolved.trace_ctx,
        )
        if freshness_error is not None:
            reason, message = freshness_error
            return self._short_circuit(
                execution_mode="blocked",
                error_message=message,
                block_reason=reason,
            )
        self._resolve_options_account()
        return None

    def _track_reconciliation_context(self) -> None:
        resolved = self.resolved
        self.services.reconciliation_tracker.track_context(
            breaker_key=resolved.strategy_breaker_key,
            user_id=resolved.user_id,
            strategy_id=resolved.signal.strategy_id,
            broker_code=resolved.broker_type.value,
            environment=resolved.environment,
            credential_ref=resolved.credential_ref,
            profile=self.profile,
            user_strategy_config=resolved.user_strategy_config,
            credentials=resolved.credentials,
            broker_account_id=resolved.broker_account_id,
            settlement_currency=resolved.settlement_currency,
        )

    def _resolve_options_account(self) -> None:
        resolved = self.resolved
        store = self.services.execution_position_store
        if not ExecutionMode.is_options(resolved.exec_mode) or store is None:
            return
        try:
            store.resolve_account_id(
                user_id=resolved.user_id,
                broker_code=resolved.broker_type.value,
                environment=resolved.environment,
                account_id=resolved.broker_account_id,
            )
        except (SQLAlchemyError, OSError):
            logger.exception("Failed to resolve broker account", **resolved.trace_ctx)

    async def _prepare_market_and_intents(self) -> ExecutionResult | None:
        resolved = self.resolved
        broker = cast(BrokerAdapter, self.broker)
        account_state = cast(AccountState, self.account_state)
        self.market_data = await self.services.get_market_data(
            resolved.signal.symbol,
            user_id=resolved.user_id,
            environment=resolved.environment,
        )
        freshness_error = self.services.gatekeeper.check_market_data_freshness(
            market_data=self.market_data,
            environment=resolved.environment,
            trace_ctx=resolved.trace_ctx,
        )
        if freshness_error is not None:
            reason, message = freshness_error
            return self._short_circuit(
                execution_mode="blocked",
                error_message=message,
                block_reason=reason,
            )
        try:
            currency_context = _resolve_order_currency_context(
                self.services.fx_rate_provider,
                resolved,
            )
        except (FXRateUnavailableError, ValueError) as exc:
            message = str(exc)
            logger.warning(
                message,
                block_reason="execution_fx_unavailable",
                **resolved.trace_ctx,
            )
            return self._short_circuit(
                execution_mode="blocked",
                error_message=message,
                block_reason="execution_fx_unavailable",
            )
        currency_error = self._apply_currency_context(broker, currency_context)
        if currency_error is not None:
            return currency_error
        self._seed_paper_price(broker)
        request = ExecutionRequest(
            user_id=resolved.user_id,
            profile=self.profile,
            user_strategy_config=resolved.user_strategy_config,
            signal=resolved.signal,
            score_context=resolved.score_ctx,
            currency_context=currency_context,
            close_quantity_override=self.close_quantity_override,
            target_position_override=self.target_position_override,
        )
        try:
            self.intents = self.services.order_builder.build(
                req=request,
                account=account_state,
                market_data=self.market_data,
            )
        except (OrderSizingUnavailableError, UnsupportedExecutionModeError) as exc:
            message = str(exc)
            block_reason = (
                "sizing_state_unavailable"
                if isinstance(exc, OrderSizingUnavailableError)
                else "execution_mode_unsupported"
            )
            logger.warning(
                message,
                block_reason=block_reason,
                **resolved.trace_ctx,
            )
            return self._short_circuit(
                execution_mode="blocked",
                error_message=message,
                block_reason=block_reason,
            )
        try:
            _attach_broker_instrument_identity(
                self.intents,
                resolved.broker_instrument,
            )
            _attach_historical_replay_fill(self.intents, resolved)
        except ValueError as exc:
            message = str(exc)
            logger.warning(
                message,
                block_reason="broker_instrument_invalid",
                **resolved.trace_ctx,
            )
            return self._short_circuit(
                execution_mode="blocked",
                error_message=message,
                block_reason="broker_instrument_invalid",
            )
        return None

    def _apply_currency_context(
        self,
        broker: BrokerAdapter,
        currency_context: OrderCurrencyContext | None,
    ) -> ExecutionResult | None:
        if currency_context is None or not hasattr(broker, "set_currency_context"):
            return None
        try:
            broker.set_currency_context(self.resolved.signal.symbol, currency_context)
        except (TypeError, ValueError, AttributeError) as exc:
            message = f"Broker rejected execution currency context: {exc}"
            logger.warning(
                message,
                block_reason="execution_fx_unavailable",
                **self.resolved.trace_ctx,
            )
            return self._short_circuit(
                execution_mode="blocked",
                error_message=message,
                block_reason="execution_fx_unavailable",
            )
        return None

    def _seed_paper_price(self, broker: BrokerAdapter) -> None:
        price_hint = (
            self.target_position_override.reference_price
            if self.target_position_override is not None
            else self.market_data.get("price") or self.resolved.signal.entry_price
        )
        if (
            isinstance(price_hint, bool)
            or not isinstance(price_hint, (Decimal, float, int, str))
            or not price_hint
            or not hasattr(broker, "set_price")
        ):
            return
        try:
            broker.set_price(self.resolved.signal.symbol, float(price_hint))
        except (ValueError, TypeError, AttributeError):
            logger.warning("Failed to seed broker price", **self.resolved.trace_ctx)

    def _apply_pre_trade_risk(self) -> ExecutionResult | None:
        if not self.services.risk_guard_enabled:
            return None
        resolved = self.resolved
        account_state = cast(AccountState, self.account_state)
        broker = cast(BrokerAdapter, self.broker)
        try:
            equity_observation = _account_equity_observation(resolved, account_state)
            risk_baseline = resolve_risk_baseline(
                self.services.session_factory,
                user_id=resolved.user_id,
                account_id=resolved.broker_account_id,
                current_equity=float(account_state.equity),
                equity_observation=equity_observation,
            )
            _require_current_equity_observation(risk_baseline, equity_observation)
        except (RiskBaselineUnavailableError, SQLAlchemyError):
            logger.exception(
                "Failed to resolve risk baseline",
                **resolved.trace_ctx,
            )
            return self._short_circuit(
                execution_mode="blocked",
                error_message="Risk baseline unavailable; blocking execution",
                block_reason="risk_baseline_unavailable",
            )
        self.profile = {
            **self.profile,
            "day_start_equity": risk_baseline.day_start_equity,
            "peak_equity": risk_baseline.peak_equity,
            "risk_baseline_has_persisted_account_peak": (risk_baseline.has_persisted_account_peak),
            "risk_baseline_current_equity_from_broker": (equity_observation.source == "broker"),
            "_risk_baseline_audit": risk_baseline.to_audit_dict(),
        }
        decision = self.services.risk_guard.evaluate(
            user_id=resolved.user_id,
            signal=resolved.signal,
            profile=self.profile,
            user_strategy_config=resolved.user_strategy_config,
            account_state=account_state,
            execution_mode=resolved.exec_mode,
            broker_capabilities=broker.capabilities,
            intents=self.intents,
            market_data=self.market_data,
            close_quantity_override=self.close_quantity_override,
            target_position_override=self.target_position_override,
        )
        if decision.allowed:
            return None
        decision.context.setdefault("broker_account_id", resolved.broker_account_id)
        decision.context.setdefault(
            "risk_baseline",
            self.profile.get("_risk_baseline_audit"),
        )
        self.services.risk_guard.persist(user_id=resolved.user_id, decision=decision)
        return self.services.short_circuit_result(
            dedup_key=resolved.dedup_key,
            user_id=resolved.user_id,
            signal=resolved.signal,
            score_context=resolved.score_context,
            profile=self.profile,
            signal_id=resolved.signal_id,
            symbol=resolved.signal.symbol,
            execution_mode="blocked",
            broker=resolved.broker_type.value,
            error_message=decision.message or "Risk guard blocked execution",
            account_state=account_state,
            intents=self.intents,
            broker_account_id=resolved.broker_account_id,
            settlement_currency=resolved.settlement_currency,
        )

    def _no_intent_outcome(self) -> ExecutionResult | None:
        if self.intents:
            return None
        resolved = self.resolved
        reason = (
            "close_no_position"
            if resolved.signal.action == SignalAction.CLOSE
            else "no_order_built"
        )
        logger.info(
            "No order intents generated for signal",
            no_intent_reason=reason,
            **resolved.trace_ctx,
        )
        return self.services.short_circuit_result(
            dedup_key=resolved.dedup_key,
            user_id=resolved.user_id,
            signal=resolved.signal,
            score_context=resolved.score_context,
            profile=self.profile,
            signal_id=resolved.signal_id,
            symbol=resolved.signal.symbol,
            execution_mode=resolved.exec_mode.value,
            broker=resolved.broker_type.value,
            error_message=None,
            success=True,
            reason=reason,
            broker_account_id=resolved.broker_account_id,
            settlement_currency=resolved.settlement_currency,
        )

    async def _refresh_account_state(self, broker: BrokerAdapter) -> AccountState:
        resolved = self.resolved
        current = cast(AccountState, self.account_state)
        try:
            return await self.services.get_account_state(
                resolved.user_id,
                broker,
                self.profile,
                allow_profile_fallback=resolved.environment != "live",
            )
        except RuntimeError as exc:
            self.services.alerts.publish(
                ExecutionAlert(
                    event_type="post_trade_account_state_unavailable",
                    severity="error",
                    message=str(exc),
                    payload={
                        "user_id": resolved.user_id,
                        "strategy_id": resolved.signal.strategy_id,
                        "broker": resolved.broker_type.value,
                        "environment": resolved.environment,
                    },
                )
            )
            return current

    def _evaluate_post_trade_risk(
        self,
        result: ExecutionResult,
        account_state: AccountState,
    ) -> None:
        if not result.success or not self.services.risk_guard_enabled:
            return
        resolved = self.resolved
        decision = self.services.risk_guard.evaluate_post_trade(
            user_id=resolved.user_id,
            profile=self.profile,
            user_strategy_config=resolved.user_strategy_config,
            account_state=account_state,
        )
        if decision.allowed:
            return
        decision.context.setdefault("broker_account_id", resolved.broker_account_id)
        self.services.risk_guard.persist(user_id=resolved.user_id, decision=decision)
        self.services.alerts.publish(
            ExecutionAlert(
                event_type="post_trade_compliance_breach",
                severity="error",
                message=decision.message or "Post-trade compliance breach",
                payload=decision.context,
            )
        )

    def _record_execution_health(
        self,
        result: ExecutionResult,
        order_results: list[BrokerOrderResult],
    ) -> None:
        resolved = self.resolved
        if result.success:
            self.services.breaker_manager.record_success(
                resolved.strategy_breaker_key,
                resolved.broker_breaker_key,
            )
            return
        rejected = [item for item in order_results if item.is_rejected]
        if not rejected:
            return
        global_failure = any(
            self.services.breaker_manager.is_broker_system_error(
                error_message=item.error_message,
                error_code=item.error_code,
            )
            for item in rejected
        )
        self.services.breaker_manager.record_failure(
            scope="broker" if global_failure else "strategy",
            breaker_key=(
                resolved.broker_breaker_key if global_failure else resolved.strategy_breaker_key
            ),
            user_id=resolved.user_id,
            strategy_id=resolved.signal.strategy_id,
            broker=resolved.broker_type.value,
            environment=resolved.environment,
            reason=result.error_message or "broker_reject",
            broker_account_id=resolved.broker_account_id,
        )

    async def _submit_and_finalize(self) -> ExecutionResult:
        resolved = self.resolved
        broker = cast(BrokerAdapter, self.broker)
        authority_error = await self._revalidate_current_authority(
            submission_boundary=True,
        )
        if authority_error is not None:
            return authority_error
        order_results = await self.services.execute_orders(
            broker,
            self.intents,
            breaker_key=resolved.strategy_breaker_key,
            user_id=resolved.user_id,
            signal=resolved.signal,
            execution_mode=resolved.exec_mode.value,
            broker_code=resolved.broker_type.value,
            broker_environment=resolved.environment,
            credential_ref=resolved.credential_ref,
            run_id=resolved.run_id,
            broker_account_id=resolved.broker_account_id,
            settlement_currency=resolved.settlement_currency,
            execution_key=resolved.dedup_key,
        )
        post_account_state = await self._refresh_account_state(broker)
        result = self.services.aggregate_results(
            signal_id=resolved.signal_id,
            symbol=resolved.signal.symbol,
            exec_mode=resolved.exec_mode,
            broker_type=resolved.broker_type,
            order_results=order_results,
            broker_account_id=resolved.broker_account_id,
            settlement_currency=resolved.settlement_currency,
        )
        self._evaluate_post_trade_risk(result, post_account_state)
        self._record_execution_health(result, order_results)
        final_result = self.services.finalize_result(
            resolved.dedup_key,
            resolved.user_id,
            resolved.signal,
            result,
            resolved.score_context,
            account_state=post_account_state,
            profile=self.profile,
            intents=self.intents,
        )
        await self.services.update_daily_nav_snapshot(
            user_id=resolved.user_id,
            account_state=post_account_state,
            account_id=resolved.broker_account_id,
        )
        return final_result

    async def run(self) -> ExecutionResult:
        """Run each stage, returning immediately on a deterministic block."""
        outcome = await self._prepare_broker_and_account()
        if outcome is not None:
            return outcome
        outcome = await self._prepare_market_and_intents()
        if outcome is not None:
            return outcome
        outcome = self._apply_pre_trade_risk()
        if outcome is not None:
            return outcome
        outcome = self._no_intent_outcome()
        if outcome is not None:
            return outcome
        return await self._submit_and_finalize()


async def execute_resolved_signal(
    services: ExecutionServices,
    resolved: ResolvedDispatch,
    *,
    close_quantity_override: CloseQuantityOverride | None = None,
    target_position_override: TargetPositionQuantityOverride | None = None,
) -> ExecutionResult:
    """Execute a fully-gated dispatch through the staged production flow."""
    return await _ResolvedExecutionFlow(
        services,
        resolved,
        close_quantity_override=close_quantity_override,
        target_position_override=target_position_override,
    ).run()
