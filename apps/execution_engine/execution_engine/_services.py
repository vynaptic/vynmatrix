"""Typed collaborator seam between ``ExecutionEngine`` and its execution flow.

``ExecutionServices`` is a frozen snapshot of the collaborators and engine
callbacks that ``_execute.execute_resolved_signal`` operates on. It mirrors
the constructor-injection seam already used by ``_dispatch.DispatchResolver``:
the flow depends on this explicit dataclass contract instead of reaching into
private members of a live engine instance. The engine builds one snapshot per
``handle_signal`` call, so collaborators replaced after construction and the
patchable engine methods (``_get_broker``, ``_get_account_state`` ...) are
captured exactly as bound at call time.

The ``*Port`` protocols beside it are the structural types for the store and
provider dependencies the engine's constructor previously accepted as ``Any``.
Each protocol is minimal: it captures exactly the methods this app calls on
the injected object, so test fakes keep working without inheritance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from contextlib import AbstractContextManager
    from datetime import datetime

    from lib_strategy.signals.signal import Signal

    from ._dispatch import DispatchResolver
    from .alerts import AlertPublisher
    from .broker_resolver import BrokerResolver
    from .brokers.base import BrokerAdapter, BrokerOrderResult
    from .circuit_breaker_manager import CircuitBreakerManager
    from .config import AccountState, BrokerType, ExecutionMode
    from .execution_gates import ExecutionGatekeeper
    from .execution_result import ExecutionResult
    from .execution_routing import ExecutionRouteResolver
    from .metrics.fx_rates import FXRateProvider
    from .metrics.pnl_service import PnLService
    from .models import OptionsIntent, OrderIntent
    from .order_builder import OrderBuilder
    from .reconciliation_tracker import ReconciliationTracker
    from .risk_guard import RiskGuard


# ---------------------------------------------------------------------------
# Structural types for the engine's injected dependencies (formerly ``Any``).
# ---------------------------------------------------------------------------


class SessionFactory(Protocol):
    """Zero-argument factory yielding a context-managed SQLAlchemy session."""

    def __call__(self) -> AbstractContextManager[Any]:
        """Open a new database session usable as a context manager."""


class MarketDataProviderPort(Protocol):
    """Port for the async quote provider used for sizing and freshness gates."""

    def get_quote(
        self,
        symbol: str,
        *,
        user_id: str,
        environment: str,
    ) -> Awaitable[dict[str, Any] | None]:
        """Return the most recent quote payload for ``symbol``, or ``None``."""


class ExecutionLogStorePort(Protocol):
    """Port for the durable execution-log writer and daily-trade counter."""

    def log(
        self,
        *,
        user_id: str,
        account_id: int,
        strategy_id: str,
        canonical_signal_id: int | None = None,
        signal_type: str,
        execution_mode: str,
        status: str,
        details: dict[str, Any],
        error_message: str | None = None,
        run_id: str | None = None,
        outbox_store: Any | None = None,
        event_message: dict[str, Any] | None = None,
    ) -> str:
        """Persist one ``execution_logs`` row, returning its identity."""

    def count_daily_trades(
        self,
        *,
        user_id: str,
        as_of: datetime | None = None,
    ) -> int:
        """Count orders actually submitted today (UTC) for ``user_id``."""


class ExecutionMetricsStorePort(Protocol):
    """Port for the account-scoped execution-metrics projection writer."""

    def record(
        self,
        *,
        user_id: str,
        account_id: int,
        strategy_id: str,
        symbol: str,
        execution_mode: str,
        broker: str,
        settlement_currency: str,
        signal_id: str | None,
        run_id: str | None,
        asset_class: str | None,
        equity: float | None,
        available_cash: float | None,
        margin_used: float | None,
        unrealized_pnl: float | None,
        realized_pnl: float | None,
        position_state_override: dict[str, Any] | None = None,
        signal_action: str | None = None,
        trade_price: float | None = None,
        trade_qty: float | None = None,
        reference_price: float | None = None,
        orders_submitted: int,
        orders_filled: int,
        total_commission: float,
        commission_currency: str | None,
        exposure: float | None = None,
        leverage: float | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: datetime | None = None,
        metric_id: str | None = None,
    ) -> str:
        """Persist one execution metric, returning the stable metric identity."""


class ExecutionPositionStorePort(Protocol):
    """Port for the positions projection (account routing + snapshot sync)."""

    def resolve_account_id(
        self,
        *,
        user_id: str,
        broker_code: str,
        account_id: int,
        environment: str,
    ) -> int:
        """Validate and return the explicitly routed broker account id."""

    def sync_positions(
        self,
        *,
        user_id: str,
        broker_code: str,
        account_id: int,
        environment: str,
        positions: list[dict[str, Any]],
        allow_empty_prune: bool = False,
    ) -> None:
        """Mirror a broker position snapshot into the DB ledger."""


class OutboxStorePort(Protocol):
    """Port for the transactional outbox carrying execution result events."""

    def enqueue(
        self,
        *,
        topic: str,
        event_type: str,
        payload: dict[str, Any],
        schema_version: str = "v1",
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        event_key: str | None = None,
        ordering_key: str | None = None,
        headers: dict[str, Any] | None = None,
        available_at: datetime | None = None,
        max_attempts: int = 10,
    ) -> str:
        """Insert (or idempotently reuse) a pending outbox message."""

    def enqueue_on_session(
        self,
        session: Any,
        *,
        topic: str,
        event_type: str,
        payload: dict[str, Any],
        schema_version: str = "v1",
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        event_key: str | None = None,
        ordering_key: str | None = None,
        headers: dict[str, Any] | None = None,
        available_at: datetime | None = None,
        max_attempts: int = 10,
    ) -> str:
        """Enqueue within the caller's open transaction (no commit/flush)."""


# ---------------------------------------------------------------------------
# Engine callback contracts consumed by the resolved execution flow.
# ---------------------------------------------------------------------------


class GetBrokerFn(Protocol):
    """Resolve a broker adapter for the resolved route (``_get_broker``)."""

    def __call__(
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
    ) -> Awaitable[BrokerAdapter | None]:
        """Return the connected adapter for the route, or ``None``."""


class GetAccountStateFn(Protocol):
    """Fetch broker account state (``_get_account_state``)."""

    def __call__(
        self,
        user_id: str,
        broker: BrokerAdapter,
        profile: dict[str, Any] | None = None,
        allow_profile_fallback: bool = True,
    ) -> Awaitable[AccountState]:
        """Return current account state, optionally profile-seeded."""


class GetMarketDataFn(Protocol):
    """Fetch a market quote for one symbol (``_get_market_data``)."""

    def __call__(
        self,
        symbol: str,
        *,
        user_id: str,
        environment: str,
    ) -> Awaitable[dict[str, Any]]:
        """Return the quote payload for ``symbol`` (``{}`` on failure)."""


class ExecuteOrdersFn(Protocol):
    """Submit built intents to the broker (``_execute_orders``)."""

    def __call__(
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
    ) -> Awaitable[list[BrokerOrderResult]]:
        """Submit each intent and return the per-order broker results."""


class AggregateResultsFn(Protocol):
    """Fold per-order results into one outcome (``_aggregate_results``)."""

    def __call__(
        self,
        signal_id: str,
        symbol: str,
        exec_mode: ExecutionMode,
        broker_type: BrokerType,
        order_results: list[BrokerOrderResult],
        broker_account_id: int | None = None,
        settlement_currency: str | None = None,
    ) -> ExecutionResult:
        """Return the aggregated ``ExecutionResult`` for the submission."""


class ShortCircuitResultFn(Protocol):
    """Build + finalize a no-orders result (``_short_circuit_result``)."""

    def __call__(
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
        """Return the finalized zero-order ``ExecutionResult``."""


class FinalizeResultFn(Protocol):
    """Persist artifacts and mark dedup state (``_finalize_result``)."""

    def __call__(
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
        """Return ``result`` after persistence and dedup bookkeeping."""


class UpdateDailyNavSnapshotFn(Protocol):
    """Persist today's NAV snapshot (``_update_daily_nav_snapshot``)."""

    def __call__(
        self,
        *,
        user_id: str,
        account_state: AccountState | None,
        account_id: int,
    ) -> Awaitable[None]:
        """Persist the account-level NAV snapshot (best-effort)."""


@dataclass(frozen=True)
class ExecutionServices:
    """Everything the post-gating execution flow needs from the engine.

    Collaborators keep their concrete types (matching ``DispatchResolver``);
    engine methods that tests patch on live instances are carried as typed
    callback fields so the flow never touches engine internals directly.
    """

    # Collaborators (constructor-wired engine components).
    risk_guard: RiskGuard
    risk_guard_enabled: bool
    breaker_manager: CircuitBreakerManager
    gatekeeper: ExecutionGatekeeper
    dispatch_resolver: DispatchResolver
    broker_resolver: BrokerResolver
    route_resolver: ExecutionRouteResolver
    reconciliation_tracker: ReconciliationTracker
    alerts: AlertPublisher
    order_builder: OrderBuilder
    session_factory: SessionFactory | None
    fx_rate_provider: FXRateProvider | None
    pnl_service: PnLService
    execution_position_store: ExecutionPositionStorePort | None

    # Engine callbacks (bound methods; patchable in tests).
    get_broker: GetBrokerFn
    get_account_state: GetAccountStateFn
    get_market_data: GetMarketDataFn
    execute_orders: ExecuteOrdersFn
    aggregate_results: AggregateResultsFn
    short_circuit_result: ShortCircuitResultFn
    finalize_result: FinalizeResultFn
    update_daily_nav_snapshot: UpdateDailyNavSnapshotFn
