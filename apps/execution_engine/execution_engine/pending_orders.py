"""Durable pending-order persistence helpers for execution recovery."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from lib_common.logging import get_logger
from lib_strategy.signals.signal import Signal
from lib_strategy.signals.utils import ensure_utc

from ._persistence_common import _require_positive_account_id
from .brokers.base import BrokerOrderResult, OrderStatus
from .models import OptionsIntent, OrderIntent, client_order_id_for

logger = get_logger(__name__)


_DURABLE_PAPER_ORDER_TYPES = frozenset({"bracket", "limit", "oco", "stop", "take_profit"})


def _resolve_source_watermark(
    session: Any,
    *,
    instr_id: int | None,
    metadata: dict[str, Any],
) -> tuple[datetime, int]:
    """Resolve and verify the exact committed source row for a resting paper order."""
    try:
        from lib_application.db.models import InstrumentPrice  # noqa: PLC0415
    except ImportError as exc:
        msg = "Instrument-price model is unavailable"
        raise PendingOrderPersistenceError(msg) from exc

    source = str(metadata.get("market_data_source") or "").strip()
    timeframe = str(metadata.get("market_data_timeframe") or "").strip()
    raw_price_id = metadata.get("source_price_id")
    raw_revision = metadata.get("source_content_revision")
    raw_timestamp = metadata.get("source_price_ts")
    if (
        instr_id is None
        or not source
        or not timeframe
        or not isinstance(raw_price_id, int)
        or isinstance(raw_price_id, bool)
        or raw_price_id <= 0
        or not isinstance(raw_revision, int)
        or isinstance(raw_revision, bool)
        or raw_revision <= 0
        or not isinstance(raw_timestamp, (datetime, str))
    ):
        msg = (
            "Durable local-paper resting orders require exact instrument, "
            "source, timeframe, source price id, revision, and timestamp provenance"
        )
        raise ValueError(msg)
    try:
        expected_ts = ensure_utc(
            raw_timestamp
            if isinstance(raw_timestamp, datetime)
            else datetime.fromisoformat(raw_timestamp)
        )
    except ValueError as exc:
        msg = "Durable local-paper source timestamp is invalid"
        raise ValueError(msg) from exc

    row = session.get(InstrumentPrice, raw_price_id)
    if row is None:
        msg = f"Durable local-paper source price {raw_price_id} does not exist"
        raise ValueError(msg)
    row_ts = ensure_utc(row.ts)
    if (
        int(row.instr_id) != instr_id
        or str(row.source) != source
        or str(row.timeframe) != timeframe
        or int(row.content_revision) != raw_revision
        or row_ts != expected_ts
    ):
        msg = f"Durable local-paper source price {raw_price_id} provenance changed"
        raise ValueError(msg)
    return row_ts, raw_revision


_CANCELLABLE_STATUSES = frozenset({"pending", "submitted", "working", "partially_filled"})
_TERMINAL_ORDER_STATES = frozenset({"filled", "rejected", "canceled"})


class PendingOrderPersistenceError(RuntimeError):
    """A pending-order write could not be confirmed durably."""


class PendingOrderRepository:
    """Persistence adapter for pending order recovery state."""

    def __init__(self, session_factory: Callable[[], Any] | None) -> None:
        self._session_factory = session_factory

    @property
    def enabled(self) -> bool:
        """Whether this adapter can durably write pending-order state."""
        return self._session_factory is not None

    def load(
        self,
        *,
        breaker_key: str | None = None,
        statuses: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Load recoverable pending orders from the database."""
        if self._session_factory is None:
            return {}
        try:
            from lib_application.db.models import (  # noqa: PLC0415
                PendingOrder,
            )
        except ImportError as exc:
            msg = "Pending-order model is unavailable"
            raise PendingOrderPersistenceError(msg) from exc

        recoverable_statuses = tuple(
            statuses
            or [
                "pending",
                "submission_unknown",
                "submitted",
                "working",
                "partially_filled",
            ]
        )
        try:
            with self._session_factory() as session:
                query = session.query(PendingOrder).filter(
                    PendingOrder.status.in_(recoverable_statuses)
                )
                if breaker_key is not None:
                    query = query.filter(PendingOrder.breaker_key == breaker_key)
                rows = query.all()
        except (SQLAlchemyError, OSError) as exc:
            logger.exception("Failed to load pending orders from database")
            msg = "Pending-order recovery state could not be loaded"
            raise PendingOrderPersistenceError(msg) from exc

        pending_orders: dict[str, dict[str, Any]] = {}
        for row in rows:
            effective_order_id = str(row.broker_order_id or row.order_id)
            pending_orders[effective_order_id] = {
                "order_id": str(row.order_id),
                "client_order_id": row.client_order_id,
                "broker_order_id": str(row.broker_order_id or "") or None,
                "breaker_key": row.breaker_key,
                "credential_ref": row.credential_ref,
                "broker_environment": row.broker_environment,
                "symbol": row.symbol,
                "settlement_currency": row.settlement_currency,
                "side": row.side,
                "broker": row.broker,
                # user_id is required by ReconciliationTracker's context
                # rehydration (per-user broker resolution after a restart).
                "user_id": row.user_id,
                "execution_mode": row.execution_mode,
                "status": row.status,
                "filled_quantity": float(row.filled_quantity or 0),
                "cumulative_filled_quantity": float(row.cumulative_filled_quantity or 0),
                "filled_price": float(row.filled_price or 0) or None,
                "commission": str(row.commission or 0),
                "commission_currency": getattr(row, "commission_currency", None),
                "broker_account_id": getattr(row, "broker_account_id", None),
                "canonical_order_id": getattr(row, "canonical_order_id", None),
                "instr_id": row.instr_id,
                "trigger_price": (
                    float(row.trigger_price) if row.trigger_price is not None else None
                ),
                "limit_price": float(row.limit_price) if row.limit_price is not None else None,
                "purpose": row.purpose,
                "time_in_force": row.time_in_force,
                "reduce_only": row.reduce_only,
                "parent_order_id": row.parent_order_id,
                "oco_group_id": row.oco_group_id,
                "market_data_source": row.market_data_source,
                "market_data_timeframe": row.market_data_timeframe,
                "last_market_data_ts": row.last_market_data_ts,
                "last_market_data_revision": row.last_market_data_revision,
                "trigger_policy_version": row.trigger_policy_version,
            }
        return pending_orders

    def persist(
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
        """Commit a pending order before broker submission.

        Returns the committed local order ID. A configured adapter never
        degrades to best-effort persistence: unavailable models, database
        errors, and operating-system I/O failures raise before the caller may
        submit the order to a broker.
        """
        broker_account_id = _require_positive_account_id(broker_account_id)
        if self._session_factory is None:
            msg = "Pending-order persistence is not configured"
            raise PendingOrderPersistenceError(msg)
        normalized_settlement = str(settlement_currency or "").strip().upper()
        if not normalized_settlement or not normalized_settlement.isalnum():
            msg = "Pending-order persistence requires settlement_currency"
            raise ValueError(msg)
        try:
            from lib_application.db.models import (  # noqa: PLC0415
                PendingOrder,
            )
        except ImportError as exc:
            msg = "Pending-order model is unavailable"
            raise PendingOrderPersistenceError(msg) from exc

        side = intent.side if hasattr(intent, "side") else "MULTI_LEG"
        order_type = getattr(intent, "order_type", "market") or "market"
        quantity = float(getattr(intent, "quantity", 0) or 0)
        limit_price = getattr(intent, "limit_price", None)
        trigger_price = getattr(intent, "stop_price", None)
        metadata = dict(getattr(intent, "metadata", None) or {})
        client_order_id = client_order_id_for(metadata) or str(order_id).strip()
        if not client_order_id:
            msg = "Pending-order persistence requires client_order_id"
            raise ValueError(msg)
        # Durable order identity MUST be the canonical external_signal_id: the
        # restart drain guard compares this row against the canonical
        # projection chain's signal identity, and the per-runtime
        # Signal.signal_id never appears in canonical persistence (storing it
        # here crashlooped the engine on the first real delayed-fill restart).
        signal_id = str(signal.external_signal_id or "").strip()
        if not signal_id:
            msg = "Pending-order persistence requires the canonical external_signal_id"
            raise ValueError(msg)
        symbol = intent.symbol if hasattr(intent, "symbol") else signal.symbol
        instr_id: int | None = None
        if signal.instrument_id is not None:
            try:
                candidate = int(signal.instrument_id)
            except (TypeError, ValueError):
                candidate = 0
            if candidate > 0:
                instr_id = candidate

        try:
            with self._session_factory() as session:
                values: dict[str, Any] = {
                    "order_id": order_id,
                    "client_order_id": client_order_id,
                    "user_id": user_id,
                    "signal_id": str(signal_id),
                    "instr_id": instr_id,
                    "symbol": symbol,
                    "settlement_currency": normalized_settlement,
                    "side": side,
                    "order_type": order_type,
                    "quantity": Decimal(str(quantity)),
                    "limit_price": Decimal(str(limit_price)) if limit_price else None,
                    "trigger_price": (
                        Decimal(str(trigger_price)) if trigger_price is not None else None
                    ),
                    "purpose": (
                        str(metadata["purpose"]).strip() if metadata.get("purpose") else None
                    ),
                    "time_in_force": str(getattr(intent, "time_in_force", "gtc")).lower(),
                    "reduce_only": bool(metadata.get("reduce_only", False)),
                    "parent_order_id": (
                        str(metadata["parent_order_id"]).strip()
                        if metadata.get("parent_order_id")
                        else (
                            str(metadata["parent_signal_id"]).strip()
                            if metadata.get("parent_signal_id")
                            else None
                        )
                    ),
                    "oco_group_id": (
                        str(metadata["oco_group_id"]).strip()
                        if metadata.get("oco_group_id")
                        else (
                            str(metadata["parent_signal_id"]).strip()
                            if metadata.get("parent_signal_id")
                            and metadata.get("purpose") in {"bracket", "stop_loss", "take_profit"}
                            else None
                        )
                    ),
                    "cumulative_filled_quantity": Decimal("0"),
                    "market_data_source": (
                        str(metadata["market_data_source"]).strip()
                        if metadata.get("market_data_source")
                        else None
                    ),
                    "market_data_timeframe": (
                        str(metadata["market_data_timeframe"]).strip()
                        if metadata.get("market_data_timeframe")
                        else None
                    ),
                    "execution_mode": execution_mode,
                    "broker": broker_code,
                    "broker_environment": broker_environment,
                    "credential_ref": credential_ref,
                    "breaker_key": breaker_key,
                    "status": "pending",
                    "run_id": run_id,
                    "idempotency_key": idempotency_key,
                    "strategy_id": signal.strategy_id,
                }
                # Migration-owned linkage columns are populated as soon as they
                # exist, while keeping this code deployable before that migration.
                if hasattr(PendingOrder, "broker_account_id"):
                    values["broker_account_id"] = broker_account_id
                if hasattr(PendingOrder, "canonical_order_id"):
                    values["canonical_order_id"] = canonical_order_id
                if (
                    str(broker_code).strip().lower() == "paper"
                    and str(broker_environment or "").strip().lower() == "paper"
                    and str(order_type).strip().lower() in _DURABLE_PAPER_ORDER_TYPES
                ):
                    source_ts, source_revision = _resolve_source_watermark(
                        session,
                        instr_id=instr_id,
                        metadata=metadata,
                    )
                    values["last_market_data_ts"] = source_ts
                    values["last_market_data_revision"] = source_revision
                record = PendingOrder(**values)
                session.add(record)
                session.commit()
        except (SQLAlchemyError, OSError) as exc:
            logger.exception("Failed to persist pending order %s", order_id)
            msg = f"Pending order {order_id} could not be persisted"
            raise PendingOrderPersistenceError(msg) from exc
        return order_id

    def mark_terminal(self, order_id: str, *, status: str, reason: str | None = None) -> bool:
        """Force a pending order into a terminal status (reconciliation terminal-sync).

        ``resolve`` is only invoked on the submit path, so a row stranded in
        ``submitted``/``pending`` (broker restart, PaperBroker memory-only book,
        crash between submit and resolve) has no other writer. The
        reconciliation worker calls this when the broker definitively reports
        the order terminal — or does not know it at all — so the row stops
        re-flagging ``missing_broker_open_order`` every cycle.

        ``order_id`` may be either the local ``order_id`` or the broker-assigned
        ``broker_order_id`` (reconciliation keys pending orders by whichever is
        available). Returns ``False`` when the row is already absent; otherwise
        returns ``True`` only after the terminal transition commits.
        """
        if self._session_factory is None:
            msg = "Pending-order persistence is not configured"
            raise PendingOrderPersistenceError(msg)
        try:
            from lib_application.db.models import (  # noqa: PLC0415
                PendingOrder,
            )
        except ImportError as exc:
            msg = "Pending-order model is unavailable"
            raise PendingOrderPersistenceError(msg) from exc

        try:
            with self._session_factory() as session:
                row = (
                    session.query(PendingOrder)
                    .filter(
                        (PendingOrder.order_id == order_id)
                        | (PendingOrder.broker_order_id == order_id)
                    )
                    .one_or_none()
                )
                if row is None:
                    return False
                row.status = status
                if reason:
                    row.error_message = reason
                row.resolved_at = datetime.now(tz=UTC)
                session.commit()
        except (SQLAlchemyError, OSError) as exc:
            logger.exception("Failed to mark pending order %s terminal", order_id)
            msg = f"Pending order {order_id} could not be marked terminal"
            raise PendingOrderPersistenceError(msg) from exc
        return True

    def cancel_working(self, order_id: str, *, reason: str) -> bool:
        """Durably cancel a still-working row; terminal rows are never overwritten.

        A close/flatten supersedes this context's resting protective orders. The
        broker's in-memory book is not authoritative for durable paper orders
        (it is empty after a restart), so the pending row and its canonical
        ``orders`` state are closed here in one transaction. Returns ``True``
        only when a non-terminal row was cancelled.
        """
        if self._session_factory is None:
            msg = "Pending-order persistence is not configured"
            raise PendingOrderPersistenceError(msg)
        try:
            from lib_application.db.models import Order, PendingOrder  # noqa: PLC0415
        except ImportError as exc:
            msg = "Pending-order model is unavailable"
            raise PendingOrderPersistenceError(msg) from exc

        try:
            with self._session_factory() as session:
                row = (
                    session.query(PendingOrder)
                    .filter(
                        (PendingOrder.order_id == order_id)
                        | (PendingOrder.broker_order_id == order_id)
                    )
                    .with_for_update()
                    .one_or_none()
                )
                if row is None or row.status not in _CANCELLABLE_STATUSES:
                    return False
                resolved_at = datetime.now(tz=UTC)
                row.status = "cancelled"
                row.error_message = reason
                row.resolved_at = resolved_at
                if row.canonical_order_id is not None:
                    order = session.get(Order, row.canonical_order_id)
                    if order is not None and order.state not in _TERMINAL_ORDER_STATES:
                        order.state = "canceled"
                        order.last_update = resolved_at
                session.commit()
        except (SQLAlchemyError, OSError) as exc:
            logger.exception("Failed to cancel pending order %s", order_id)
            msg = f"Pending order {order_id} could not be cancelled"
            raise PendingOrderPersistenceError(msg) from exc
        return True

    def resolve(self, order_id: str, result: BrokerOrderResult) -> str:
        """Commit a broker response and return the resolved local order ID."""
        if self._session_factory is None:
            msg = "Pending-order persistence is not configured"
            raise PendingOrderPersistenceError(msg)
        try:
            from lib_application.db.models import (  # noqa: PLC0415
                PendingOrder,
            )
        except ImportError as exc:
            msg = "Pending-order model is unavailable"
            raise PendingOrderPersistenceError(msg) from exc

        status_map = {
            OrderStatus.PENDING: "submitted",
            OrderStatus.SUBMITTED: "submitted",
            OrderStatus.WORKING: "working",
            OrderStatus.PARTIALLY_FILLED: "partially_filled",
            OrderStatus.FILLED: "filled",
            OrderStatus.CANCELLED: "cancelled",
            OrderStatus.REJECTED: "rejected",
            OrderStatus.EXPIRED: "expired",
        }
        status = status_map[result.status]

        try:
            with self._session_factory() as session:
                row = (
                    session.query(PendingOrder)
                    .filter(
                        (PendingOrder.order_id == order_id)
                        | (PendingOrder.broker_order_id == order_id)
                    )
                    .one_or_none()
                )
                if row is None:
                    msg = f"Pending order {order_id} does not exist"
                    raise PendingOrderPersistenceError(msg)
                row.status = status
                row.broker_order_id = result.order_id
                row.filled_quantity = Decimal(str(result.filled_quantity or 0))
                row.cumulative_filled_quantity = Decimal(str(result.filled_quantity or 0))
                row.filled_price = (
                    Decimal(str(result.average_price)) if result.average_price else None
                )
                row.commission = Decimal(str(result.commission or 0))
                if hasattr(row, "commission_currency"):
                    row.commission_currency = result.commission_currency
                row.error_message = result.error_message
                row.resolved_at = datetime.now(tz=UTC) if result.is_terminal else None
                row.submitted_at = datetime.now(tz=UTC)
                session.commit()
                resolved_order_id = str(row.order_id)
        except (SQLAlchemyError, OSError) as exc:
            logger.exception("Failed to resolve pending order %s", order_id)
            msg = f"Pending order {order_id} could not be resolved"
            raise PendingOrderPersistenceError(msg) from exc
        return resolved_order_id

    def mark_submission_unknown(
        self,
        order_id: str,
        *,
        reason: str,
        client_order_id: str | None = None,
    ) -> bool:
        """Persist an ambiguous broker submission without permitting a retry.

        The stable client identity remains the reconciliation key. Callers must
        reconcile before resubmitting; a timeout is not evidence that the
        broker rejected the original request.
        """
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            msg = "Unknown submission state requires a reason"
            raise ValueError(msg)
        return self._mark_submission_state(
            order_id,
            status="submission_unknown",
            reason=normalized_reason,
            client_order_id=client_order_id,
        )

    def resolve_by_client_order_id(
        self,
        *,
        broker_account_id: int,
        client_order_id: str,
        result: BrokerOrderResult,
    ) -> str:
        """Resolve an ambiguous submission using account-scoped client identity."""
        if self._session_factory is None:
            msg = "Pending-order persistence is not configured"
            raise PendingOrderPersistenceError(msg)
        from lib_application.db.models import PendingOrder  # noqa: PLC0415

        normalized_client_id = str(client_order_id or "").strip()
        if not normalized_client_id:
            msg = "Client-order reconciliation requires client_order_id"
            raise ValueError(msg)
        with self._session_factory() as session:
            row = (
                session.query(PendingOrder)
                .filter(
                    PendingOrder.broker_account_id
                    == _require_positive_account_id(broker_account_id),
                    PendingOrder.client_order_id == normalized_client_id,
                )
                .one_or_none()
            )
            if row is None:
                msg = (
                    f"Pending order client identity {normalized_client_id!r} "
                    f"does not exist for account {broker_account_id}"
                )
                raise PendingOrderPersistenceError(msg)
            local_order_id = str(row.order_id)
        return self.resolve(local_order_id, result)

    def _mark_submission_state(
        self,
        order_id: str,
        *,
        status: str,
        reason: str,
        client_order_id: str | None,
    ) -> bool:
        if self._session_factory is None:
            msg = "Pending-order persistence is not configured"
            raise PendingOrderPersistenceError(msg)
        from lib_application.db.models import PendingOrder  # noqa: PLC0415

        try:
            with self._session_factory() as session:
                row = session.get(PendingOrder, order_id)
                if row is None:
                    return False
                if client_order_id is not None:
                    normalized_client_id = str(client_order_id).strip()
                    if not normalized_client_id:
                        msg = "client_order_id cannot be blank"
                        raise ValueError(msg)
                    row.client_order_id = normalized_client_id
                row.status = status
                row.error_message = reason
                session.commit()
        except (SQLAlchemyError, OSError) as exc:
            logger.exception("Failed to mark pending order %s %s", order_id, status)
            msg = f"Pending order {order_id} could not be marked {status}"
            raise PendingOrderPersistenceError(msg) from exc
        return True
