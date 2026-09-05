"""Reconciliation context + pending-order tracking for the execution engine.

Extracted from ``apps/execution_engine/execution_engine/engine.py`` as the
fifth step of the Phase-3 ExecutionEngine decomposition. This module owns the
mutable state and operations that the reconciliation worker depends on:

* **Reconciliation contexts** — per-breaker-key bundle of
  ``(user_id, strategy_id, broker_code, environment, credential_ref,
  profile, user_strategy_config, credentials)`` recorded each time a request
  successfully resolved a broker route. The reconciliation worker iterates
  these contexts to know which broker accounts to reconcile.
* **Pending orders** — orders that have been persisted to the
  ``pending_orders`` table but have not yet been resolved. Combined view
  layers the in-memory cache on top of the DB-backed
  :class:`PendingOrderRepository`.
* **Health** — last reconciliation outcome (healthy / error) plus timestamp.
  Exposed via :py:meth:`status` for the FastAPI health probe and
  :py:meth:`is_healthy` for the dispatch path.

``ExecutionEngine`` exposes the tracker health and read-side methods needed by
the reconciliation worker. The execution path writes contexts and pending
orders through this collaborator directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, func

from lib_strategy.signals.signal import Signal

from .brokers.base import BrokerFill, BrokerOrderResult
from .circuit_breaker_manager import CircuitBreakerManager
from .models import OptionsIntent, OrderIntent
from .pending_orders import PendingOrderPersistenceError, PendingOrderRepository

#: ``CircuitBreakerManager.strategy_key`` format:
#: ``strategy:{user_id}:{strategy_id}:{broker_code}:{credential_ref}``.
_STRATEGY_BREAKER_KEY_PARTS = 5


def _strategy_id_from_breaker_key(breaker_key: str) -> str:
    """Recover the strategy_id embedded in a strategy breaker key."""
    parts = breaker_key.split(":", _STRATEGY_BREAKER_KEY_PARTS - 1)
    if len(parts) == _STRATEGY_BREAKER_KEY_PARTS and parts[0] == "strategy":
        return parts[2] or "unknown"
    return "unknown"


class ReconciliationTracker:
    """Owns reconciliation contexts, pending-order cache, and health state."""

    def __init__(
        self,
        *,
        pending_order_repo: PendingOrderRepository,
        canonical_execution_store: Any | None = None,
        session_factory: Any | None = None,
    ) -> None:
        self._pending_order_repo = pending_order_repo
        self._canonical_execution_store = canonical_execution_store
        self._session_factory = session_factory
        self._reconciliation_contexts: dict[str, dict[str, Any]] = {}
        self._pending_orders: dict[str, dict[str, Any]] = {}
        self._reconciliation_healthy = False
        self._initial_reconciliation_complete = False
        self._last_reconciled_at: datetime | None = None
        self._last_reconciliation_error: str | None = None
        # Hydrate the in-memory cache from the persisted table.
        self.restore_pending_orders()
        # Rebuild reconciliation contexts from the restored rows so the worker
        # is not blind after a restart (contexts are otherwise registered only
        # when an order executes — until then, stale pending orders were never
        # re-checked or terminal-synced).
        self.restore_contexts_from_durable_state()

    # ------------------------------------------------------------------
    # Reconciliation contexts
    # ------------------------------------------------------------------

    def track_context(
        self,
        *,
        breaker_key: str,
        user_id: str,
        strategy_id: str,
        broker_code: str,
        environment: str,
        credential_ref: str,
        profile: dict[str, Any],
        user_strategy_config: dict[str, Any],
        credentials: dict[str, str] | None,
        broker_account_id: int | None = None,
        settlement_currency: str | None = None,
    ) -> None:
        """Record a reconciliation context against ``breaker_key``."""
        self._reconciliation_contexts[breaker_key] = {
            "breaker_key": breaker_key,
            "strategy_breaker_key": breaker_key,
            "broker_breaker_key": CircuitBreakerManager.broker_key(
                broker_code=broker_code,
                environment=environment,
            ),
            "user_id": user_id,
            "strategy_id": strategy_id,
            "broker_code": broker_code,
            "environment": environment,
            "credential_ref": credential_ref,
            "profile": dict(profile or {}),
            "user_strategy_config": dict(user_strategy_config or {}),
            "credentials": dict(credentials or {}),
            "broker_account_id": broker_account_id,
            "settlement_currency": settlement_currency,
        }

    def iter_contexts(self) -> list[dict[str, Any]]:
        # Reconciliation is account-level. Traffic can register a newer
        # strategy context for an account already discovered at startup; prefer
        # the richer/latest context and never reconcile the same broker book
        # twice in one cycle.
        by_account: dict[tuple[str, int, str, str], dict[str, Any]] = {}
        legacy_contexts: list[dict[str, Any]] = []
        for context in self._reconciliation_contexts.values():
            raw_account_id = context.get("broker_account_id")
            if raw_account_id is None:
                # Rows created before broker-account attribution was mandatory
                # still need terminal synchronization. They cannot be safely
                # collapsed into an account partition, so retain one context
                # per breaker key until those rows drain.
                legacy_contexts.append(context)
                continue
            key = (
                str(context.get("user_id") or ""),
                int(raw_account_id),
                str(context.get("broker_code") or ""),
                str(context.get("environment") or ""),
            )
            current = by_account.get(key)
            current_score = bool((current or {}).get("profile"))
            candidate_score = bool(context.get("profile"))
            if current is None or candidate_score >= current_score:
                by_account[key] = context
        return [*by_account.values(), *legacy_contexts]

    def restore_contexts_from_durable_state(self) -> None:
        """Discover paper accounts from bindings, canonical fills, and recoverable orders."""
        if self._session_factory is None:
            self.restore_contexts_from_pending_orders()
            return

        from lib_application.db.models import (  # noqa: I001, PLC0415
            Broker,
            BrokerCredential,
            Execution,
            Instrument,
            LinkedBrokerAccount,
            Order,
            OrderIntent as CanonicalOrderIntent,
            PendingOrder,
            UserStrategyBinding,
        )
        from lib_common.time_utils import now_utc  # noqa: PLC0415

        recoverable_statuses = (
            "pending",
            "submission_unknown",
            "submitted",
            "working",
            "partially_filled",
        )
        now_naive = now_utc().replace(tzinfo=None)
        with self._session_factory() as session:
            accounts = (
                session.query(LinkedBrokerAccount, Broker)
                .join(Broker, Broker.broker_id == LinkedBrokerAccount.broker_id)
                .filter(
                    LinkedBrokerAccount.environment == "paper",
                    LinkedBrokerAccount.status == "connected",
                )
                .all()
            )
            for account, broker in accounts:
                active_binding = (
                    session.query(UserStrategyBinding)
                    .filter(
                        UserStrategyBinding.broker_account_id == account.account_id,
                        UserStrategyBinding.is_active.is_(True),
                    )
                    .order_by(UserStrategyBinding.binding_id.asc())
                    .first()
                )
                signed_quantity = func.sum(
                    case(
                        (
                            CanonicalOrderIntent.side == "BUY",
                            Execution.qty,
                        ),
                        else_=-Execution.qty,
                    )
                )
                canonical_position = (
                    session.query(
                        Execution.instr_id,
                        Instrument.settlement_currency,
                        signed_quantity.label("net_quantity"),
                    )
                    .join(Order, Order.order_id == Execution.order_id)
                    .join(
                        CanonicalOrderIntent,
                        CanonicalOrderIntent.intent_id == Order.intent_id,
                    )
                    .join(Instrument, Instrument.instr_id == Execution.instr_id)
                    .filter(
                        Order.account_id == account.account_id,
                    )
                    .group_by(Execution.instr_id, Instrument.settlement_currency)
                    .having(signed_quantity != 0)
                    .order_by(Execution.instr_id.asc())
                    .first()
                )
                pending = (
                    session.query(PendingOrder)
                    .filter(
                        PendingOrder.broker_account_id == account.account_id,
                        PendingOrder.status.in_(recoverable_statuses),
                    )
                    .order_by(PendingOrder.created_at.asc())
                    .first()
                )
                if active_binding is None and canonical_position is None and pending is None:
                    continue

                strategy_id = (
                    str(active_binding.strategy_id)
                    if active_binding is not None and active_binding.strategy_id
                    else (
                        str(pending.strategy_id)
                        if pending is not None and pending.strategy_id
                        else "account-recovery"
                    )
                )
                credentials = (
                    session.query(BrokerCredential)
                    .filter(BrokerCredential.account_id == account.account_id)
                    .all()
                )
                usable = [
                    item
                    for item in credentials
                    if str(item.status) == "active"
                    and (
                        item.expires_at is None or item.expires_at.replace(tzinfo=None) > now_naive
                    )
                ]
                broker_code = str(broker.code).strip().lower()
                if len(usable) == 1:
                    credential_ref = str(usable[0].secret_ref)
                elif not credentials and broker_code == "paper":
                    credential_ref = "paper-paper"
                else:
                    credential_ref = f"__unresolved__:{broker_code}:paper"

                settlement_currency = str(account.base_ccy).strip().upper()
                if canonical_position is not None:
                    settlement_currency = (
                        str(canonical_position.settlement_currency).strip().upper()
                    )
                elif pending is not None and pending.settlement_currency:
                    settlement_currency = str(pending.settlement_currency).strip().upper()

                breaker_key = CircuitBreakerManager.strategy_key(
                    user_id=str(account.user_id),
                    strategy_id=strategy_id,
                    broker_code=broker_code,
                    credential_ref=credential_ref,
                )
                account_profile = {
                    "account_id": int(account.account_id),
                    "broker": broker_code,
                    "environment": "paper",
                    "status": str(account.status),
                    "base_ccy": str(account.base_ccy),
                    "paper_initial_equity": (
                        float(account.paper_initial_equity)
                        if account.paper_initial_equity is not None
                        else None
                    ),
                    "paper_initial_cash": (
                        float(account.paper_initial_cash)
                        if account.paper_initial_cash is not None
                        else None
                    ),
                    "credential_ref": credential_ref,
                }
                self.track_context(
                    breaker_key=breaker_key,
                    user_id=str(account.user_id),
                    strategy_id=strategy_id,
                    broker_code=broker_code,
                    environment="paper",
                    credential_ref=credential_ref,
                    profile={
                        "accounts": {str(account.account_id): account_profile},
                        "brokers": {broker_code: account_profile},
                    },
                    user_strategy_config={
                        "binding_id": (
                            int(active_binding.binding_id) if active_binding is not None else None
                        )
                    },
                    credentials=None,
                    broker_account_id=int(account.account_id),
                    settlement_currency=settlement_currency,
                )
        # Retain recovery visibility for legacy/orphan rows while database
        # constraints are being tightened. Durable account discovery above is
        # authoritative; this fallback cannot replace an already-built context.
        self.restore_contexts_from_pending_orders()

    def restore_contexts_from_pending_orders(self) -> None:
        """Synthesize contexts for restored pending orders after a restart.

        The pending-order rows carry everything ``get_reconciliation_broker``
        needs (user, broker, environment, credential_ref); the credential dict
        itself is not persisted, so LIVE contexts are deliberately NOT
        rehydrated — resolving a live broker without its credentials would fail
        every cycle and flip reconciliation unhealthy (blocking the live gate).
        Live rows terminal-sync once real live traffic re-registers a context
        with working credentials.
        """
        for payload in self._pending_orders.values():
            breaker_key = str(payload.get("breaker_key") or "")
            if not breaker_key or breaker_key in self._reconciliation_contexts:
                continue
            environment = str(payload.get("broker_environment") or "paper").lower()
            if environment == "live":
                continue
            broker_code = str(payload.get("broker") or "")
            user_id = str(payload.get("user_id") or "")
            if not broker_code or not user_id:
                continue
            self.track_context(
                breaker_key=breaker_key,
                user_id=user_id,
                strategy_id=_strategy_id_from_breaker_key(breaker_key),
                broker_code=broker_code,
                environment=environment,
                credential_ref=str(payload.get("credential_ref") or ""),
                profile={},
                user_strategy_config={},
                credentials=None,
                broker_account_id=payload.get("broker_account_id"),
                settlement_currency=payload.get("settlement_currency"),
            )

    # ------------------------------------------------------------------
    # Pending-order cache (DB-backed + in-memory layer)
    # ------------------------------------------------------------------

    def list_pending_orders(self) -> dict[str, dict[str, Any]]:
        """Return every pending order — DB rows merged with the in-memory cache."""
        orders = self.load_pending_orders_from_db()
        orders.update(self._pending_orders)
        return orders

    def pending_orders_for(self, breaker_key: str) -> dict[str, dict[str, Any]]:
        """Pending orders restricted to a single breaker key."""
        orders = self.load_pending_orders_from_db(breaker_key=breaker_key)
        orders.update(
            {
                order_id: payload
                for order_id, payload in self._pending_orders.items()
                if payload.get("breaker_key") == breaker_key
            }
        )
        return orders

    def pending_orders_for_account(self, broker_account_id: int) -> dict[str, dict[str, Any]]:
        """Recoverable orders for an account, independent of in-memory breaker keys."""
        return {
            order_id: payload
            for order_id, payload in self.list_pending_orders().items()
            if int(payload.get("broker_account_id") or 0) == int(broker_account_id)
        }

    def restore_pending_orders(self) -> None:
        """Hydrate the in-memory cache from the pending-orders table."""
        self._pending_orders = self.load_pending_orders_from_db()

    def load_pending_orders_from_db(
        self,
        *,
        breaker_key: str | None = None,
        statuses: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        return self._pending_order_repo.load(breaker_key=breaker_key, statuses=statuses)

    def persist_order(
        self,
        *,
        order_id: str,
        user_id: str,
        signal: Signal,
        intent: OrderIntent | OptionsIntent,
        execution_mode: str,
        broker_code: str,
        settlement_currency: str,
        breaker_key: str | None = None,
        broker_environment: str | None = None,
        credential_ref: str | None = None,
        run_id: str | None = None,
        idempotency_key: str | None = None,
        broker_account_id: int,
        canonical_order_id: int | None = None,
    ) -> str:
        """Persist an order and confirm its durable local identity."""
        persisted_order_id = self._pending_order_repo.persist(
            order_id=order_id,
            user_id=user_id,
            signal=signal,
            intent=intent,
            execution_mode=execution_mode,
            broker_code=broker_code,
            settlement_currency=settlement_currency,
            breaker_key=breaker_key,
            broker_environment=broker_environment,
            credential_ref=credential_ref,
            run_id=run_id,
            idempotency_key=idempotency_key,
            broker_account_id=broker_account_id,
            canonical_order_id=canonical_order_id,
        )
        if persisted_order_id != order_id:
            msg = (
                f"Pending-order persistence returned {persisted_order_id!r}; expected {order_id!r}"
            )
            raise PendingOrderPersistenceError(msg)
        return persisted_order_id

    def resolve_order(
        self,
        order_id: str,
        result: BrokerOrderResult,
        *,
        fills: list[BrokerFill],
        canonical_order_id: int | None = None,
        instr_id: int | None = None,
    ) -> str:
        """Commit exact canonical fills before closing recovery state.

        The canonical store is idempotent for repeated venue trade IDs. Persist
        them first so a crash or database error cannot mark the
        pending row terminal while leaving the append-only fill ledger empty.
        If the later pending-order update fails, reconciliation can safely
        replay the canonical result and retry the still-recoverable row.
        """
        if self._canonical_execution_store is not None:
            if canonical_order_id is None:
                msg = "Canonical fill persistence requires canonical_order_id"
                raise RuntimeError(msg)
            self._canonical_execution_store.apply_broker_result(
                order_id=canonical_order_id,
                result=result,
                fills=fills,
                instr_id=instr_id,
            )
        resolved_order_id = self._pending_order_repo.resolve(order_id, result)
        if not resolved_order_id:
            msg = f"Pending-order resolution for {order_id!r} was not confirmed"
            raise PendingOrderPersistenceError(msg)
        return resolved_order_id

    def cancel_working_order(self, order_id: str, *, reason: str) -> bool:
        """Durably cancel a superseded resting order and drop it from the cache."""
        if not self._pending_order_repo.enabled:
            return False
        cancelled = self._pending_order_repo.cancel_working(order_id, reason=reason)
        if cancelled:
            self._pending_orders.pop(order_id, None)
        return cancelled

    def mark_submission_unknown(
        self,
        order_id: str,
        *,
        reason: str,
        client_order_id: str,
    ) -> bool:
        """Keep an ambiguous submit recoverable under its stable client identity."""
        marked = self._pending_order_repo.mark_submission_unknown(
            order_id,
            reason=reason,
            client_order_id=client_order_id,
        )
        if marked:
            payload = dict(self._pending_orders.get(order_id) or {})
            payload.update(
                {
                    "order_id": order_id,
                    "client_order_id": client_order_id,
                    "status": "submission_unknown",
                    "error_message": reason,
                }
            )
            self._pending_orders[order_id] = payload
        return marked

    # ------------------------------------------------------------------
    # Direct state access used by OrderExecutor and reconciliation.
    # ------------------------------------------------------------------

    @property
    def pending_orders_cache(self) -> dict[str, dict[str, Any]]:
        """Mutable in-memory cache of pending orders (engine-internal)."""
        return self._pending_orders

    @property
    def persistence_enabled(self) -> bool:
        """Whether broker submission is coupled to durable order persistence."""
        return self._canonical_execution_store is not None or self._pending_order_repo.enabled

    @property
    def canonical_execution_store(self) -> Any | None:
        """Expose the canonical read/write boundary to the reconciliation worker."""
        return self._canonical_execution_store

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def mark_health(self, *, healthy: bool, error: str | None = None) -> None:
        self._initial_reconciliation_complete = True
        self._reconciliation_healthy = healthy
        self._last_reconciled_at = datetime.now(tz=UTC)
        self._last_reconciliation_error = error

    def is_healthy(self) -> bool:
        return self._initial_reconciliation_complete and self._reconciliation_healthy

    def status(self) -> dict[str, Any]:
        return {
            "healthy": self.is_healthy(),
            "initial_reconciliation_complete": self._initial_reconciliation_complete,
            "discovered_partitions": len(self.iter_contexts()),
            "last_reconciled_at": (
                self._last_reconciled_at.isoformat() if self._last_reconciled_at else None
            ),
            "last_error": self._last_reconciliation_error,
        }
