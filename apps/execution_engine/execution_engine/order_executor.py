"""Broker-side order submission for the execution engine.

Extracted from ``apps/execution_engine/execution_engine/engine.py`` as the
sixth step of the Phase-3 ExecutionEngine decomposition. This module owns the
per-intent broker call: persist a pre-submission row, hand the intent to the
broker adapter, route the response back through the
:class:`ReconciliationTracker`, and update the in-memory pending-order cache
so the reconciliation worker sees the latest state.

The executor deliberately does NOT decide which broker to use
(``BrokerResolver``) and does NOT post-process the run-level
``ExecutionResult`` (``ExecutionPersistence``). Its job is the inner loop:

.. code-block:: text

    for intent in intents:
        persist pending row
        await broker.submit
        resolve pending row
        update in-memory cache for the reconciliation worker

The previous in-place mutation of ``ExecutionEngine._pending_orders`` (a dict
that was aliased onto the tracker's cache as a temporary bridge during the
:class:`ReconciliationTracker` extraction) is now routed through
``ReconciliationTracker.pending_orders_cache``, so the engine no longer needs
that alias.

``ExecutionEngine`` exposes this service through its internal execution boundary
because the extracted orchestration step operates on an engine context.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Protocol, cast

from lib_common.logging import get_logger
from lib_strategy.signals.signal import Signal

from ._persistence_common import _require_positive_account_id
from .account_execution_serializer import (
    AccountExecutionFenceLostError,
    AccountExecutionSerializer,
)
from .brokers.base import BrokerAdapter, BrokerFill, BrokerOrderResult
from .brokers.paper import PaperBroker
from .config import BrokerType, ExecutionMode
from .execution_result import ExecutionResult, aggregate_order_results
from .models import OptionsIntent, OrderIntent, client_order_id_for
from .pending_orders import PendingOrderPersistenceError
from .reconciliation_tracker import ReconciliationTracker

logger = get_logger(__name__)


class _CanonicalExecutionStore(Protocol):
    def create_order(self, **kwargs: Any) -> Any: ...

    def abort_pre_submission(self, **kwargs: Any) -> None: ...

    def mark_submission_unknown(self, **kwargs: Any) -> None: ...

    def read_durable_result(
        self,
        order_id: int,
    ) -> tuple[BrokerOrderResult, list[BrokerFill]] | None: ...


def _instrument_id(signal: Signal | None) -> int | None:
    if signal is None or signal.instrument_id is None:
        return None
    try:
        value = int(signal.instrument_id)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _canonical_signal_id(signal: Signal | None) -> int:
    """Return the required canonical signal identity for order persistence."""
    if signal is None:
        msg = "Canonical order persistence requires an attributed strategy signal"
        raise ValueError(msg)
    metadata = signal.metadata or {}
    if not isinstance(metadata, dict):
        msg = "Signal metadata must be a mapping for canonical persistence"
        raise TypeError(msg)
    raw = metadata.get("canonical_signal_id")
    if raw is None:
        msg = "Canonical order persistence requires canonical_signal_id"
        raise ValueError(msg)
    if isinstance(raw, bool):
        msg = "canonical_signal_id must be a positive integer"
        raise TypeError(msg)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        msg = "canonical_signal_id must be a positive integer"
        raise ValueError(msg) from exc
    if value <= 0:
        msg = "canonical_signal_id must be a positive integer"
        raise ValueError(msg)
    return value


def _strategy_id(signal: Signal | None) -> str:
    """Return the mandatory strategy attribution for canonical orders."""
    strategy_id = "" if signal is None else str(signal.strategy_id or "").strip()
    if not strategy_id:
        msg = "Canonical order persistence requires an attributed strategy signal"
        raise ValueError(msg)
    return strategy_id


def _order_idempotency_key(execution_key: str | None, intent_index: int) -> str | None:
    """Derive one stable, bounded identity per concrete order leg."""
    normalized = str(execution_key or "").strip()
    if not normalized:
        return None
    material = f"{normalized}:order-leg:{intent_index}".encode()
    return hashlib.sha256(material).hexdigest()


class OrderExecutor:
    """Submit orders to a broker adapter, persist + resolve pending rows."""

    def __init__(
        self,
        *,
        tracker: ReconciliationTracker,
        canonical_execution_store: _CanonicalExecutionStore | None = None,
        account_serializer: AccountExecutionSerializer | None = None,
    ) -> None:
        self._tracker = tracker
        self._canonical_execution_store = canonical_execution_store
        self._account_serializer = account_serializer

    def _abort_pre_submission(
        self,
        *,
        canonical_order_id: int | None,
        reason: str,
        code: str,
    ) -> None:
        """Close a canonical attempt when the broker was never called."""
        if canonical_order_id is None or self._canonical_execution_store is None:
            return
        self._canonical_execution_store.abort_pre_submission(
            order_id=canonical_order_id,
            reason=reason,
            code=code,
        )

    async def execute(
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
        """Submit the entry, then place bracket exits only after it fills.

        On a spot broker you cannot SELL an asset you do not yet hold, and market
        fills settle asynchronously — so submitting the stop/take-profit legs in the
        same synchronous pass as the entry fails with "insufficient balance". Instead:
        submit the primary (entry) leg(s) first, and only place the reduce-only exit
        legs once the entry has filled, sized to the quantity that actually filled.
        """
        if self._account_serializer is not None:
            lease = self._account_serializer.current_lease()
            if (
                lease.user_id != str(user_id)
                or broker_account_id is None
                or lease.broker_account_id != int(broker_account_id)
            ):
                message = "Broker submission is outside the current account execution generation"
                raise AccountExecutionFenceLostError(message)

        async def _submit(
            intent_index: int,
            intent: OrderIntent | OptionsIntent,
        ) -> BrokerOrderResult:
            return await self._submit_intent(
                broker,
                intent,
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
                order_idempotency_key=_order_idempotency_key(
                    execution_key,
                    intent_index,
                ),
            )

        indexed_intents = list(enumerate(intents))
        exit_legs = [
            (index, intent) for index, intent in indexed_intents if self._is_bracket_exit(intent)
        ]
        primary_legs = [
            (index, intent)
            for index, intent in indexed_intents
            if not self._is_bracket_exit(intent)
        ]

        results: list[BrokerOrderResult] = []
        filled_qty = 0.0
        for intent_index, intent in primary_legs:
            if self._is_close(intent):
                # A resting OCO bracket / stop / take-profit reserves the base
                # balance on spot brokers, so a flatten submitted while it rests
                # either rejects ("insufficient funds") or orphans a live GTC
                # bracket that can fire later against new balance. Cancel this
                # context's resting orders for the symbol first.
                await self._cancel_resting_orders(
                    broker,
                    symbol=intent.symbol,
                    broker_code=broker_code,
                    credential_ref=credential_ref,
                    broker_environment=broker_environment,
                )
            result = await _submit(intent_index, intent)
            results.append(result)
            # Count partial fills too: an entry cancelled after a timeout can still
            # have filled part of its size — that quantity is a LIVE position and
            # needs protective exits sized to it.
            if result.filled_quantity:
                filled_qty += float(result.filled_quantity)

        if exit_legs:
            if filled_qty > 0:
                for intent_index, intent in exit_legs:
                    # Size exits to what actually filled (fees/slippage/partial
                    # fills can shave the requested size); the adapter quantizes
                    # DOWN so we never oversell.
                    intent.quantity = filled_qty
                    results.append(await _submit(intent_index, intent))
            else:
                logger.warning(
                    "Bracket entry was cancelled/rejected with zero fill; "
                    "skipping %d protective exit(s) for %s",
                    len(exit_legs),
                    getattr(signal, "symbol", "?"),
                )
        return results

    @staticmethod
    def _is_bracket_exit(intent: OrderIntent | OptionsIntent) -> bool:
        """A reduce-only stop-loss / take-profit leg (placed after the entry fills)."""
        if isinstance(intent, OptionsIntent):
            return False
        meta = getattr(intent, "metadata", None) or {}
        return meta.get("purpose") in {"stop_loss", "take_profit", "bracket"}

    @staticmethod
    def _is_close(intent: OrderIntent | OptionsIntent) -> bool:
        """A close/flatten order (built by ``OrderBuilder._build_close_order``)."""
        meta = getattr(intent, "metadata", None) or {}
        return meta.get("purpose") == "close_position"

    async def _cancel_resting_orders(
        self,
        broker: BrokerAdapter,
        *,
        symbol: str,
        broker_code: str,
        credential_ref: str | None,
        broker_environment: str | None,
    ) -> None:
        """Cancel this context's tracked resting orders for ``symbol`` before a close.

        Scoped strictly to orders this executor tracked for the same broker /
        credential / environment context — never another user's or strategy's
        orders. A cancel failure (e.g. the resting order already filled) must not
        block the flatten: log and proceed so the remaining quantity is closed.
        """
        cache = self._tracker.pending_orders_cache
        for key, row in list(cache.items()):
            if (
                row.get("symbol") != symbol
                or row.get("broker") != broker_code
                or row.get("credential_ref") != credential_ref
                or row.get("broker_environment") != broker_environment
            ):
                continue
            broker_order_id = row.get("broker_order_id") or key
            try:
                cancelled = await broker.cancel_order(str(broker_order_id))
            except Exception:
                # Boundary catch: broker cancel can fail in many ways (network,
                # already-filled order); the close must still be attempted.
                logger.exception(
                    "Failed to cancel resting order %s for %s before close",
                    broker_order_id,
                    symbol,
                )
                continue
            if cancelled:
                cache.pop(key, None)
                logger.info(
                    "Cancelled resting order %s for %s before close",
                    broker_order_id,
                    symbol,
                )
            else:
                logger.warning(
                    "Resting order %s for %s could not be cancelled (already filled?); "
                    "proceeding with close",
                    broker_order_id,
                    symbol,
                )

    async def _submit_intent(  # noqa: PLR0911, PLR0912, PLR0915
        self,
        broker: BrokerAdapter,
        intent: OrderIntent | OptionsIntent,
        *,
        breaker_key: str | None,
        user_id: str,
        signal: Signal | None,
        execution_mode: str,
        broker_code: str,
        broker_environment: str | None,
        credential_ref: str | None,
        run_id: str | None,
        broker_account_id: int | None,
        settlement_currency: str | None,
        order_idempotency_key: str | None,
    ) -> BrokerOrderResult:
        """Persist → submit → resolve a single order intent (crash-safe)."""
        cache = self._tracker.pending_orders_cache
        persisted_account_id: int | None = None
        persistence_enabled = bool(getattr(self._tracker, "persistence_enabled", False))
        if signal is not None and persistence_enabled:
            if settlement_currency is None:
                return BrokerOrderResult.rejected(
                    "Order persistence requires settlement_currency",
                    code="settlement_currency_required",
                )
            try:
                persisted_account_id = _require_positive_account_id(cast("int", broker_account_id))
            except ValueError as exc:
                return BrokerOrderResult.rejected(
                    str(exc),
                    code="broker_account_required",
                )
        # Stable pre-submission ID so the row exists before the broker call. If the
        # broker returns its own ID we update the row.
        pre_order_id = (
            f"pre-{order_idempotency_key[:32]}"
            if order_idempotency_key
            else f"pre-{uuid.uuid4().hex[:12]}"
        )
        canonical_order_id: int | None = None
        if self._canonical_execution_store is not None and settlement_currency is None:
            return BrokerOrderResult.rejected(
                "Canonical order requires settlement_currency",
                code="settlement_currency_required",
            )
        if self._canonical_execution_store is not None:
            try:
                canonical_ref = self._canonical_execution_store.create_order(
                    local_order_id=pre_order_id,
                    user_id=user_id,
                    account_id=broker_account_id,
                    broker_code=broker_code,
                    execution_mode=execution_mode,
                    strategy_id=_strategy_id(signal),
                    canonical_signal_id=_canonical_signal_id(signal),
                    instr_id=_instrument_id(signal),
                    intent=intent,
                    settlement_currency=settlement_currency,
                    idempotency_key=order_idempotency_key,
                    broker_environment=broker_environment,
                )
                canonical_order_id = canonical_ref.order_id
                pre_order_id = str(getattr(canonical_ref, "local_order_id", None) or pre_order_id)
                if bool(getattr(canonical_ref, "reused", False)):
                    if str(getattr(canonical_ref, "state", "")) == "submission_unknown":
                        return BrokerOrderResult(
                            success=False,
                            order_id=None,
                            error_message=(
                                "Broker submission outcome remains unknown; "
                                "reconciliation by client_order_id is required"
                            ),
                            error_code="submission_unknown",
                            raw_response={
                                "canonical_order_id": canonical_order_id,
                                "client_order_id": client_order_id_for(
                                    dict(getattr(intent, "metadata", None) or {})
                                )
                                or pre_order_id,
                            },
                        )
                    durable = self._canonical_execution_store.read_durable_result(
                        canonical_order_id
                    )
                    if durable is None:
                        return BrokerOrderResult.rejected(
                            "Canonical order replay could not read its durable state",
                            code="canonical_order_recovery_failed",
                        )
                    durable_result, durable_fills = durable
                    try:
                        self._tracker.resolve_order(
                            pre_order_id,
                            durable_result,
                            fills=durable_fills,
                            canonical_order_id=canonical_order_id,
                            instr_id=_instrument_id(signal),
                        )
                    except (PendingOrderPersistenceError, RuntimeError, ValueError):
                        # The canonical ledger is authoritative. A missing or
                        # still-unavailable pending projection remains a
                        # reconciliation concern, never permission to resubmit.
                        logger.exception(
                            "Canonical order replay recovered durable broker state "
                            "but could not close its pending projection",
                            order_id=pre_order_id,
                            canonical_order_id=canonical_order_id,
                        )
                    cache.pop(pre_order_id, None)
                    if durable_result.order_id:
                        cache.pop(durable_result.order_id, None)
                    return durable_result
            except Exception as exc:
                logger.exception(
                    "Canonical order creation failed; broker submission blocked",
                    user_id=user_id,
                    broker=broker_code,
                    broker_account_id=broker_account_id,
                )
                return BrokerOrderResult.rejected(
                    f"Canonical order persistence failed: {exc}",
                    code="canonical_order_persistence_failed",
                )

        # Persist *before* broker submission for crash safety.
        if (
            signal is not None
            and settlement_currency is not None
            and persisted_account_id is not None
        ):
            try:
                persisted_order_id = self._tracker.persist_order(
                    order_id=pre_order_id,
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
                    idempotency_key=order_idempotency_key,
                    broker_account_id=persisted_account_id,
                    canonical_order_id=canonical_order_id,
                )
            except PendingOrderPersistenceError:
                logger.exception(
                    "Pending-order persistence failed; broker submission blocked",
                    order_id=pre_order_id,
                    user_id=user_id,
                    broker=broker_code,
                    broker_account_id=persisted_account_id,
                )
                self._abort_pre_submission(
                    canonical_order_id=canonical_order_id,
                    reason="Pending-order persistence failed",
                    code="pending_order_persistence_failed",
                )
                return BrokerOrderResult.rejected(
                    "Pending-order persistence failed",
                    code="pending_order_persistence_failed",
                )
            except (RuntimeError, TypeError, ValueError):
                self._abort_pre_submission(
                    canonical_order_id=canonical_order_id,
                    reason="Unexpected pending-order persistence failure",
                    code="pending_order_persistence_contract_failed",
                )
                raise
            if persisted_order_id != pre_order_id:
                logger.error(
                    "Pending-order persistence acknowledgement mismatch; broker submission blocked",
                    order_id=pre_order_id,
                    persisted_order_id=persisted_order_id,
                    user_id=user_id,
                    broker=broker_code,
                    broker_account_id=persisted_account_id,
                )
                self._abort_pre_submission(
                    canonical_order_id=canonical_order_id,
                    reason="Pending-order persistence acknowledgement mismatch",
                    code="pending_order_persistence_failed",
                )
                return BrokerOrderResult.rejected(
                    "Pending-order persistence was not confirmed",
                    code="pending_order_persistence_failed",
                )

        if canonical_order_id is not None and isinstance(broker, PaperBroker):
            instrument_id = _instrument_id(signal)
            if instrument_id is None:
                self._abort_pre_submission(
                    canonical_order_id=canonical_order_id,
                    reason="Local paper order has no canonical instrument identity",
                    code="canonical_paper_binding_failed",
                )
                return BrokerOrderResult.rejected(
                    "Local paper order requires canonical instrument identity",
                    code="canonical_paper_binding_failed",
                )
            try:
                broker.bind_canonical_order(
                    intent,
                    order_id=canonical_order_id,
                    instrument_id=instrument_id,
                )
            except Exception as exc:
                self._abort_pre_submission(
                    canonical_order_id=canonical_order_id,
                    reason=f"Local paper durable-fill binding failed: {exc}",
                    code="canonical_paper_binding_failed",
                )
                logger.exception(
                    "Local paper durable-fill binding failed; submission blocked",
                    canonical_order_id=canonical_order_id,
                    instr_id=instrument_id,
                )
                return BrokerOrderResult.rejected(
                    "Local paper durable-fill binding failed",
                    code="canonical_paper_binding_failed",
                )

        try:
            if isinstance(intent, OptionsIntent):
                result = await broker.submit_options_order(intent)
            else:
                result = await broker.submit_order(intent)
        except Exception as exc:
            canonical_store = self._canonical_execution_store
            if (
                canonical_order_id is not None
                and isinstance(broker, PaperBroker)
                and canonical_store is not None
            ):
                durable = canonical_store.read_durable_result(canonical_order_id)
                if durable is not None and durable[0].has_fill:
                    durable_result, durable_fills = durable
                    logger.exception(
                        "Local paper state update failed after durable fill commit; "
                        "recovering canonical result",
                        canonical_order_id=canonical_order_id,
                    )
                    try:
                        self._tracker.resolve_order(
                            pre_order_id,
                            durable_result,
                            fills=durable_fills,
                            canonical_order_id=canonical_order_id,
                            instr_id=_instrument_id(signal),
                        )
                    except (PendingOrderPersistenceError, RuntimeError, ValueError):
                        logger.exception(
                            "Durable paper result recovered but pending projection "
                            "could not be resolved",
                            order_id=pre_order_id,
                            canonical_order_id=canonical_order_id,
                        )
                    return durable_result
            # A transport/runtime exception after the submit call begins cannot
            # prove rejection: the venue may have accepted the order before the
            # acknowledgement was lost. Persist ambiguity under the stable
            # client identity and never permit an automatic resubmit.
            client_order_id = (
                client_order_id_for(dict(getattr(intent, "metadata", None) or {})) or pre_order_id
            )
            reason = (
                "Broker submission acknowledgement unavailable "
                f"({type(exc).__name__}); reconcile by client_order_id"
            )
            logger.exception(
                "Broker submission outcome unknown: %s %s",
                intent.side,
                intent.symbol,
            )
            try:
                if canonical_order_id is not None and canonical_store is not None:
                    canonical_store.mark_submission_unknown(
                        order_id=canonical_order_id,
                        reason=reason,
                    )
                marked = self._tracker.mark_submission_unknown(
                    pre_order_id,
                    reason=reason,
                    client_order_id=client_order_id,
                )
                if not marked:
                    msg = f"Pending order {pre_order_id} could not be marked submission_unknown"
                    raise PendingOrderPersistenceError(msg)  # noqa: TRY301
            except Exception:
                logger.exception(
                    "Failed to persist ambiguous broker submission",
                    order_id=pre_order_id,
                    canonical_order_id=canonical_order_id,
                )
                raise
            cache[pre_order_id] = {
                **dict(cache.get(pre_order_id) or {}),
                "order_id": pre_order_id,
                "client_order_id": client_order_id,
                "broker_order_id": None,
                "breaker_key": breaker_key,
                "credential_ref": credential_ref,
                "broker_environment": broker_environment,
                "symbol": intent.symbol,
                "side": intent.side,
                "broker": broker_code,
                "execution_mode": execution_mode,
                "status": "submission_unknown",
                "broker_account_id": broker_account_id,
                "canonical_order_id": canonical_order_id,
                "instr_id": _instrument_id(signal),
                "settlement_currency": settlement_currency,
                "error_message": reason,
            }
            return BrokerOrderResult(
                success=False,
                order_id=None,
                error_message=reason,
                error_code="submission_unknown",
                raw_response={"client_order_id": client_order_id},
            )

        # Update the durable pending-orders row with the broker response, then
        # sync the in-memory cache the reconciliation worker reads from.
        actual_id = result.order_id or pre_order_id
        persistence_failed = False
        fills: list[BrokerFill] = []
        # Exact venue trades are required only when the canonical ledger is
        # wired.  Lightweight/non-persistent engine instances intentionally do
        # not force every test or notify-only adapter through the fill port.
        # Production live routing always wires the canonical store and is
        # separately gated on exact-fill capability.
        if result.has_fill and self._canonical_execution_store is not None:
            if result.order_id is None:
                persistence_failed = True
                logger.error(
                    "Filled broker result omitted broker order ID; preserving "
                    "pending recovery state",
                    order_id=actual_id,
                    canonical_order_id=canonical_order_id,
                )
            else:
                try:
                    fills = await broker.get_fills(result.order_id)
                except Exception:
                    persistence_failed = True
                    logger.exception(
                        "Exact broker fill retrieval failed; preserving pending recovery state",
                        order_id=actual_id,
                        canonical_order_id=canonical_order_id,
                    )
        if not persistence_failed:
            try:
                self._tracker.resolve_order(
                    pre_order_id,
                    result,
                    fills=fills,
                    canonical_order_id=canonical_order_id,
                    instr_id=_instrument_id(signal),
                )
            except Exception:
                persistence_failed = True
                logger.exception(
                    "Post-submit order/fill persistence failed; preserving broker economics",
                    order_id=actual_id,
                    canonical_order_id=canonical_order_id,
                )

        if persistence_failed:
            logger.error(
                "Broker order requires reconciliation before recovery state can close",
                order_id=actual_id,
                canonical_order_id=canonical_order_id,
            )

        if actual_id:
            if result.is_terminal and not persistence_failed:
                cache.pop(pre_order_id, None)
                cache.pop(actual_id, None)
            else:
                cache.pop(pre_order_id, None)
                cache[actual_id] = {
                    "order_id": pre_order_id,
                    "client_order_id": (
                        client_order_id_for(dict(getattr(intent, "metadata", None) or {}))
                        or pre_order_id
                    ),
                    "broker_order_id": (actual_id if actual_id != pre_order_id else None),
                    "breaker_key": breaker_key,
                    "credential_ref": credential_ref,
                    "broker_environment": broker_environment,
                    "symbol": intent.symbol,
                    "side": intent.side,
                    "broker": broker_code,
                    "execution_mode": execution_mode,
                    "status": result.status.value,
                    "filled_quantity": result.filled_quantity,
                    "filled_price": result.filled_price,
                    "commission": str(result.commission),
                    "broker_account_id": broker_account_id,
                    "canonical_order_id": canonical_order_id,
                    "instr_id": _instrument_id(signal),
                    "settlement_currency": settlement_currency,
                    "canonical_persistence_pending": persistence_failed,
                }

        if result.is_filled:
            logger.info(
                "Order filled: %s %s %.4f @ %.4f",
                intent.side,
                intent.symbol,
                result.filled_quantity,
                result.average_price,
            )
        elif result.is_rejected:
            logger.warning(
                "Order rejected: %s %s - %s",
                intent.side,
                intent.symbol,
                result.error_message,
            )
        return result

    @staticmethod
    def aggregate(
        *,
        signal_id: str,
        symbol: str,
        exec_mode: ExecutionMode,
        broker_type: BrokerType,
        order_results: list[BrokerOrderResult],
        broker_account_id: int | None = None,
        settlement_currency: str | None = None,
    ) -> ExecutionResult:
        """Aggregate per-intent broker results into a single ExecutionResult."""
        return aggregate_order_results(
            signal_id=signal_id,
            symbol=symbol,
            exec_mode=exec_mode,
            broker_type=broker_type,
            order_results=order_results,
            broker_account_id=broker_account_id,
            settlement_currency=settlement_currency,
        )
