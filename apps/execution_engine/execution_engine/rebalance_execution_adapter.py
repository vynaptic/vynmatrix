# ruff: noqa: EM101
"""Adapter from durable account plans to the established signal execution path."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Protocol

from sqlalchemy.exc import SQLAlchemyError

from lib_common.internal_events import RebalanceExecutionCommandEvent
from lib_strategy.signals.signal import Signal

from ._dispatch import (
    DispatchOutcome,
    DispatchResolutionError,
    ResolvedDispatch,
    build_dispatch_context,
)
from ._execute import _resolve_order_currency_context
from ._services import ExecutionServices
from .config import AccountState, BrokerType
from .execution_result import RETRYABLE_BLOCK_REASONS, ExecutionResult
from .execution_routing import CurrentAuthorityError
from .metrics.fx_rates import FXRateUnavailableError
from .models import (
    CloseQuantityOverride,
    ExecutionRequest,
    TargetPositionQuantityOverride,
)
from .order_builder import OrderSizingUnavailableError
from .rebalance_store import RebalancePlanProgressSnapshot

_DRIFT_INVALID_CODE = "target_drift_policy_invalid"
_DRIFT_MISSING_CODE = "target_drift_policy_missing"
_ENTRY_CASH_BUFFER_BPS_KEY = "entry_cash_buffer_bps"
_BASIS_POINTS = Decimal("10000")


def _floor_to_lot_size(quantity: Decimal, lot_size: Decimal) -> Decimal:
    """Return a nonnegative target that never exceeds its requested notional."""

    if not quantity.is_finite() or quantity < 0 or not lot_size.is_finite() or lot_size <= 0:
        raise ValueError("Target quantity and catalogue lot size must be finite and nonnegative")
    lots = (quantity / lot_size).to_integral_value(rounding=ROUND_FLOOR)
    return lots * lot_size


def _ibkr_lot_sized_target(resolved: ResolvedDispatch, quantity: Decimal) -> Decimal:
    if resolved.broker_type is not BrokerType.IBKR:
        return quantity
    identity = resolved.broker_instrument
    if identity is None or identity.lot_size is None:
        raise ValueError("IBKR portfolio targets require an exact catalogue lot_size")
    return _floor_to_lot_size(quantity, identity.lot_size)


class RebalanceExecutionError(RuntimeError):
    """A plan cannot safely continue."""

    def __init__(self, code: str, detail: str, *, retryable: bool) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.retryable = retryable


def execution_error(
    code: str,
    detail: str,
    *,
    retryable: bool,
) -> RebalanceExecutionError:
    return RebalanceExecutionError(code, detail, retryable=retryable)


@dataclass(frozen=True)
class RebalanceProcessResult:
    """HTTP-facing durable plan outcome."""

    progress: RebalancePlanProgressSnapshot
    acquired: bool
    retryable: bool

    def to_dict(self) -> dict[str, object]:
        return {
            **self.progress.to_dict(),
            "acquired": self.acquired,
            "retryable": self.retryable,
        }


class HandleSignal(Protocol):
    """Internal call shape retained by ``ExecutionEngine.handle_signal``."""

    async def __call__(
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
    ) -> ExecutionResult: ...


class RebalanceExecutionAdapter(Protocol):
    """Existing execution operations used by the durable state machine."""

    async def authoritative_account_state(
        self,
        command: RebalanceExecutionCommandEvent,
        signal: Signal,
    ) -> AccountState: ...

    async def strategy_positions(
        self,
        command: RebalanceExecutionCommandEvent,
    ) -> Mapping[str, Decimal]: ...

    def recoverable_account_orders(
        self,
        command: RebalanceExecutionCommandEvent,
    ) -> Mapping[str, Mapping[str, object]]: ...

    async def build_target_override(
        self,
        command: RebalanceExecutionCommandEvent,
        signal: Signal,
        *,
        plan_leg_id: str,
        target_allocation: Decimal,
        account: AccountState,
        strategy_quantity: Decimal,
        broker_quantity: Decimal,
    ) -> TargetPositionQuantityOverride: ...

    async def execute(
        self,
        command: RebalanceExecutionCommandEvent,
        signal: Signal,
        *,
        target_allocation: Decimal | None,
        close_quantity_override: CloseQuantityOverride | None,
        target_position_override: TargetPositionQuantityOverride | None,
    ) -> ExecutionResult: ...

    def validate_entry_funding(
        self,
        command: RebalanceExecutionCommandEvent,
        account: AccountState,
        overrides: Sequence[TargetPositionQuantityOverride],
    ) -> None: ...


class ExecutionEngineRebalanceAdapter:
    """Compose batch processing from the typed single-signal service seam."""

    def __init__(
        self,
        services: ExecutionServices,
        handle_signal: HandleSignal,
    ) -> None:
        self._services = services
        self._handle_signal = handle_signal

    async def authoritative_account_state(
        self,
        command: RebalanceExecutionCommandEvent,
        signal: Signal,
    ) -> AccountState:
        """Resolve current authority and fetch broker state without fallback."""
        attempt_signal = self._execution_signal(command, signal)
        self._validate_execution_session(command, attempt_signal)
        profile, config = self._execution_inputs(command)
        resolved = self._resolve_dispatch(
            command,
            attempt_signal,
            profile=profile,
            config=config,
            purpose="account-refresh",
        )
        self._validate_paper_resolution(command, resolved)
        try:
            authority = self._services.route_resolver.validate_current_rebalance_account_route(
                user_id=resolved.user_id,
                binding_id=command.binding_id,
                strategy_id=resolved.signal.strategy_id,
                account_id=resolved.broker_account_id,
                broker_type=resolved.broker_type,
                environment=resolved.environment,
                credential_ref=resolved.credential_ref,
                instrument_id=resolved.signal.instrument_id,
            )
            await self._services.broker_resolver.ensure_current_authority(
                user_id=resolved.user_id,
                broker_account_id=resolved.broker_account_id,
                credential_version=authority.credential_version,
            )
            broker = await self._services.get_broker(
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
            if broker is None:
                msg = "Paper broker adapter is unavailable"
                raise RuntimeError(msg)  # noqa: TRY301
            if not broker.is_connected and not await broker.connect():
                msg = "Paper broker connection failed"
                raise RuntimeError(msg)  # noqa: TRY301
            account = await self._services.get_account_state(
                resolved.user_id,
                broker,
                profile,
                allow_profile_fallback=False,
            )
        except CurrentAuthorityError as exc:
            raise execution_error(
                "current_authorization_rejected",
                str(exc),
                retryable=False,
            ) from exc
        except (OSError, RuntimeError, SQLAlchemyError, TimeoutError) as exc:
            raise execution_error(
                "account_state_unavailable",
                str(exc),
                retryable=True,
            ) from exc
        freshness_error = self._services.gatekeeper.check_account_state_freshness(
            account_state=account,
            environment=resolved.environment,
            trace_ctx=resolved.trace_ctx,
            require_fresh=resolved.broker_type is BrokerType.IBKR,
        )
        if freshness_error is not None:
            code, detail = freshness_error
            raise execution_error(code, detail, retryable=True)
        return account

    async def strategy_positions(
        self,
        command: RebalanceExecutionCommandEvent,
    ) -> Mapping[str, Decimal]:
        try:
            return await self._services.pnl_service.get_fifo_positions(
                command.user_id,
                account_id=command.broker_route.broker_account_id,
                strategy_id=command.strategy_id,
                broker=command.broker_route.broker,
                mode=command.broker_route.broker_environment,
            )
        except (OSError, RuntimeError, SQLAlchemyError, TimeoutError) as exc:
            raise execution_error(
                "strategy_fifo_unavailable",
                str(exc),
                retryable=True,
            ) from exc

    def recoverable_account_orders(
        self,
        command: RebalanceExecutionCommandEvent,
    ) -> Mapping[str, Mapping[str, object]]:
        """Return every nonterminal order that may change the account later."""
        return self._services.reconciliation_tracker.pending_orders_for_account(
            command.broker_route.broker_account_id
        )

    async def build_target_override(
        self,
        command: RebalanceExecutionCommandEvent,
        signal: Signal,
        *,
        plan_leg_id: str,
        target_allocation: Decimal,
        account: AccountState,
        strategy_quantity: Decimal,
        broker_quantity: Decimal,
    ) -> TargetPositionQuantityOverride:
        """Size one total target using current account, quote, and FX evidence."""
        attempt_signal = self._execution_signal(command, signal)
        self._validate_execution_session(command, attempt_signal)
        profile, config = self._execution_inputs(
            command,
            target_allocation=target_allocation,
        )
        resolved = self._resolve_dispatch(
            command,
            attempt_signal,
            profile=profile,
            config=config,
            purpose=f"target:{plan_leg_id}",
        )
        self._validate_paper_resolution(command, resolved)
        freshness_error = self._services.gatekeeper.check_account_state_freshness(
            account_state=account,
            environment=resolved.environment,
            trace_ctx=resolved.trace_ctx,
            require_fresh=resolved.broker_type is BrokerType.IBKR,
        )
        if freshness_error is not None:
            code, detail = freshness_error
            raise execution_error(code, detail, retryable=True)
        try:
            market_data = await self._services.get_market_data(
                resolved.signal.symbol,
                user_id=resolved.user_id,
                environment=resolved.environment,
            )
        except (OSError, RuntimeError, SQLAlchemyError, TimeoutError) as exc:
            raise execution_error(
                "market_data_unavailable",
                str(exc),
                retryable=True,
            ) from exc
        market_error = self._services.gatekeeper.check_rebalance_market_data_freshness(
            market_data=market_data,
            expected_open_at=command.execute_not_before,
            environment=resolved.environment,
            trace_ctx=resolved.trace_ctx,
        )
        if market_error is not None:
            code, detail = market_error
            raise execution_error(code, detail, retryable=True)
        quote_observed_at = self._services.gatekeeper.market_data_timestamp(market_data)
        if quote_observed_at is None:
            raise execution_error(
                "rebalance_market_data_timestamp_missing",
                "Portfolio rebalance quote timestamp disappeared after validation",
                retryable=True,
            )
        try:
            reference_price = Decimal(str(market_data["price"]))
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise execution_error(
                "rebalance_market_data_price_invalid",
                "Portfolio rebalance quote price changed after validation",
                retryable=True,
            ) from exc
        try:
            currency_context = _resolve_order_currency_context(
                self._services.fx_rate_provider,
                resolved,
            )
            request = ExecutionRequest(
                user_id=resolved.user_id,
                profile=profile,
                user_strategy_config=config,
                signal=resolved.signal,
                currency_context=currency_context,
            )
            target_quantity = (
                Decimal("0")
                if target_allocation == 0
                else self._services.order_builder.target_position_quantity(
                    request,
                    account=account,
                    market_data=market_data,
                )
            )
            target_quantity = _ibkr_lot_sized_target(resolved, target_quantity)
        except (FXRateUnavailableError, OrderSizingUnavailableError) as exc:
            raise execution_error(
                "target_sizing_state_unavailable",
                str(exc),
                retryable=True,
            ) from exc
        except ValueError as exc:
            raise execution_error(
                "target_sizing_invalid",
                str(exc),
                retryable=False,
            ) from exc
        return TargetPositionQuantityOverride(
            account_plan_id=command.account_plan_id,
            plan_leg_id=plan_leg_id,
            symbol=resolved.signal.symbol,
            target_allocation=target_allocation,
            target_quantity=target_quantity,
            strategy_quantity=strategy_quantity,
            broker_quantity=broker_quantity,
            target_weight_drift_fraction=self._target_drift_fraction(resolved.signal),
            broker_observed_at=account.fetched_at,
            reference_price=reference_price,
            quote_observed_at=quote_observed_at,
            quote_execution_authority=str(market_data.get("execution_authority") or "shared"),
            settlement_currency=resolved.settlement_currency,
        )

    async def execute(
        self,
        command: RebalanceExecutionCommandEvent,
        signal: Signal,
        *,
        target_allocation: Decimal | None,
        close_quantity_override: CloseQuantityOverride | None,
        target_position_override: TargetPositionQuantityOverride | None,
    ) -> ExecutionResult:
        attempt_signal = self._execution_signal(command, signal)
        self._validate_execution_session(command, attempt_signal)
        profile, config = self._execution_inputs(
            command,
            target_allocation=target_allocation,
        )
        if target_position_override is not None:
            quote_payload = {
                "price": str(target_position_override.reference_price),
                "timestamp": target_position_override.quote_observed_at,
                "execution_authority": target_position_override.quote_execution_authority,
            }
            quote_error = self._services.gatekeeper.check_rebalance_market_data_freshness(
                market_data=quote_payload,
                expected_open_at=command.execute_not_before,
                environment=command.broker_route.broker_environment,
                trace_ctx={
                    "user_id": command.user_id,
                    "strategy_id": command.strategy_id,
                    "account_plan_id": command.account_plan_id,
                },
            )
            if quote_error is not None:
                code, detail = quote_error
                raise execution_error(code, detail, retryable=True)
            metadata = {
                **attempt_signal.metadata,
                "rebalance_reference_price": str(target_position_override.reference_price),
                "rebalance_quote_observed_at": (
                    target_position_override.quote_observed_at.isoformat()
                ),
                "rebalance_quote_execution_authority": (
                    target_position_override.quote_execution_authority
                ),
            }
            if attempt_signal.entry_price is not None and attempt_signal.entry_price > 0:
                metadata["decision_to_quote_gap_fraction"] = str(
                    target_position_override.reference_price
                    / Decimal(str(attempt_signal.entry_price))
                    - Decimal("1")
                )
            attempt_signal = replace(attempt_signal, metadata=metadata)
        return await self._handle_signal(
            user_id=command.user_id,
            profile=profile,
            user_strategy_config=config,
            signal=attempt_signal,
            allow_historical_replay=False,
            close_quantity_override=close_quantity_override,
            target_position_override=target_position_override,
        )

    def validate_entry_funding(
        self,
        command: RebalanceExecutionCommandEvent,
        account: AccountState,
        overrides: Sequence[TargetPositionQuantityOverride],
    ) -> None:
        """Require cash-only USD funding for an IBKR paper entry batch."""
        if not overrides:
            return
        route = command.broker_route
        execution_mode = route.execution_mode or command.execution_policy.execution_mode
        if (
            route.broker.strip().lower() != "ibkr"
            or route.broker_environment != "paper"
            or route.live_enabled
            or not route.sandbox
            or str(route.asset_class or "").strip().lower() != "equity"
            or str(execution_mode or "").strip().lower() != "spot"
        ):
            raise execution_error(
                "ibkr_entry_funding_route_invalid",
                "Cash-only entry funding requires an IBKR paper spot-equity route",
                retryable=False,
            )
        if account.broker is not BrokerType.IBKR or account.source != "broker":
            raise execution_error(
                "ibkr_entry_funding_state_unavailable",
                "IBKR paper entries require an authoritative broker account snapshot",
                retryable=True,
            )
        freshness_error = self._services.gatekeeper.check_account_state_freshness(
            account_state=account,
            environment="paper",
            trace_ctx={
                "user_id": command.user_id,
                "strategy_id": command.strategy_id,
                "account_plan_id": command.account_plan_id,
            },
            require_fresh=True,
        )
        if freshness_error is not None:
            code, detail = freshness_error
            raise execution_error(code, detail, retryable=True)

        buffer_bps = self._entry_cash_buffer_bps(command)
        required = Decimal("0")
        for override in overrides:
            if override.delta_quantity <= 0:
                raise execution_error(
                    "ibkr_entry_funding_delta_invalid",
                    "IBKR entry funding may validate only positive target deltas",
                    retryable=False,
                )
            if override.settlement_currency != "USD":
                raise execution_error(
                    "ibkr_entry_settlement_currency_invalid",
                    "IBKR cash-only equity entries require an explicit USD settlement currency",
                    retryable=False,
                )
            required += override.delta_quantity * override.reference_price
        required *= Decimal("1") + buffer_bps / _BASIS_POINTS

        usd_balances = [
            balance
            for balance in account.balances
            if str(balance.get("currency") or "").strip().upper() == "USD"
        ]
        if len(usd_balances) != 1:
            raise execution_error(
                "ibkr_usd_settled_cash_unavailable",
                "IBKR account snapshot must contain exactly one USD settled-cash balance",
                retryable=True,
            )
        balance = usd_balances[0]
        try:
            total = Decimal(str(balance["total"]))
            available = Decimal(str(balance["available"]))
            locked = Decimal(str(balance["locked"]))
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise execution_error(
                "ibkr_usd_settled_cash_unavailable",
                "IBKR USD settled-cash balance is incomplete or invalid",
                retryable=True,
            ) from exc
        if (
            not total.is_finite()
            or not available.is_finite()
            or not locked.is_finite()
            or total < 0
            or available < 0
            or locked < 0
            or available > total
            or locked != total - available
        ):
            raise execution_error(
                "ibkr_usd_settled_cash_unavailable",
                "IBKR USD settled-cash balance is incomplete or invalid",
                retryable=True,
            )
        if required > available:
            raise execution_error(
                "ibkr_usd_settled_cash_insufficient",
                "IBKR USD settled cash cannot fund the buffered entry batch",
                retryable=True,
            )

    @staticmethod
    def _entry_cash_buffer_bps(command: RebalanceExecutionCommandEvent) -> Decimal:
        raw = command.execution_policy.risk_caps.get(_ENTRY_CASH_BUFFER_BPS_KEY)
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise execution_error(
                "ibkr_entry_funding_policy_missing",
                f"IBKR paper entries require risk_caps.{_ENTRY_CASH_BUFFER_BPS_KEY}",
                retryable=False,
            ) from exc
        if not value.is_finite() or not Decimal("0") < value <= Decimal("1000"):
            raise execution_error(
                "ibkr_entry_funding_policy_invalid",
                f"risk_caps.{_ENTRY_CASH_BUFFER_BPS_KEY} must be in (0, 1000]",
                retryable=False,
            )
        return value

    @staticmethod
    def _execution_signal(
        command: RebalanceExecutionCommandEvent,
        signal: Signal,
    ) -> Signal:
        """Create a bounded attempt envelope while preserving decision lineage."""
        authorized_at = datetime.now(tz=UTC)
        if authorized_at < command.execute_not_before:
            raise execution_error(
                "rebalance_not_yet_actionable",
                "Rebalance cannot execute before its pinned official session open",
                retryable=True,
            )
        if authorized_at >= command.expires_at:
            raise execution_error(
                "plan_expired",
                "Rebalance command expired before execution",
                retryable=False,
            )
        metadata = dict(signal.metadata)
        metadata.update(
            {
                "portfolio_rebalance_authorized_at": authorized_at.isoformat(),
                "portfolio_rebalance_data_use_scope": command.data_use_scope,
                "portfolio_rebalance_decision_timestamp": signal.timestamp.isoformat(),
                "portfolio_rebalance_decision_cutoff": command.decision_cutoff.isoformat(),
                "portfolio_rebalance_execute_not_before": (command.execute_not_before.isoformat()),
                "portfolio_rebalance_execution_session_sha256": (command.execution_session_sha256),
                "portfolio_rebalance_provider_authority_sha256": (
                    command.provider_authority_sha256
                ),
            }
        )
        return replace(
            signal,
            timestamp=authorized_at,
            expires_at=command.expires_at,
            metadata=metadata,
        )

    def _resolve_dispatch(
        self,
        command: RebalanceExecutionCommandEvent,
        signal: Signal,
        *,
        profile: dict[str, Any],
        config: dict[str, Any],
        purpose: str,
    ) -> ResolvedDispatch:
        dispatch = build_dispatch_context(
            user_id=command.user_id,
            profile=profile,
            user_strategy_config=config,
            signal=signal,
        )
        try:
            resolution = self._services.dispatch_resolver.resolve(
                dispatch=dispatch,
                user_id=command.user_id,
                dedup_key=f"rebalance-{purpose}:{command.account_plan_id}",
                credentials=None,
                score_context=None,
                allow_historical_replay=False,
            )
        except DispatchResolutionError as exc:
            raise execution_error(
                "account_route_resolution_failed",
                str(exc),
                retryable=isinstance(
                    exc.cause,
                    (OSError, SQLAlchemyError, TimeoutError),
                ),
            ) from exc
        if isinstance(resolution, DispatchOutcome):
            raise execution_error(
                resolution.block_reason or "account_route_blocked",
                resolution.error_message or "Account route did not resolve",
                retryable=resolution.block_reason in RETRYABLE_BLOCK_REASONS,
            )
        return resolution

    @staticmethod
    def _validate_paper_resolution(
        command: RebalanceExecutionCommandEvent,
        resolved: ResolvedDispatch,
    ) -> None:
        route = command.broker_route
        expected_broker_type = {
            "paper": BrokerType.PAPER,
            "ibkr": BrokerType.IBKR,
        }.get(route.broker.strip().lower())
        if (
            command.data_use_scope != "paper_forward"
            or route.broker_environment != "paper"
            or route.live_enabled
            or not route.sandbox
            or resolved.environment != "paper"
            or expected_broker_type is None
            or resolved.broker_type is not expected_broker_type
            or resolved.broker_account_id != route.broker_account_id
            or resolved.user_id != command.user_id
        ):
            raise execution_error(
                "paper_partition_mismatch",
                "Rebalance resolved outside its frozen paper account partition",
                retryable=False,
            )

    def _validate_execution_session(
        self,
        command: RebalanceExecutionCommandEvent,
        signal: Signal,
    ) -> None:
        error = self._services.gatekeeper.check_rebalance_execution_session(
            signal=signal,
            expected_open_at=command.execute_not_before,
            expected_content_sha256=command.execution_session_sha256,
            trace_ctx={
                "account_plan_id": command.account_plan_id,
                "broker_account_id": command.broker_route.broker_account_id,
                "strategy_id": command.strategy_id,
                "user_id": command.user_id,
            },
        )
        if error is None:
            return
        code, detail = error
        raise execution_error(
            code,
            detail,
            retryable=code
            not in {
                "rebalance_execution_session_digest_mismatch",
                "rebalance_execution_session_mismatch",
                "market_session_identity_invalid",
            },
        )

    @staticmethod
    def _target_drift_fraction(signal: Signal) -> Decimal:
        raw_value = signal.metadata.get("target_weight_drift_fraction")
        if raw_value is None or raw_value == "":
            raise execution_error(
                _DRIFT_MISSING_CODE,
                "Portfolio target signal requires target_weight_drift_fraction metadata",
                retryable=False,
            )
        try:
            fraction = Decimal(str(raw_value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise execution_error(
                _DRIFT_INVALID_CODE,
                "Portfolio target signal has invalid target_weight_drift_fraction metadata",
                retryable=False,
            ) from exc
        if not fraction.is_finite() or not 0 <= fraction < 1:
            raise execution_error(
                _DRIFT_INVALID_CODE,
                "Portfolio target signal has invalid target_weight_drift_fraction metadata",
                retryable=False,
            )
        return fraction

    @staticmethod
    def _execution_inputs(
        command: RebalanceExecutionCommandEvent,
        *,
        target_allocation: Decimal | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        route = command.broker_route.model_dump(mode="json")
        policy = command.execution_policy.model_dump(mode="json")
        profile: dict[str, Any] = {
            "broker": command.broker_route.broker,
            "broker_account_id": command.broker_route.broker_account_id,
            "live_enabled": False,
            "sandbox": True,
            "_broker_route_snapshot": route,
            "_execution_policy_snapshot": policy,
        }
        config = dict(command.execution_policy.config)
        config.update(
            {
                "allowed_brokers": list(command.broker_route.allowed_brokers)
                or [command.broker_route.broker],
                "binding_id": command.binding_id,
                "broker": command.broker_route.broker,
                "broker_account_id": command.broker_route.broker_account_id,
                "execution_mode": (
                    command.broker_route.execution_mode
                    or command.execution_policy.execution_mode
                    or "spot"
                ),
                "mode": "paper",
                "risk_caps": dict(command.execution_policy.risk_caps),
                "_causation_event_id": command.event_id,
                "_execution_policy_snapshot": policy,
            }
        )
        sizing = dict(command.execution_policy.sizing)
        if target_allocation is not None:
            if not target_allocation.is_finite() or not 0 <= target_allocation <= 1:
                msg = "Selected target allocation must be finite and in [0, 1]"
                raise ValueError(msg)
            if target_allocation > 0:
                allocation = float(target_allocation)
                configured_cap = sizing.get("max_position_pct")
                effective_cap = (
                    allocation if configured_cap is None else min(allocation, float(configured_cap))
                )
                sizing.update(
                    {
                        "method": "fixed_pct",
                        "fixed_pct": allocation,
                        "max_position_pct": effective_cap,
                    }
                )
        config["sizing"] = sizing
        return profile, config


__all__ = [
    "ExecutionEngineRebalanceAdapter",
    "RebalanceExecutionAdapter",
    "RebalanceExecutionError",
    "RebalanceProcessResult",
    "execution_error",
]
